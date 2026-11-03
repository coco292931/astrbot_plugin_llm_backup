"""会话目录、对话元数据与备份文件命名。"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import PluginConfig
from .paths import sanitize_component
from .textutil import format_time
from .writer import write_json_atomic

_META_NAME = "_session.json"


class SessionStore:
    """维护 sessions/<会话>/<日期>/ 布局与会话级元数据。"""

    def __init__(self, root: Path, config: PluginConfig) -> None:
        self._root = Path(root)
        self._config = config
        self._sessions_dir = self._root / "sessions"
        self._meta: dict[str, dict[str, Any]] = {}
        self._dirty: set[str] = set()
        self._counts: dict[Path, int] = {}
        self._parts: dict[tuple[str, str, str], int] = {}
        self._suffix = ".jsonl.gz" if config.storage.compress_jsonl else ".jsonl"

    @staticmethod
    def parse_umo(umo: str) -> tuple[str, str, str]:
        parts = str(umo or "").split(":")
        platform = parts[0] if len(parts) >= 1 and parts[0] else "unknown"
        message_type = parts[1] if len(parts) >= 2 and parts[1] else "unknown"
        session_id = ":".join(parts[2:]) if len(parts) >= 3 else str(umo or "")
        return platform, message_type, session_id

    @staticmethod
    def conversation_short(cid: str | None) -> str:
        text = str(cid or "").strip()
        if not text:
            return "_default"
        return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]

    def session_key(self, umo: str) -> str:
        platform, message_type, session_id = self.parse_umo(umo)
        template = self._config.as_str(
            "session_dir_template",
            "{platform}_{message_type}_{session_id}",
        )
        try:
            raw = template.format(
                platform=platform,
                message_type=message_type,
                session_id=session_id,
            )
        except (KeyError, IndexError, ValueError):
            raw = f"{platform}_{message_type}_{session_id}"
        return sanitize_component(raw, max_bytes=140, fallback="session")

    def session_dir(self, umo: str) -> Path:
        return self._sessions_dir / self.session_key(umo)

    def day_dir(self, umo: str, day: str) -> Path:
        return self.session_dir(umo) / sanitize_component(day, fallback="unknown")

    def meta_path(self, umo: str) -> Path:
        return self.session_dir(umo) / _META_NAME

    def meta(self, umo: str) -> dict[str, Any]:
        cached = self._meta.get(umo)
        if cached is not None:
            return cached
        data: dict[str, Any] = {}
        path = self.meta_path(umo)
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, ValueError):
                data = {}
        platform, message_type, session_id = self.parse_umo(umo)
        data.setdefault("v", 1)
        data.setdefault("umo", umo)
        data.setdefault("platform", platform)
        data.setdefault("msg_type", message_type)
        data.setdefault("session_id", session_id)
        if not isinstance(data.get("conversations"), dict):
            data["conversations"] = {}
        self._meta[umo] = data
        return data

    def set_identity(self, umo: str, *, private: bool, name: str = "") -> None:
        meta = self.meta(umo)
        if meta.get("private") != private:
            meta["private"] = private
            self._dirty.add(umo)
        if name and meta.get("name") != name:
            meta["name"] = name
            self._dirty.add(umo)
        if not meta.get("created"):
            meta["created"] = format_time(datetime.now().astimezone())
            self._dirty.add(umo)

    def register_conversation(
        self,
        umo: str,
        cid: str | None,
        title: str | None,
        persona_id: str | None,
    ) -> str:
        short = self.conversation_short(cid)
        meta = self.meta(umo)
        conversations = meta.setdefault("conversations", {})
        entry = conversations.setdefault(short, {"cid": str(cid or "")})
        changed = False
        if title and entry.get("title") != title:
            entry["title"] = title
            changed = True
        if persona_id and entry.get("persona") != persona_id:
            entry["persona"] = persona_id
            changed = True
        if changed:
            self._dirty.add(umo)
        return short

    def conversation_stem(self, conversation: str, title: str | None) -> str:
        template = self._config.as_str(
            "conversation_file_template",
            "{conv_short}",
        )
        try:
            raw = template.format(
                conv_short=conversation or "_default",
                conv_title=title or "",
            )
        except (KeyError, IndexError, ValueError):
            raw = f"{conversation or '_default'}_{title or ''}"
        stem = sanitize_component(raw, max_bytes=100, fallback=conversation or "conversation")
        return stem.rstrip("_") or (conversation or "conversation")

    def record_path(
        self,
        umo: str,
        day: str,
        conversation: str,
        title: str | None,
        max_turns: int = 0,
    ) -> Path:
        directory = self.day_dir(umo, day)
        stem = self.conversation_stem(conversation, title)
        key = (umo, day, stem)
        part = self._parts.get(key, 0)
        while True:
            name = stem if part == 0 else f"{stem}_{part + 1}"
            path = directory / f"{name}{self._suffix}"
            count = self._counts.get(path)
            if count is None:
                count = self._scan(path)
                self._counts[path] = count
            if max_turns <= 0 or count < max_turns:
                self._parts[key] = part
                return path
            part += 1

    def mark_appended(self, path: Path) -> None:
        self._counts[path] = self._counts.get(path, 0) + 1

    def day_file_count(self, umo: str, day: str) -> int:
        directory = self.day_dir(umo, day)
        if not directory.is_dir():
            return 0
        return sum(
            self._scan(path) for path in directory.glob(f"*{self._suffix}")
        )

    async def flush(self) -> None:
        dirty = [umo for umo in self._dirty if umo in self._meta]
        self._dirty.clear()
        for umo in dirty:
            meta = self._meta[umo]
            try:
                await asyncio.to_thread(write_json_atomic, self.meta_path(umo), meta)
            except OSError:
                self._dirty.add(umo)

    @staticmethod
    def _scan(path: Path) -> int:
        if not path.is_file():
            return 0
        try:
            if path.suffix == ".gz":
                with gzip.open(path, "rt", encoding="utf-8") as handle:
                    return sum(1 for line in handle if line.strip())
            with open(path, encoding="utf-8") as handle:
                return sum(1 for line in handle if line.strip())
        except OSError:
            return 0
