"""备份服务：钩子编排、媒体与人设调度、后台任务。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from .config import PluginConfig
from .media_store import MediaStore
from .paths import sha256_text, url_basename
from .persona_store import PersonaStore
from .recorder import (
    ToolCallRecord,
    TurnState,
    build_turn_record,
    collect_steps,
    locate_baseline,
    message_signature,
)
from .rotation import RotationManager, target_day
from .session_store import SessionStore
from .textutil import (
    chain_plain_text,
    component_kind,
    format_time,
    json_safe,
    media_display_name,
    media_kind,
    media_source,
    tool_result_text,
)
from .writer import Writer, write_json_atomic

_TOOL_MEDIA_KINDS = {
    "image": "image",
    "record": "audio",
    "video": "video",
    "file": "file",
}


def _call(target: Any, name: str, default: Any = None, *args: Any) -> Any:
    method = getattr(target, name, None)
    if not callable(method):
        return default
    try:
        return method(*args)
    except Exception:
        return default


def event_umo(event: Any) -> str:
    value = getattr(event, "unified_msg_origin", "")
    if value:
        return str(value)
    session = getattr(event, "session", None)
    return str(session) if session is not None else ""


def event_platform(event: Any) -> str:
    value = _call(event, "get_platform_name", "")
    if value:
        return str(value)
    meta = getattr(event, "platform_meta", None)
    return str(getattr(meta, "name", "") or "")


def event_is_private(event: Any) -> bool:
    value = _call(event, "is_private_chat", None)
    if isinstance(value, bool):
        return value
    message_obj = getattr(event, "message_obj", None)
    raw = getattr(message_obj, "type", "")
    kind = getattr(raw, "value", raw)
    return "friend" in str(kind).lower()


def event_text(event: Any) -> str:
    value = _call(event, "get_message_str", "")
    if value:
        return str(value)
    return str(getattr(event, "message_str", "") or "")


def event_sender_id(event: Any) -> str:
    value = _call(event, "get_sender_id", "")
    if value:
        return str(value)
    sender = getattr(getattr(event, "message_obj", None), "sender", None)
    return str(getattr(sender, "user_id", "") or "")


def event_sender_name(event: Any) -> str:
    value = _call(event, "get_sender_name", "")
    return str(value or "")


def event_group_id(event: Any) -> str:
    value = _call(event, "get_group_id", "")
    return str(value or "")


def event_components(event: Any) -> list[Any]:
    value = _call(event, "get_messages", None)
    if isinstance(value, list):
        return value
    message_obj = getattr(event, "message_obj", None)
    value = getattr(message_obj, "message", None)
    return list(value) if isinstance(value, (list, tuple)) else []


class BackupService:
    """把 AstrBot 钩子事件转换成按会话落盘的结构化记录。"""

    def __init__(
        self,
        config: PluginConfig,
        data_dir: Path,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config = config
        self._logger = logger or logging.getLogger("astrbot")
        self._root = config.resolve_root(Path(data_dir))
        self._capture = config.capture_settings()
        self._rotation_cfg = config.rotation
        self._persona_cfg = config.persona
        self._storage_cfg = config.storage
        self._performance = config.performance
        self._writer = Writer(self._root, self._storage_cfg, self._logger)
        self._sessions = SessionStore(self._root, config)
        self._rotation = RotationManager(self._root, self._rotation_cfg)
        self._media = MediaStore(self._root, config, self._logger)
        self._personas = PersonaStore(self._root, config, self._logger)
        self._active: dict[str, TurnState] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._timers: dict[str, asyncio.Task[None]] = {}
        self._decisions: deque[dict[str, Any]] = deque(maxlen=40)
        self._warned: set[tuple[str, str]] = set()
        self._persona_resolver: Callable[[Any, str], Awaitable[tuple[str, Any]]] | None = None
        self._tz: Any = None
        self._started = False
        self._stats = {"turns": 0, "errors": 0}
        self._last_error = ""
        self._env_version = ""
        self._missing_hooks: list[str] = []
        self._include_cron = config.as_bool("include_cron", False)
        self._include_webchat = config.as_bool("include_webchat", True)
        self._include_private = config.as_bool("include_private", True)
        self._include_groups = config.as_bool("include_groups", False)
        self._include_plugin = config.as_bool("include_plugin_initiated", False)
        self._filter_mode = config.as_str("filter_mode", "whitelist").strip().lower()
        self._filter_umos = set(config.as_list("umos"))
        self._filter_senders = set(config.as_list("senders"))
        mode = self._rotation_cfg.mode
        self._allow_lazy = mode in {"both", "lazy"}
        self._allow_scheduled = mode in {"both", "scheduled"}

    @property
    def root(self) -> Path:
        return self._root

    def set_persona_resolver(
        self,
        resolver: Callable[[Any, str], Awaitable[tuple[str, Any]]],
    ) -> None:
        self._persona_resolver = resolver

    def set_environment(
        self,
        *,
        version: str = "",
        missing_hooks: list[str] | None = None,
    ) -> None:
        self._env_version = str(version or "")
        self._missing_hooks = list(missing_hooks or [])
        if self._missing_hooks:
            self._logger.warning(
                "[llm_backup] 当前 AstrBot 缺少钩子: %s，将使用回复后兜底落盘",
                ", ".join(self._missing_hooks),
            )

    def set_timezone(self, name: str) -> None:
        if not name:
            return
        try:
            from zoneinfo import ZoneInfo

            self._tz = ZoneInfo(str(name))
        except Exception:
            self._tz = None

    async def start(self) -> None:
        if self._started:
            return
        await asyncio.to_thread(self._root.mkdir, parents=True, exist_ok=True)
        self._writer.start()
        await asyncio.to_thread(self._media.load)
        await asyncio.to_thread(self._personas.load)
        await asyncio.to_thread(self._rotation.load)
        await self._recover_pending()
        await self._cleanup_partials()
        self._spawn("rotation", self._rotation_cfg.check_interval, self._tick_rotation)
        if self._persona_cfg.enabled and self._persona_cfg.poll_minutes > 0:
            self._spawn("persona", self._persona_cfg.poll_minutes * 60, self._tick_persona)
        self._spawn("sweep", 30, self._tick_sweep)
        if self._storage_cfg.retention_days > 0:
            self._spawn("retention", 6 * 3600, self._tick_retention)
        self._started = True
        self._logger.info("[llm_backup] 备份目录: %s", self._root)

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for task in list(self._timers.values()):
            task.cancel()
        if self._timers:
            await asyncio.gather(*self._timers.values(), return_exceptions=True)
        self._timers.clear()
        for state in list(self._active.values()):
            await self._finalize(state, status="interrupted", note="plugin stopped")
        await asyncio.to_thread(self._rotation.save)
        await self._media.flush()
        await self._personas.flush()
        await self._sessions.flush()
        await self._writer.stop()

    async def flush(self) -> None:
        for state in list(self._active.values()):
            await self._finalize(state, status="manual")
        await self._media.flush()
        await self._personas.flush()
        await self._sessions.flush()
        await asyncio.to_thread(self._rotation.save)
        await self._writer.flush()

    async def handle_llm_request(self, event: Any, request: Any) -> None:
        if not self._started:
            self._note("llm_request", event, False, "service_not_started")
            return
        umo = event_umo(event)
        allowed, reason = self._evaluate(event, umo)
        if not allowed:
            self._note("llm_request", event, False, reason)
            return
        key = self._base_key(event, umo)
        previous = self._active.pop(key, None)
        if previous is not None and self._is_empty_turn(previous):
            previous.finished = True
            self._cancel_timer(previous)
            await self._await_media(previous)
        elif previous is not None:
            await self._finalize(previous, status="superseded")
        self._active[key] = await self._open_turn(event, umo, request, key)
        self._note("llm_request", event, True, "opened")

    @staticmethod
    def _is_empty_turn(state: TurnState) -> bool:
        return (
            not state.tool_calls
            and not state.steps
            and state.reply is None
            and state.baseline is None
        )

    async def handle_agent_begin(self, event: Any, run_context: Any) -> None:
        state, _ = self._state_or_note("agent_begin", event)
        if state is None:
            return
        self._touch_state(state)
        messages = list(getattr(run_context, "messages", None) or [])
        state.baseline = len(messages)
        state.baseline_sig = message_signature(messages[-1]) if messages else ""

    async def handle_tool_start(
        self,
        event: Any,
        tool: Any,
        tool_args: Any,
    ) -> None:
        state, _ = self._state_or_note("using_llm_tool", event)
        if state is None or self._capture.level("tool_calls") == "off":
            return
        self._touch_state(state)
        state.tool_calls.append(
            ToolCallRecord(
                name=str(getattr(tool, "name", "") or "unknown"),
                args=json_safe(tool_args) if tool_args else None,
                started=time.time(),
            )
        )

    async def handle_tool_end(
        self,
        event: Any,
        tool: Any,
        tool_args: Any,
        tool_result: Any,
    ) -> None:
        state, _ = self._state_or_note("llm_tool_respond", event)
        if state is None or self._capture.level("tool_calls") == "off":
            return
        self._touch_state(state)
        name = str(getattr(tool, "name", "") or "unknown")
        record = self._pending_tool(state, name)
        if record is None:
            record = ToolCallRecord(
                name=name,
                args=json_safe(tool_args) if tool_args else None,
                started=time.time(),
            )
            state.tool_calls.append(record)
        record.done = True
        record.duration_ms = int(max(0.0, time.time() - record.started) * 1000)
        record.ok = not bool(getattr(tool_result, "isError", False))
        if self._capture.tool_results:
            record.result = self._capture.mask(tool_result_text(tool_result))
        for source, media_name, kind in self._tool_media_sources(name, record.args):
            state.media_tasks.append(
                asyncio.create_task(
                    self._store_tool_media(state, record, source, media_name, kind)
                )
            )

    async def handle_llm_response(self, event: Any, response: Any) -> None:
        state, reason = self._state_or_note("llm_response", event)
        if state is None:
            return
        self._touch_state(state)
        reasoning = getattr(response, "reasoning_content", None)
        if reasoning:
            state.reasoning = str(reasoning)
        text = str(getattr(response, "completion_text", "") or "")
        if not text:
            text = chain_plain_text(getattr(response, "result_chain", None))
        if text:
            state.reply = text
        usage = getattr(response, "usage", None)
        if usage is not None:
            state.usage_in = int(getattr(usage, "input", 0) or 0)
            state.usage_out = int(getattr(usage, "output", 0) or 0)
        if str(getattr(response, "role", "")) == "err":
            state.status = "err"
            state.note = text or "llm error"
        self._schedule_finalize(state)

    async def handle_agent_done(
        self,
        event: Any,
        run_context: Any,
        response: Any = None,
    ) -> None:
        state, _ = self._state_or_note("agent_done", event)
        if state is None:
            return
        if state.reply is None and response is not None:
            text = str(getattr(response, "completion_text", "") or "")
            if text:
                state.reply = text
        await self._await_media(state)
        messages = list(getattr(run_context, "messages", None) or [])
        if state.baseline is not None or messages:
            start = locate_baseline(
                messages,
                state.baseline,
                state.baseline_sig,
                state.user_text,
            )
            steps, degraded = collect_steps(
                messages,
                start,
                state.tool_calls,
                self._capture,
            )
            if steps:
                state.steps = steps
            if degraded or start < 0:
                if state.status == "ok":
                    state.status = "degraded"
                if start < 0 and not state.note:
                    state.note = "steps_unavailable"
        await self._finalize(state)

    def status(self, umo: str | None = None) -> dict[str, Any]:
        day = self._rotation.day_of(umo) if umo else None
        return {
            "root": str(self._root),
            "started": self._started,
            "day": day,
            "active": len(self._active),
            "turns": self._stats["turns"],
            "media": self._media.count,
            "personas": self._personas.count,
            "queue": self._writer.pending,
            "turns_today": (
                self._sessions.day_file_count(umo, day) if umo and day else 0
            ),
            "last_error": self._last_error,
        }

    def status_text(self, umo: str | None = None) -> str:
        data = self.status(umo)
        lines = [
            f"备份目录: {data['root']}",
            f"运行状态: {'运行中' if data['started'] else '已停止'}",
            f"当前逻辑日: {data['day'] or '未建立'}",
            f"活动轮次: {data['active']} / 写队列: {data['queue']}",
            f"累计轮次: {data['turns']}",
            f"媒体文件: {data['media']} / 人设快照: {data['personas']}",
        ]
        if umo:
            lines.append(f"本会话今日记录: {data['turns_today']}")
        if data["last_error"]:
            lines.append(f"最近错误: {data['last_error']}")
        lines.append("排查: /backup_debug")
        return "\n".join(lines)

    def debug_text(self, event: Any = None) -> str:
        lines = [
            f"备份目录: {self._root}",
            f"服务状态: {'运行中' if self._started else '未启动'}",
            f"AstrBot: {self._env_version or '未知版本'}",
            f"缺失钩子: {', '.join(self._missing_hooks) or '无'}",
            f"过滤模式: {self._filter_mode} / 白名单 {len(self._filter_umos)} 条 / 用户 {len(self._filter_senders)} 条",
            f"开关: 私聊 {self._include_private} 群聊 {self._include_groups} "
            f"cron {self._include_cron} 插件发起 {self._include_plugin}",
            f"活动轮次: {len(self._active)} / 写队列: {self._writer.pending}",
        ]
        if event is not None:
            umo = event_umo(event)
            allowed, reason = self._evaluate(event, umo)
            verdict = "记录" if allowed else f"跳过（{reason}）"
            lines.extend(
                [
                    f"当前会话: {umo or '空'}",
                    f"平台: {event_platform(event) or '未知'} / 私聊: {event_is_private(event)}",
                    f"当前判定: {verdict}",
                ]
            )
        if self._decisions:
            lines.append("最近判定:")
            for item in list(self._decisions)[-8:]:
                flag = "OK" if item["ok"] else "SKIP"
                detail = item.get("why") or ""
                lines.append(
                    f"  {item['t']} {item['stage']:<14} {flag:<4} {item['umo']} {detail}"
                )
        else:
            lines.append("最近判定: 尚未收到任何 LLM 钩子")
        return "\n".join(lines)

    async def force_rotate(self, umo: str) -> str:
        now = self._now()
        current = self._rotation.day_of(umo)
        next_day = None
        if current:
            try:
                next_day = date.fromisoformat(current) + timedelta(days=1)
            except ValueError:
                next_day = None
        if next_day is None:
            next_day = target_day(now, self._rotation.boundary)
        self._rotation.force(umo, next_day.isoformat())
        await asyncio.to_thread(self._rotation.save)
        return next_day.isoformat()

    async def doctor(self) -> str:
        problems: list[str] = []
        try:
            await asyncio.to_thread(self._root.mkdir, parents=True, exist_ok=True)
            probe = self._root / ".write_probe"
            await asyncio.to_thread(probe.write_text, "ok", encoding="utf-8")
            await asyncio.to_thread(probe.unlink)
        except OSError as exc:
            problems.append(f"备份目录不可写: {exc}")
        free_mb = -1
        try:
            free_mb = shutil.disk_usage(self._root).free // (1024 * 1024)
            if free_mb < 512:
                problems.append(f"剩余空间偏低: {free_mb} MB")
        except OSError:
            pass
        if not self._include_private and not self._include_groups:
            problems.append("私聊与群聊均已关闭，不会记录任何会话")
        if self._filter_mode == "whitelist" and self._filter_umos:
            problems.append(f"当前为白名单模式，仅记录 {len(self._filter_umos)} 个会话")
        lines = [
            f"备份目录: {self._root}",
            f"AstrBot: {self._env_version or '未知版本'}",
            f"缺失钩子: {', '.join(self._missing_hooks) or '无'}",
            f"剩余空间: {'未知' if free_mb < 0 else f'{free_mb} MB'}",
            f"换日点: {self._rotation_cfg.day_boundary} / 空闲阈值: {self._rotation_cfg.idle_minutes} 分钟",
            f"详细度: {self._config.as_str('detail_level', 'standard')}",
            f"媒体: {'开启' if self._config.media.enabled else '关闭'} / 上限 {self._config.media.max_file_mb} MB",
            f"人设: {'开启' if self._persona_cfg.enabled else '关闭'}",
            f"检查结果: {'正常' if not problems else '；'.join(problems)}",
        ]
        return "\n".join(lines)

    async def _open_turn(
        self,
        event: Any,
        umo: str,
        request: Any,
        key: str,
    ) -> TurnState:
        now = self._now()
        day, _ = self._rotation.resolve(umo, now, allow_advance=self._allow_lazy)
        self._rotation.touch(umo, now)
        private = event_is_private(event)
        state = TurnState(key=key, umo=umo, day=day, started_at=now, private=private)
        state.last_seen_at = now
        state.source = self._origin(event)
        state.sender_id = event_sender_id(event)
        state.sender_name = event_sender_name(event)
        state.group_id = event_group_id(event)
        state.user_text = event_text(event)
        state.prompt = str(getattr(request, "prompt", "") or "")

        conversation = getattr(request, "conversation", None)
        cid = str(getattr(conversation, "cid", "") or "")
        title = str(getattr(conversation, "title", "") or "")
        persona_id = str(_call(event, "get_extra", "", "_trace_persona_id") or "")
        state.title = title
        state.conversation = self._sessions.register_conversation(
            umo, cid, title, persona_id
        )
        self._sessions.set_identity(
            umo,
            private=private,
            name=state.sender_name if private else "",
        )
        state.model = str(getattr(request, "model", "") or "")
        state.context_count = len(list(getattr(request, "contexts", None) or []))
        state.tools = self._tool_names(request)
        system_prompt = str(getattr(request, "system_prompt", "") or "")
        if system_prompt:
            state.system_prompt_hash = sha256_text(system_prompt)[:16]
            if self._capture.level("system_prompt") == "full":
                state.system_prompt_text = system_prompt
        state.media_tasks.append(
            asyncio.create_task(self._collect_input_media(state, event))
        )
        await self._capture_persona(umo, event, now, day)
        await self._write_pending(state)
        return state

    def _state_of(self, event: Any) -> TurnState | None:
        umo = event_umo(event)
        if not umo:
            return None
        return self._active.get(self._base_key(event, umo))

    def _state_or_note(
        self,
        stage: str,
        event: Any,
    ) -> tuple[TurnState | None, str]:
        state = self._state_of(event)
        if state is None:
            self._note(stage, event, False, "no_active_turn")
            return None, "no_active_turn"
        return state, ""

    def _note(self, stage: str, event: Any, ok: bool, reason: str = "") -> None:
        umo = event_umo(event) if event is not None else ""
        if not ok:
            key = (stage, reason)
            if key not in self._warned:
                self._warned.add(key)
                self._logger.info(
                    "[llm_backup] %s 跳过: %s (%s)，后续同类提示不再重复",
                    stage,
                    reason,
                    umo or "空",
                )
        self._decisions.append(
            {
                "t": self._now().strftime("%H:%M:%S"),
                "stage": stage,
                "umo": umo,
                "ok": ok,
                "why": reason,
            }
        )

    def _umo_matches(self, umo: str) -> bool:
        for pattern in self._filter_umos:
            if pattern == umo:
                return True
            if "*" in pattern and fnmatchcase(umo, pattern):
                return True
        return False

    def _evaluate(self, event: Any, umo: str) -> tuple[bool, str]:
        if not self._config.enabled:
            return False, "plugin_disabled"
        if not self._started:
            return False, "service_not_started"
        if not umo:
            return False, "empty_umo"
        platform = event_platform(event)
        if platform == "cron" and not self._include_cron:
            return False, "cron_filtered"
        if platform == "webchat" and not self._include_webchat:
            return False, "webchat_filtered"
        private = event_is_private(event)
        if private and not self._include_private:
            return False, "private_disabled"
        if not private and not self._include_groups:
            return False, "group_disabled"
        if (
            _call(event, "get_extra", None, "provider_request") is not None
            and not self._include_plugin
        ):
            return False, "plugin_initiated_filtered"
        if self._filter_mode == "whitelist" and self._filter_umos:
            if not self._umo_matches(umo):
                return False, "umo_not_in_whitelist"
        if self._filter_mode == "blacklist" and self._umo_matches(umo):
            return False, "umo_in_blacklist"
        if self._filter_senders and event_sender_id(event) not in self._filter_senders:
            return False, "sender_not_in_list"
        return True, ""

    def _schedule_finalize(self, state: TurnState) -> None:
        delay = self._performance.finalize_debounce_seconds
        if delay <= 0 or state.finished:
            return
        self._cancel_timer(state)
        self._timers[state.key] = asyncio.create_task(
            self._delayed_finalize(state, delay)
        )

    def _cancel_timer(self, state: TurnState) -> None:
        task = self._timers.pop(state.key, None)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    async def _delayed_finalize(self, state: TurnState, delay: int) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if state.finished:
            return
        await self._finalize(state, status="fallback", note="agent_done_missing")

    async def _finalize(
        self,
        state: TurnState,
        status: str = "ok",
        note: str = "",
    ) -> None:
        if state.finished:
            return
        state.finished = True
        self._cancel_timer(state)
        if status != "ok":
            state.status = status
        if note:
            state.note = note
        await self._await_media(state)
        record = build_turn_record(state, self._capture, ended_at=self._now())
        path = self._sessions.record_path(
            state.umo,
            state.day,
            state.conversation,
            state.title,
            self._rotation_cfg.max_turns_per_file,
        )
        self._writer.append(self._rel(path), record)
        self._sessions.mark_appended(path)
        await self._writer.flush()
        await self._remove_pending(state)
        self._stats["turns"] += 1
        if self._active.get(state.key) is state:
            self._active.pop(state.key, None)
        await self._sessions.flush()
        self._logger.info(
            "[llm_backup] 写入 %s (status=%s, steps=%d, tools=%d)",
            path,
            state.status,
            len(state.steps),
            len(state.tool_calls),
        )

    async def _await_media(self, state: TurnState) -> None:
        tasks = list(state.media_tasks)
        state.media_tasks.clear()
        if not tasks:
            return
        pending = [task for task in tasks if not task.done()]
        timeout = self._performance.media_wait_seconds
        if pending and timeout > 0:
            _, still_running = await asyncio.wait(pending, timeout=timeout)
            for task in still_running:
                task.cancel()
            if still_running:
                note = "media_timeout"
                state.note = f"{state.note};{note}" if state.note else note
        for task in tasks:
            if task.cancelled():
                continue
            error = task.exception()
            if error is not None:
                self._stats["errors"] += 1
                self._last_error = str(error)

    async def _collect_input_media(self, state: TurnState, event: Any) -> None:
        try:
            refs: list[dict[str, Any]] = []
            for component in event_components(event):
                for source, name, kind in self._media_sources(component):
                    ref = await self._media.store_source(
                        source, name=name, kind=kind, day=state.day
                    )
                    if ref:
                        refs.append(ref)
            if refs:
                state.input_media.extend(refs)
                await self._media.flush()
        except Exception as exc:
            self._stats["errors"] += 1
            self._last_error = f"media: {exc}"
            self._logger.warning("[llm_backup] 采集输入媒体失败: %s", exc)

    def _media_sources(self, component: Any) -> list[tuple[str, str, str]]:
        kind = media_kind(component)
        if kind is None:
            if component_kind(component) == "Reply" and self._config.media.capture_quoted:
                items: list[tuple[str, str, str]] = []
                for nested in getattr(component, "chain", None) or []:
                    items.extend(self._media_sources(nested))
                return items
            return []
        source = media_source(component)
        if not source:
            return []
        name = media_display_name(component, f"{kind}_{int(time.time())}")
        return [(source, name, kind)]

    def _tool_media_sources(
        self,
        tool_name: str,
        args: Any,
    ) -> list[tuple[str, str, str]]:
        if tool_name != "send_message_to_user" or not isinstance(args, dict):
            return []
        messages = args.get("messages")
        if not isinstance(messages, list):
            return []
        items: list[tuple[str, str, str]] = []
        for entry in messages:
            if not isinstance(entry, dict):
                continue
            kind = _TOOL_MEDIA_KINDS.get(str(entry.get("type", "")).lower())
            if kind is None:
                continue
            source = str(entry.get("path") or entry.get("url") or "").strip()
            if not source:
                continue
            name = str(entry.get("name") or "").strip()
            if not name:
                name = (
                    url_basename(source)
                    if source.startswith(("http://", "https://"))
                    else Path(source.replace("\\", "/")).name
                )
            items.append((source, name or kind, kind))
        return items

    async def _store_tool_media(
        self,
        state: TurnState,
        record: ToolCallRecord,
        source: str,
        name: str,
        kind: str,
    ) -> None:
        ref = await self._media.store_source(source, name=name, kind=kind, day=state.day)
        if ref:
            record.media.append(ref)

    def _should_record(self, event: Any, umo: str) -> bool:
        return self._evaluate(event, umo)[0]

    @staticmethod
    def _tool_names(request: Any) -> list[str]:
        tool_set = getattr(request, "func_tool", None)
        names = getattr(tool_set, "names", None)
        if not callable(names):
            return []
        try:
            return [str(name) for name in names()]
        except Exception:
            return []

    @staticmethod
    def _pending_tool(state: TurnState, name: str) -> ToolCallRecord | None:
        for record in reversed(state.tool_calls):
            if record.done:
                continue
            if record.name == name:
                return record
        return None

    def _origin(self, event: Any) -> str:
        if event_platform(event) == "cron":
            return "cron"
        if _call(event, "get_extra", None, "provider_request") is not None:
            return "plugin"
        return "user"

    @staticmethod
    def _base_key(event: Any, umo: str) -> str:
        message_obj = getattr(event, "message_obj", None)
        message_id = str(getattr(message_obj, "message_id", "") or "").strip()
        if not message_id:
            created = getattr(event, "created_at", 0)
            message_id = hashlib.sha1(f"{umo}:{created}".encode()).hexdigest()[:12]
        return f"{umo}#{message_id}"

    def _pending_path(self, state: TurnState) -> Path:
        digest = hashlib.sha1(state.key.encode("utf-8")).hexdigest()[:16]
        return self._root / "state" / "pending" / f"{digest}.json"

    async def _write_pending(self, state: TurnState) -> None:
        payload = {
            "key": state.key,
            "umo": state.umo,
            "day": state.day,
            "conv": state.conversation,
            "title": state.title,
            "ts": format_time(state.started_at),
            "who": state.sender_id,
            "private": state.private,
        }
        try:
            await asyncio.to_thread(
                write_json_atomic,
                self._pending_path(state),
                payload,
            )
        except OSError as exc:
            self._last_error = f"pending: {exc}"

    async def _remove_pending(self, state: TurnState) -> None:
        path = self._pending_path(state)
        try:
            await asyncio.to_thread(path.unlink, missing_ok=True)
        except OSError:
            pass

    async def _recover_pending(self) -> None:
        directory = self._root / "state" / "pending"
        if not directory.is_dir():
            return
        now = self._now()
        recovered: list[Path] = []
        for path in sorted(directory.glob("*.json")):
            payload: Any = None
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = None
            try:
                if isinstance(payload, dict) and payload.get("umo"):
                    umo = str(payload["umo"])
                    day = str(payload.get("day") or "") or self._rotation.resolve(umo, now)[0]
                    target = self._sessions.record_path(
                        umo,
                        day,
                        str(payload.get("conv") or ""),
                        str(payload.get("title") or ""),
                        self._rotation_cfg.max_turns_per_file,
                    )
                    record: dict[str, Any] = {
                        "ts": payload.get("ts") or format_time(now),
                        "st": "interrupted",
                        "note": "进程重启前未完成",
                    }
                    if payload.get("conv"):
                        record["conv"] = str(payload["conv"])
                    self._writer.append(self._rel(target), record)
                    self._sessions.mark_appended(target)
                    recovered.append(path)
            except Exception as exc:
                self._logger.warning("[llm_backup] 恢复未完成记录失败: %s", exc)
                recovered.append(path)
        if recovered:
            await self._writer.flush()
        for path in recovered:
            path.unlink(missing_ok=True)

    async def _cleanup_partials(self) -> None:
        await asyncio.to_thread(self._cleanup_partials_sync)

    def _cleanup_partials_sync(self) -> None:
        cutoff = time.time() - 3600
        for directory in (self._root / "state" / "tmp", self._root / "files"):
            if not directory.is_dir():
                continue
            for path in directory.rglob("*.part"):
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink(missing_ok=True)
                except OSError:
                    continue
        temp_dir = self._root / "state" / "tmp"
        if temp_dir.is_dir():
            for path in temp_dir.iterdir():
                try:
                    if path.is_file() and path.stat().st_mtime < cutoff:
                        path.unlink(missing_ok=True)
                except OSError:
                    continue

    async def _capture_persona(
        self,
        umo: str,
        event: Any,
        now: datetime,
        day: str,
    ) -> None:
        if not self._persona_cfg.enabled or self._persona_resolver is None:
            return
        daily_due = self._persona_cfg.daily and self._personas.day_of(umo) != day
        if not daily_due and not self._persona_cfg.on_change:
            return
        try:
            persona_id, persona = await self._persona_resolver(event, umo)
            if not persona:
                return
            await self._personas.capture(
                persona_id,
                persona,
                now,
                source="daily" if daily_due else "request",
            )
            if daily_due:
                self._personas.mark_day(umo, day)
            await self._personas.flush()
        except Exception as exc:
            self._logger.warning("[llm_backup] 人设快照失败: %s", exc)

    def _spawn(
        self,
        name: str,
        interval: int,
        handler: Callable[[], Awaitable[None]],
    ) -> None:
        self._tasks.append(
            asyncio.create_task(self._loop(name, max(5, interval), handler))
        )

    async def _loop(
        self,
        name: str,
        interval: int,
        handler: Callable[[], Awaitable[None]],
    ) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await handler()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._stats["errors"] += 1
                self._last_error = f"{name}: {exc}"
                self._logger.error(
                    "[llm_backup] 后台任务 %s 失败: %s", name, exc, exc_info=True
                )

    async def _tick_rotation(self) -> None:
        now = self._now()
        for umo in self._rotation.tracked():
            day, changed = self._rotation.resolve(
                umo, now, allow_advance=self._allow_scheduled
            )
            if not changed or not self._rotation_cfg.precreate:
                continue
            directory = self._sessions.day_dir(umo, day)
            try:
                await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
            except OSError:
                continue
        await asyncio.to_thread(self._rotation.save)

    async def _tick_persona(self) -> None:
        if self._persona_resolver is None:
            return
        now = self._now()
        for umo in self._rotation.tracked():
            day, _ = self._rotation.resolve(umo, now)
            if self._personas.day_of(umo) == day:
                continue
            try:
                persona_id, persona = await self._persona_resolver(None, umo)
            except Exception:
                continue
            if not persona:
                continue
            await self._personas.capture(persona_id, persona, now, source="poll")
            self._personas.mark_day(umo, day)
        await self._personas.flush()

    async def _tick_sweep(self) -> None:
        now = self._now()
        timeout = self._performance.turn_timeout_seconds
        for state in list(self._active.values()):
            reference = state.last_seen_at or state.started_at
            if (now - reference).total_seconds() < timeout:
                continue
            await self._finalize(state, status="timeout", note="turn timeout")

    async def _tick_retention(self) -> None:
        await asyncio.to_thread(self._apply_retention)

    def _apply_retention(self) -> None:
        days = self._storage_cfg.retention_days
        if days <= 0:
            return
        cutoff = (self._now().date() - timedelta(days=days)).isoformat()
        for path in (self._root / "sessions").glob("*/*"):
            if path.is_dir() and path.name < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        for path in (self._root / "files").glob("*/*"):
            if path.is_dir() and path.name < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        for path in (self._root / "personas").glob("*/*"):
            if path.is_file() and path.name[:10] < cutoff:
                path.unlink(missing_ok=True)

    def _rel(self, path: Path) -> str:
        return path.relative_to(self._root).as_posix()

    def _touch_state(self, state: TurnState) -> None:
        state.last_seen_at = self._now()

    def _now(self) -> datetime:
        if self._tz is not None:
            return datetime.now(self._tz)
        return datetime.now().astimezone()
