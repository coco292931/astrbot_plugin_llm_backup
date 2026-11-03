"""跨平台路径与文件名处理。"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_DRIVE_PREFIX = re.compile(r"^/[A-Za-z]:")
_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
MAX_COMPONENT_BYTES = 120


def truncate_bytes(text: str, max_bytes: int, suffix: str = "") -> str:
    """按 UTF-8 字节数截断字符串。"""
    encoded = str(text).encode("utf-8")
    if len(encoded) <= max_bytes:
        return str(text)
    budget = max_bytes - len(suffix.encode("utf-8"))
    if budget <= 0:
        return suffix
    return encoded[:budget].decode("utf-8", "ignore") + suffix


def sanitize_component(
    value: object,
    *,
    max_bytes: int = MAX_COMPONENT_BYTES,
    fallback: str = "unnamed",
) -> str:
    """把任意字符串转成 Windows 与 Linux 都合法的单级路径名。"""
    text = unicodedata.normalize("NFC", str(value or ""))
    text = _ILLEGAL_CHARS.sub("_", text)
    text = re.sub(r"\s+", " ", text).strip().strip(".")
    if not text:
        return fallback
    if text.split(".")[0].strip().lower() in _RESERVED_NAMES:
        text = f"_{text}"
    trimmed = truncate_bytes(text, max_bytes, suffix="_").rstrip(" .")
    return trimmed or fallback


def safe_join(root: Path, *parts: str) -> Path:
    """拼接路径并保证结果不会逃逸 root。"""
    base = Path(root).resolve(strict=False)
    target = base
    for part in parts:
        target = target / sanitize_component(part)
    target = target.resolve(strict=False)
    if target != base and not target.is_relative_to(base):
        raise ValueError(f"路径越界: {target}")
    return target


def is_file_uri(value: str) -> bool:
    return str(value or "").lower().startswith("file:")


def file_uri_to_path(uri: str) -> str:
    """把 file:// URI 转成当前平台可直接使用的路径。"""
    parsed = urlparse(str(uri))
    scheme = (parsed.scheme or "").lower()
    if scheme and scheme != "file":
        raise ValueError(f"不是 file URI: {uri}")
    path = url2pathname(unquote(parsed.path))
    netloc = parsed.netloc
    if netloc and netloc.lower() != "localhost":
        if os.name == "nt":
            return f"\\\\{netloc}{path}"
        return f"//{netloc}{path}"
    if os.name == "nt" and _DRIVE_PREFIX.match(path):
        path = path[1:]
    return path


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def extension_of(name: str) -> str:
    suffix = PurePosixPath(str(name or "").replace("\\", "/")).suffix
    return suffix[1:].lower() if suffix else ""


def url_basename(url: str) -> str:
    try:
        return PurePosixPath(unquote(urlparse(str(url)).path)).name
    except ValueError:
        return ""


def relative_posix(path: Path, root: Path) -> str:
    return (
        Path(path)
        .resolve(strict=False)
        .relative_to(Path(root).resolve(strict=False))
        .as_posix()
    )
