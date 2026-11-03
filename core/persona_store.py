"""人设快照落盘与内容去重。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import PluginConfig
from .paths import sanitize_component
from .textutil import compact_json, format_time
from .writer import write_json_atomic


class PersonaStore:
    """把会话当前人设按内容哈希去重保存在 personas/<人设>/。"""

    def __init__(
        self,
        root: Path,
        config: PluginConfig,
        logger: Any | None = None,
    ) -> None:
        self._root = Path(root)
        self._dir = self._root / "personas"
        self._index_path = self._root / "state" / "persona_index.json"
        self._persona_cfg = config.persona
        self._logger = logger
        self._items: dict[str, dict[str, list[Any]]] = {}
        self._days: dict[str, str] = {}
        self._dirty = False

    @property
    def count(self) -> int:
        return sum(len(entries) for entries in self._items.values())

    def load(self) -> None:
        self._items = {}
        self._days = {}
        if not self._index_path.is_file():
            return
        try:
            payload = json.loads(self._index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        items = payload.get("items")
        if isinstance(items, dict):
            for persona_id, entries in items.items():
                if isinstance(entries, dict):
                    self._items[str(persona_id)] = {
                        str(key): value
                        for key, value in entries.items()
                        if isinstance(value, list) and value
                    }
        days = payload.get("days")
        if isinstance(days, dict):
            self._days = {str(key): str(value) for key, value in days.items()}

    def snapshot(self) -> dict[str, Any]:
        return {"v": 1, "items": self._items, "days": self._days}

    def save(self) -> None:
        if not self._dirty:
            return
        write_json_atomic(self._index_path, self.snapshot())
        self._dirty = False

    async def flush(self) -> None:
        if not self._dirty:
            return
        await asyncio.to_thread(self.save)

    def day_of(self, umo: str) -> str | None:
        return self._days.get(umo)

    def mark_day(self, umo: str, day: str) -> None:
        if self._days.get(umo) != day:
            self._days[umo] = day
            self._dirty = True

    @staticmethod
    def fingerprint(persona: Any) -> str:
        if not isinstance(persona, dict):
            return ""
        payload = {
            "prompt": str(persona.get("prompt") or ""),
            "tools": _normalize(persona.get("tools")),
            "skills": _normalize(persona.get("skills")),
            "begin_dialogs": list(persona.get("begin_dialogs") or []),
            "error": str(persona.get("custom_error_message") or ""),
        }
        return hashlib.sha256(compact_json(payload).encode("utf-8")).hexdigest()

    async def capture(
        self,
        persona_id: str,
        persona: Any,
        now: datetime,
        *,
        source: str = "request",
    ) -> str:
        if not self._persona_cfg.enabled or not isinstance(persona, dict):
            return ""
        fingerprint = self.fingerprint(persona)
        if not fingerprint:
            return ""
        key = str(persona_id or persona.get("name") or "default").strip() or "default"
        index = self._items.setdefault(key, {})
        entry = index.get(fingerprint)
        if entry:
            entry[2] = format_time(now)
            return str(entry[0])

        directory = self._dir / sanitize_component(key, max_bytes=60)
        path = directory / f"{now.date().isoformat()}__{fingerprint[:8]}.md"
        relative = path.relative_to(self._root).as_posix()
        content = self.render(key, persona, fingerprint, now, source)
        await asyncio.to_thread(_write_text, path, content)
        index[fingerprint] = [relative, format_time(now), format_time(now)]
        self._dirty = True
        return relative

    def render(
        self,
        persona_id: str,
        persona: dict[str, Any],
        fingerprint: str,
        now: datetime,
        source: str,
    ) -> str:
        lines = [
            "---",
            f"persona_id: {json.dumps(persona_id, ensure_ascii=False)}",
            f"hash: {fingerprint}",
            f"first_seen: {now.date().isoformat()}",
            f"captured_at: {format_time(now)}",
            f"source: {source}",
            "---",
            "",
            str(persona.get("prompt") or ""),
            "",
            "## tools",
            _bullets(persona.get("tools")),
            "",
            "## skills",
            _bullets(persona.get("skills")),
        ]
        dialogs = persona.get("begin_dialogs") or []
        if dialogs:
            lines.extend(["", "## begin_dialogs"])
            lines.extend(f"- {json.dumps(item, ensure_ascii=False)}" for item in dialogs)
        error = persona.get("custom_error_message")
        if error:
            lines.extend(["", "## custom_error_message", str(error)])
        return "\n".join(lines).rstrip() + "\n"


def _normalize(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        return sorted(str(item) for item in value)
    return [str(value)]


def _bullets(value: Any) -> str:
    if value is None:
        return "- *"
    items = [str(item) for item in value] if isinstance(value, (list, tuple, set)) else [str(value)]
    if not items:
        return "- (none)"
    return "\n".join(f"- {item}" for item in items)


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
