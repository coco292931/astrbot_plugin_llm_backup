"""插件配置读取、详细度解析与路径推导。"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .textutil import truncate_text

DETAIL_PRESETS: dict[str, dict[str, Any]] = {
    "minimal": {
        "user_input": "summary",
        "reasoning": "off",
        "reply": "summary",
        "tool_calls": "off",
        "tool_results": False,
        "system_prompt": "off",
    },
    "standard": {
        "user_input": "full",
        "reasoning": "summary",
        "reply": "full",
        "tool_calls": "summary",
        "tool_results": False,
        "system_prompt": "hash",
    },
    "full": {
        "user_input": "full",
        "reasoning": "full",
        "reply": "full",
        "tool_calls": "full",
        "tool_results": True,
        "system_prompt": "full",
    },
}

_LEVELS = {"off", "summary", "full"}
_PROMPT_LEVELS = {"off", "hash", "full"}
_FULL_LIMITS = {
    "user_input": "max_prompt_chars",
    "reasoning": "max_reasoning_chars",
    "reply": "max_reply_chars",
}
_TRUTHY = {"1", "true", "yes", "on", "enable", "enabled"}
_FALSY = {"0", "false", "no", "off", "disable", "disabled"}


@lru_cache(maxsize=64)
def _compile(pattern: str) -> re.Pattern[str] | None:
    try:
        return re.compile(pattern)
    except re.error:
        return None


@dataclass(frozen=True)
class CaptureSettings:
    user_input: str = "full"
    reasoning: str = "summary"
    reply: str = "full"
    tool_calls: str = "summary"
    tool_results: bool = False
    system_prompt: str = "hash"
    summary_chars: int = 200
    max_prompt_chars: int = 8000
    max_reasoning_chars: int = 4000
    max_reply_chars: int = 8000
    max_tool_args_chars: int = 2000
    max_tool_result_chars: int = 2000
    mask_patterns: tuple[str, ...] = ()

    def level(self, field: str) -> str:
        value = getattr(self, field, "off")
        allowed = _PROMPT_LEVELS if field == "system_prompt" else _LEVELS
        return value if value in allowed else "off"

    def render(self, field: str, text: str | None) -> str:
        if not text:
            return ""
        level = self.level(field)
        if level == "off":
            return ""
        limit = (
            self.summary_chars
            if level == "summary"
            else int(getattr(self, _FULL_LIMITS.get(field, "max_prompt_chars"), 0))
        )
        return self.mask(truncate_text(str(text), limit))

    def mask(self, text: str) -> str:
        if not text or not self.mask_patterns:
            return text
        value = text
        for pattern in self.mask_patterns:
            compiled = _compile(pattern)
            if compiled is not None:
                value = compiled.sub("***", value)
        return value


@dataclass(frozen=True)
class MediaConfig:
    enabled: bool = True
    store_bytes: bool = True
    capture_quoted: bool = False
    max_file_mb: int = 50
    download_timeout: int = 20


@dataclass(frozen=True)
class RotationConfig:
    day_boundary: str = "02:00"
    idle_minutes: int = 120
    mode: str = "both"
    check_interval: int = 60
    precreate: bool = False
    max_turns_per_file: int = 0


@dataclass(frozen=True)
class PersonaConfig:
    enabled: bool = True
    daily: bool = True
    on_change: bool = True
    poll_minutes: int = 30


@dataclass(frozen=True)
class StorageConfig:
    compress_jsonl: bool = False
    retention_days: int = 0


@dataclass(frozen=True)
class PerformanceConfig:
    turn_timeout_seconds: int = 600
    media_wait_seconds: int = 5
    finalize_debounce_seconds: int = 20


class PluginConfig:
    """对 AstrBot 配置字典的安全读取封装。"""

    def __init__(self, raw: Mapping[str, Any] | None = None) -> None:
        self.raw: dict[str, Any] = dict(raw or {})

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.raw
        for key in str(path).split("."):
            if not isinstance(node, Mapping) or key not in node:
                return default
            node = node[key]
        return default if node is None else node

    def as_str(self, path: str, default: str = "") -> str:
        value = self.get(path, default)
        return str(value) if value is not None else default

    def as_int(self, path: str, default: int = 0) -> int:
        value = self.get(path, default)
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return default

    def as_bool(self, path: str, default: bool = False) -> bool:
        value = self.get(path, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in _TRUTHY:
                return True
            if normalized in _FALSY:
                return False
        return default

    def as_list(self, path: str) -> list[str]:
        value = self.get(path, [])
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    def as_dict(self, path: str) -> dict[str, Any]:
        value = self.get(path, {})
        return dict(value) if isinstance(value, Mapping) else {}

    @property
    def enabled(self) -> bool:
        return self.as_bool("enabled", True)

    def capture_settings(self) -> CaptureSettings:
        preset_name = self.as_str("detail_level", "standard").strip().lower()
        preset = DETAIL_PRESETS.get(preset_name, DETAIL_PRESETS["standard"])

        def pick(field: str) -> str:
            value = self.as_str(f"capture.{field}", "follow").strip().lower()
            allowed = _PROMPT_LEVELS if field == "system_prompt" else _LEVELS
            return value if value in allowed else str(preset[field])

        tool_results_raw = self.as_str("capture.tool_results", "follow").strip().lower()
        if tool_results_raw in _TRUTHY:
            tool_results = True
        elif tool_results_raw in _FALSY:
            tool_results = False
        else:
            tool_results = bool(preset["tool_results"])

        return CaptureSettings(
            user_input=pick("user_input"),
            reasoning=pick("reasoning"),
            reply=pick("reply"),
            tool_calls=pick("tool_calls"),
            tool_results=tool_results,
            system_prompt=pick("system_prompt"),
            summary_chars=self.as_int("limits.summary_chars", 200),
            max_prompt_chars=self.as_int("limits.max_prompt_chars", 8000),
            max_reasoning_chars=self.as_int("limits.max_reasoning_chars", 4000),
            max_reply_chars=self.as_int("limits.max_reply_chars", 8000),
            max_tool_args_chars=self.as_int("limits.max_tool_args_chars", 2000),
            max_tool_result_chars=self.as_int("limits.max_tool_result_chars", 2000),
            mask_patterns=tuple(self.as_list("privacy.mask_patterns")),
        )

    @property
    def media(self) -> MediaConfig:
        return MediaConfig(
            enabled=self.as_bool("media.enabled", True),
            store_bytes=self.as_bool("media.store_bytes", True),
            capture_quoted=self.as_bool("media.capture_quoted", False),
            max_file_mb=max(0, self.as_int("media.max_file_mb", 50)),
            download_timeout=max(1, self.as_int("media.download_timeout_seconds", 20)),
        )

    @property
    def rotation(self) -> RotationConfig:
        mode = self.as_str("rotation.mode", "both").strip().lower()
        if mode not in {"both", "lazy", "scheduled"}:
            mode = "both"
        return RotationConfig(
            day_boundary=self.as_str("rotation.day_boundary", "02:00"),
            idle_minutes=max(0, self.as_int("rotation.idle_minutes", 120)),
            mode=mode,
            check_interval=max(5, self.as_int("rotation.check_interval_seconds", 60)),
            precreate=self.as_bool("rotation.precreate_next_day_folder", False),
            max_turns_per_file=max(0, self.as_int("rotation.max_turns_per_file", 0)),
        )

    @property
    def persona(self) -> PersonaConfig:
        return PersonaConfig(
            enabled=self.as_bool("persona.enabled", True),
            daily=self.as_bool("persona.daily", True),
            on_change=self.as_bool("persona.on_change", True),
            poll_minutes=max(0, self.as_int("persona.poll_minutes", 30)),
        )

    @property
    def storage(self) -> StorageConfig:
        return StorageConfig(
            compress_jsonl=self.as_bool("storage.compress_jsonl", False),
            retention_days=max(0, self.as_int("storage.retention_days", 0)),
        )

    @property
    def performance(self) -> PerformanceConfig:
        return PerformanceConfig(
            turn_timeout_seconds=max(60, self.as_int("performance.turn_timeout_seconds", 600)),
            media_wait_seconds=max(0, self.as_int("performance.media_wait_seconds", 5)),
            finalize_debounce_seconds=max(
                0, self.as_int("performance.finalize_debounce_seconds", 20)
            ),
        )

    def resolve_root(self, data_dir: Path) -> Path:
        raw = self.as_str("backup_root", "").strip()
        if not raw:
            return Path(data_dir) / "backup"
        expanded = os.path.expandvars(os.path.expanduser(raw))
        path = Path(expanded)
        if not path.is_absolute():
            path = Path(data_dir) / path
        return path.resolve(strict=False)
