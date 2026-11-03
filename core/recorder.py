"""轮次状态与备份记录构建。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import CaptureSettings
from .textutil import compact_json, format_time, split_message_content, truncate_text


@dataclass
class ToolCallRecord:
    name: str
    args: Any = None
    result: str = ""
    ok: bool = True
    started: float = 0.0
    duration_ms: int = 0
    media: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False


@dataclass
class TurnState:
    """一次 LLM 请求对应的一轮往来流水。"""

    key: str
    umo: str
    day: str
    started_at: datetime
    private: bool = True
    title: str = ""
    conversation: str = ""
    source: str = "user"
    sender_id: str = ""
    sender_name: str = ""
    group_id: str = ""
    user_text: str = ""
    prompt: str = ""
    input_media: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    context_count: int = 0
    tools: list[str] = field(default_factory=list)
    system_prompt_hash: str = ""
    system_prompt_text: str | None = None
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    reasoning: str | None = None
    reply: str | None = None
    output_media_refs: list[dict[str, Any]] = field(default_factory=list)
    usage_in: int = 0
    usage_out: int = 0
    baseline: int | None = None
    baseline_sig: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    status: str = "ok"
    note: str = ""
    finished: bool = False
    last_seen_at: datetime | None = None
    media_tasks: list[Any] = field(default_factory=list)


def message_signature(message: Any) -> str:
    role = str(getattr(message, "role", ""))
    content = getattr(message, "content", None)
    if isinstance(content, str):
        size = len(content)
    elif isinstance(content, (list, tuple)):
        size = len(content)
    else:
        size = -1
    return f"{role}:{size}"


def locate_baseline(
    messages: list[Any],
    baseline: int | None,
    signature: str,
    user_text: str,
) -> int:
    """定位本轮增量起点；失败返回 -1。"""
    if baseline is not None and 0 <= baseline <= len(messages):
        if baseline == 0 or message_signature(messages[baseline - 1]) == signature:
            return baseline

    probe = str(user_text or "").strip()[:64]
    if not probe:
        return -1
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if str(getattr(message, "role", "")) != "user":
            continue
        _, text = split_message_content(message)
        if text.strip()[:64] == probe:
            return index + 1
    return -1


def media_paths(refs: list[dict[str, Any]] | None) -> list[str]:
    paths = []
    for ref in refs or []:
        if isinstance(ref, dict):
            value = ref.get("p")
            if isinstance(value, str) and value:
                paths.append(value)
    return paths


def media_errors(refs: list[dict[str, Any]] | None) -> int:
    return sum(1 for ref in refs or [] if isinstance(ref, dict) and ref.get("e"))


def render_tool(record: ToolCallRecord, capture: CaptureSettings) -> dict[str, Any]:
    item: dict[str, Any] = {"n": record.name}
    if record.args:
        item["a"] = _shrink(record.args, capture.max_tool_args_chars)
    if capture.tool_results and record.result:
        item["r"] = truncate_text(record.result, capture.max_tool_result_chars)
    if not record.ok:
        item["ok"] = False
    if record.duration_ms:
        item["ms"] = record.duration_ms
    paths = media_paths(record.media)
    if paths:
        item["m"] = paths
    return item


def collect_steps(
    messages: list[Any],
    start: int,
    tool_calls: list[ToolCallRecord],
    capture: CaptureSettings,
) -> tuple[list[dict[str, Any]], bool]:
    """从 Agent 上下文增量中还原中间思考与工具调用。"""
    steps: list[dict[str, Any]] = []
    if start < 0 or start > len(messages):
        return steps, True

    include_tools = capture.level("tool_calls") != "off"
    cursor = 0
    degraded = False

    for message in messages[start:]:
        if str(getattr(message, "role", "")) != "assistant":
            continue
        calls = getattr(message, "tool_calls", None) or []
        if not calls:
            continue

        reasoning, text = split_message_content(message)
        step: dict[str, Any] = {}
        rendered_reasoning = capture.render("reasoning", reasoning)
        if rendered_reasoning:
            step["r"] = rendered_reasoning
        rendered_text = capture.render("reply", text)
        if rendered_text:
            step["t"] = rendered_text

        rendered_tools = []
        if include_tools:
            for _ in calls:
                if cursor < len(tool_calls):
                    rendered_tools.append(render_tool(tool_calls[cursor], capture))
                    cursor += 1
                else:
                    degraded = True
        if rendered_tools:
            step["tools"] = rendered_tools
        if step:
            steps.append(step)

    if include_tools and cursor < len(tool_calls):
        steps.append(
            {"tools": [render_tool(record, capture) for record in tool_calls[cursor:]]}
        )
        degraded = True
    return steps, degraded


def build_turn_record(
    state: TurnState,
    capture: CaptureSettings,
    ended_at: datetime | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {"ts": format_time(state.started_at)}
    if ended_at is not None:
        record["end"] = format_time(ended_at)

    who: dict[str, Any] = {}
    if state.sender_id:
        who["id"] = state.sender_id
    if not state.private:
        if state.group_id:
            record["gid"] = state.group_id
        if state.sender_name:
            who["n"] = state.sender_name
    if who:
        record["who"] = who
    if state.conversation:
        record["conv"] = state.conversation
    if state.source != "user":
        record["src"] = state.source

    inbound: dict[str, Any] = {}
    text = capture.render("user_input", state.user_text)
    if text:
        inbound["t"] = text
    if state.prompt and state.prompt != state.user_text:
        prompt = capture.render("user_input", state.prompt)
        if prompt:
            inbound["p"] = prompt
    paths = media_paths(state.input_media)
    if paths:
        inbound["m"] = paths
    if inbound:
        record["in"] = inbound

    request: dict[str, Any] = {}
    if state.model:
        request["model"] = state.model
    if state.context_count:
        request["ctx"] = state.context_count
    if state.tools:
        if capture.level("tool_calls") == "full":
            request["tools"] = list(state.tools)
        else:
            request["tools"] = len(state.tools)
    system_prompt_level = capture.level("system_prompt")
    if state.system_prompt_hash and system_prompt_level != "off":
        if system_prompt_level == "full" and state.system_prompt_text:
            request["sp"] = {
                "h": state.system_prompt_hash,
                "t": truncate_text(state.system_prompt_text, capture.max_prompt_chars),
            }
        else:
            request["sp"] = state.system_prompt_hash
    if request:
        record["req"] = request

    if state.steps:
        record["steps"] = state.steps

    out: dict[str, Any] = {}
    reasoning = capture.render("reasoning", state.reasoning)
    if reasoning:
        out["r"] = reasoning
    reply = capture.render("reply", state.reply)
    if reply:
        out["t"] = reply
    output_paths = media_paths(state.output_media_refs)
    if output_paths:
        out["m"] = output_paths
    if out:
        record["out"] = out

    if state.usage_in or state.usage_out:
        record["u"] = {"i": state.usage_in, "o": state.usage_out}
    if state.status != "ok":
        record["st"] = state.status

    note = state.note
    errors = media_errors(state.input_media) + media_errors(state.output_media_refs)
    if errors:
        note = f"{note};media_error={errors}" if note else f"media_error={errors}"
    if note:
        record["note"] = note
    return record


def _shrink(value: Any, limit: int) -> Any:
    dumped = compact_json(value)
    if limit <= 0 or len(dumped) <= limit:
        return value
    return f"{dumped[:limit]}…"
