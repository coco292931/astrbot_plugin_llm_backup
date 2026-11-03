"""与 AstrBot 版本解耦的文本与消息组件工具。

消息组件一律按鸭子类型读取，避免不同 AstrBot 版本之间的类定义差异。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from urllib.parse import urlparse

from .paths import extension_of, file_uri_to_path, is_file_uri

_MEDIA_KINDS = {
    "Image": "image",
    "Record": "audio",
    "Video": "video",
    "File": "file",
}

_PLAIN_KINDS = {"Plain", "Text"}


def component_kind(component: Any) -> str:
    raw = getattr(component, "type", None)
    value = getattr(raw, "value", raw)
    if isinstance(value, str) and value:
        return value
    return type(component).__name__


def media_kind(component: Any) -> str | None:
    return _MEDIA_KINDS.get(component_kind(component))


def media_source(component: Any) -> str:
    """按可用性挑选媒体来源，跳过不存在或已失效的本地路径。"""
    kind = component_kind(component)
    candidates: list[Any] = []
    if kind == "File":
        candidates.append(getattr(component, "file_", None))
    else:
        candidates.append(getattr(component, "path", None))
        candidates.append(getattr(component, "file", None))
    candidates.append(getattr(component, "url", None))

    values = [
        value.strip()
        for value in candidates
        if isinstance(value, str) and value.strip()
    ]
    remote: list[str] = []
    for value in values:
        if value.startswith(("http://", "https://", "base64://", "data:")):
            remote.append(value)
            continue
        try:
            local = file_uri_to_path(value) if is_file_uri(value) else value
            if Path(local).is_file():
                return value
        except (OSError, ValueError):
            continue
    if remote:
        return remote[0]
    return values[0] if values else ""


def media_display_name(component: Any, fallback: str) -> str:
    if component_kind(component) == "File":
        name = getattr(component, "name", None)
        if isinstance(name, str) and name.strip():
            return name.strip()

    source = media_source(component)
    if source.startswith(("http://", "https://")):
        try:
            base = Path(unquote(urlparse(source).path)).name
        except ValueError:
            base = ""
        if base:
            return base
    if source and not source.startswith(("base64://", "data:")):
        try:
            local = file_uri_to_path(source) if is_file_uri(source) else source
            base = Path(str(local).replace("\\", "/")).name
        except (OSError, ValueError):
            base = ""
        if base:
            return base
    return fallback


def chain_plain_text(chain: Any) -> str:
    if chain is None:
        return ""
    items = getattr(chain, "chain", chain)
    if isinstance(items, str):
        return items
    if not isinstance(items, (list, tuple)):
        return ""
    parts = []
    for item in items:
        if component_kind(item) in _PLAIN_KINDS:
            text = getattr(item, "text", None)
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def split_message_content(message: Any) -> tuple[str, str]:
    """拆分模型消息里的思考内容与正文。"""
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return "", content
    reasoning: list[str] = []
    text: list[str] = []
    for part in content or []:
        raw = getattr(part, "type", None)
        kind = getattr(raw, "value", raw) or type(part).__name__
        if kind == "think":
            value = getattr(part, "think", "")
            if isinstance(value, str):
                reasoning.append(value)
        elif kind == "text":
            value = getattr(part, "text", "")
            if isinstance(value, str):
                text.append(value)
    return "".join(reasoning), "".join(text)


def tool_result_text(result: Any) -> str:
    if result is None:
        return ""
    content = getattr(result, "content", None)
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for item in content or []:
        text = getattr(item, "text", None)
        parts.append(text if isinstance(text, str) else f"[{type(item).__name__}]")
    return "\n".join(parts)


def truncate_text(text: str | None, limit: int) -> str:
    if not text:
        return ""
    value = str(text)
    if limit <= 0 or len(value) <= limit:
        return value
    return f"{value[:limit]}…"


def compact_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def json_safe(value: Any, depth: int = 0) -> Any:
    """把工具参数等对象转换成可 JSON 序列化的结构。"""
    if depth > 8:
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, Mapping):
        return {str(key): json_safe(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item, depth + 1) for item in value]
    dumper = getattr(value, "model_dump", None)
    if callable(dumper):
        try:
            return json_safe(dumper(), depth + 1)
        except Exception:
            pass
    return str(value)


def format_time(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat(timespec="seconds")


def brief_source(source: str, limit: int = 160) -> str:
    text = str(source or "")
    if text.startswith("base64://"):
        return f"base64://<{len(text) - 9} chars>"
    if text.startswith("data:"):
        return f"data:<{len(text)} chars>"
    return truncate_text(text, limit)


def guess_extension_from_url(url: str) -> str:
    return extension_of(unquote(urlparse(str(url)).path))
