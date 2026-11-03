"""媒体文件落盘、按扩展名分类与内容去重。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from .config import PluginConfig
from .paths import (
    extension_of,
    file_uri_to_path,
    is_file_uri,
    sanitize_component,
    sha256_file,
    url_basename,
)
from .textutil import brief_source, guess_extension_from_url
from .writer import write_json_atomic

DEFAULT_CATEGORIES: dict[str, list[str]] = {
    "图片": [
        "jpg", "jpeg", "png", "gif", "webp", "bmp", "svg",
        "ico", "tif", "tiff", "heic", "heif", "avif",
    ],
    "音频": [
        "mp3", "wav", "flac", "aac", "ogg", "oga",
        "m4a", "amr", "silk", "opus", "wma",
    ],
    "视频": ["mp4", "mov", "mkv", "avi", "webm", "flv", "wmv", "m4v", "mpeg", "mpg"],
    "文档": [
        "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
        "txt", "md", "rtf", "odt", "ods", "csv", "tsv",
    ],
    "压缩包": ["zip", "rar", "7z", "tar", "gz", "bz2", "xz"],
    "代码": [
        "py", "js", "ts", "tsx", "jsx", "json", "yaml", "yml", "toml",
        "xml", "html", "css", "sql", "sh", "bat", "ps1",
        "java", "c", "cpp", "h", "go", "rs", "rb", "php",
    ],
    "字体": ["ttf", "otf", "woff", "woff2"],
    "其他": [],
}

_KIND_CATEGORY = {"image": "图片", "audio": "音频", "video": "视频", "file": "文档"}


def build_category_map(config: PluginConfig) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for category, extensions in DEFAULT_CATEGORIES.items():
        for ext in extensions:
            mapping[ext] = category
    custom = config.as_dict("media.categories")
    for category, extensions in custom.items():
        if isinstance(extensions, str):
            extensions = [extensions]
        if not isinstance(extensions, (list, tuple)):
            continue
        for ext in extensions:
            normalized = str(ext).strip().lower().lstrip(".")
            if normalized:
                mapping[normalized] = str(category)
    return mapping


def sniff_extension(head: bytes) -> str:
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG"):
        return "png"
    if head.startswith(b"GIF8"):
        return "gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if head.startswith(b"#!AMR"):
        return "amr"
    if head.startswith(b"OggS"):
        return "ogg"
    if head[4:8] == b"ftyp":
        return "mp4"
    return "bin"


def decode_base64_source(source: str) -> bytes:
    text = str(source)
    if text.startswith("base64://"):
        return base64.b64decode(text[len("base64://"):], validate=False)
    if text.startswith("data:"):
        _, _, payload = text.partition(",")
        return base64.b64decode(payload, validate=False)
    return base64.b64decode(text, validate=False)


def sniff_file_extension(path: Path) -> str:
    try:
        with open(path, "rb") as handle:
            head = handle.read(16)
    except OSError:
        return ""
    return sniff_extension(head)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class MediaStore:
    """把用户与模型产生的媒体复制到 files/<大类>/<首次日期>/。"""

    def __init__(
        self,
        root: Path,
        config: PluginConfig,
        logger: logging.Logger | None = None,
    ) -> None:
        self._root = Path(root)
        self._state = self._root / "state"
        self._index_path = self._state / "media_index.json"
        self._config = config
        self._media = config.media
        self._logger = logger or logging.getLogger("astrbot")
        self._ext_map = build_category_map(config)
        self._index: dict[str, list[Any]] = {}
        self._dirty = False
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(4)
        self._max_bytes = self._media.max_file_mb * 1024 * 1024

    @property
    def count(self) -> int:
        return len(self._index)

    def load(self) -> None:
        self._index = {}
        if not self._index_path.is_file():
            return
        try:
            payload = json.loads(self._index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        items = payload.get("items") if isinstance(payload, dict) else None
        if isinstance(items, dict):
            for sha, entry in items.items():
                if isinstance(entry, list) and entry:
                    self._index[str(sha)] = entry

    def save(self) -> None:
        if not self._dirty:
            return
        write_json_atomic(self._index_path, {"v": 1, "items": self._index})
        self._dirty = False

    async def flush(self) -> None:
        if not self._dirty:
            return
        await asyncio.to_thread(self.save)

    def category_for(self, ext: str, kind: str = "file") -> str:
        normalized = str(ext or "").lower().lstrip(".")
        if normalized and normalized in self._ext_map:
            return self._ext_map[normalized]
        return _KIND_CATEGORY.get(kind, "其他")

    async def store_source(
        self,
        source: str,
        *,
        name: str = "",
        kind: str = "file",
        day: str,
    ) -> dict[str, Any]:
        if not self._media.enabled:
            return {}
        text = str(source or "").strip()
        if not text:
            return {"e": "empty"}
        try:
            if text.startswith(("base64://", "data:")):
                data = await asyncio.to_thread(decode_base64_source, text)
                return await self._store_bytes(data, name=name, kind=kind, day=day)
            if text.startswith(("http://", "https://")):
                if not self._media.store_bytes:
                    return {"s": brief_source(text)}
                return await self._store_url(text, name=name, kind=kind, day=day)
            return await self._store_path(text, name=name, kind=kind, day=day)
        except Exception as exc:
            self._logger.warning("[llm_backup] 媒体备份失败 %s: %s", brief_source(text), exc)
            return {"s": brief_source(text), "e": type(exc).__name__}

    async def _store_path(
        self,
        source: str,
        *,
        name: str,
        kind: str,
        day: str,
    ) -> dict[str, Any]:
        path = Path(file_uri_to_path(source) if is_file_uri(source) else source)
        if not path.is_file():
            return {"s": brief_source(source), "e": "missing"}
        if not self._media.store_bytes:
            return {"s": brief_source(source)}
        size = path.stat().st_size
        if self._max_bytes and size > self._max_bytes:
            return {"s": brief_source(source), "e": "too_large", "b": size}
        sha = await asyncio.to_thread(sha256_file, path)
        cached = await self._lookup(sha)
        if cached:
            return cached
        ext = extension_of(name) or extension_of(path.name) or await asyncio.to_thread(
            sniff_file_extension, path
        )
        target = self._target(sha, name or path.name, kind, day, ext)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f"{target.name}.{uuid.uuid4().hex[:6]}.part")
        await asyncio.to_thread(shutil.copyfile, path, temp)
        os.replace(temp, target)
        return await self._register(sha, target, size, day)

    async def _store_bytes(
        self,
        data: bytes,
        *,
        name: str,
        kind: str,
        day: str,
    ) -> dict[str, Any]:
        if not self._media.store_bytes:
            return {"s": f"<{len(data)} bytes>"}
        if self._max_bytes and len(data) > self._max_bytes:
            return {"e": "too_large", "b": len(data)}
        sha = await asyncio.to_thread(lambda: sha256_bytes(data))
        cached = await self._lookup(sha)
        if cached:
            return cached
        ext = extension_of(name) or sniff_extension(data[:16])
        target = self._target(sha, name, kind, day, ext)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f"{target.name}.{uuid.uuid4().hex[:6]}.part")
        await asyncio.to_thread(lambda: temp.write_bytes(data))
        os.replace(temp, target)
        return await self._register(sha, target, len(data), day)

    async def _store_url(
        self,
        url: str,
        *,
        name: str,
        kind: str,
        day: str,
    ) -> dict[str, Any]:
        async with self._semaphore:
            temp_dir = self._state / "tmp"
            temp_dir.mkdir(parents=True, exist_ok=True)
            temp = temp_dir / f"dl_{uuid.uuid4().hex[:12]}"
            try:
                await self._download(url, temp)
                size = temp.stat().st_size
                sha = await asyncio.to_thread(sha256_file, temp)
                cached = await self._lookup(sha)
                if cached:
                    return cached
                ext = (
                    extension_of(name)
                    or guess_extension_from_url(url)
                    or await asyncio.to_thread(sniff_file_extension, temp)
                )
                target = self._target(
                    sha,
                    name or url_basename(url) or kind,
                    kind,
                    day,
                    ext,
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temp, target)
                return await self._register(sha, target, size, day)
            finally:
                if temp.exists():
                    temp.unlink(missing_ok=True)

    async def _download(self, url: str, dest: Path) -> None:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=self._media.download_timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                response.raise_for_status()
                written = 0
                with open(dest, "wb") as handle:
                    async for chunk in response.content.iter_chunked(1 << 16):
                        written += len(chunk)
                        if self._max_bytes and written > self._max_bytes:
                            raise ValueError("too_large")
                        handle.write(chunk)

    def _target(self, sha: str, name: str, kind: str, day: str, ext: str = "") -> Path:
        suffix = ext or extension_of(name) or "bin"
        category = self.category_for(suffix, kind)
        stem = Path(str(name).replace("\\", "/")).stem or kind
        safe_stem = sanitize_component(stem, max_bytes=60, fallback=kind)
        filename = f"{safe_stem}__{sha[:8]}.{suffix}"
        return (
            self._root
            / "files"
            / sanitize_component(category, max_bytes=40)
            / sanitize_component(day)
            / filename
        )

    async def _lookup(self, sha: str) -> dict[str, Any] | None:
        async with self._lock:
            entry = self._index.get(sha)
            if not entry:
                return None
            relative = str(entry[0])
            path = self._root / relative
            if not path.is_file():
                self._index.pop(sha, None)
                self._dirty = True
                return None
            return {"p": relative, "b": int(entry[1]) if len(entry) > 1 else path.stat().st_size}

    async def _register(self, sha: str, target: Path, size: int, day: str) -> dict[str, Any]:
        relative = target.relative_to(self._root).as_posix()
        async with self._lock:
            self._index[sha] = [relative, size, day]
            self._dirty = True
        return {"p": relative, "b": size}
