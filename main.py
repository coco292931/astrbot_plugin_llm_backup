"""对话备份插件入口。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .core.config import PluginConfig
from .core.service import BackupService, event_umo

PLUGIN_NAME = "astrbot_plugin_llm_backup"


def _hook(name: str):
    """按 AstrBot 版本注册可选钩子，缺失时退化为空装饰器。"""
    factory = getattr(filter, name, None)
    if not callable(factory):
        return lambda function: function
    try:
        return factory()
    except Exception:
        return lambda function: function


@register(
    PLUGIN_NAME,
    "coco",
    "按会话备份 LLM 往来流水、媒体与人设",
    "1.0.0",
    "https://github.com/coco292931/astrbot_plugin_llm_backup",
)
class LlmBackupPlugin(Star):
    """记录用户与 LLM 的每一轮往来，按会话与逻辑日落盘。"""

    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context, config)
        self._config = PluginConfig(dict(config or {}))
        self._service: BackupService | None = None

    async def initialize(self) -> None:
        data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        service = BackupService(self._config, data_dir, self.logger)
        service.set_persona_resolver(self._resolve_persona)
        try:
            timezone = str(self.context.get_config().get("timezone") or "")
        except Exception:
            timezone = ""
        service.set_timezone(timezone)
        await service.start()
        self._service = service

    async def terminate(self) -> None:
        service = self._service
        self._service = None
        if service is not None:
            await service.stop()

    async def _resolve_persona(self, event: Any, umo: str) -> tuple[str, Any]:
        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            return "", None

        persona_id = ""
        if event is not None:
            getter = getattr(event, "get_extra", None)
            if callable(getter):
                try:
                    persona_id = str(getter("_trace_persona_id") or "")
                except Exception:
                    persona_id = ""

        if not persona_id:
            persona_id = await self._conversation_persona_id(umo)

        persona = None
        lookup = getattr(manager, "get_persona_v3_by_id", None)
        if persona_id and callable(lookup):
            try:
                persona = lookup(persona_id)
            except Exception:
                persona = None

        if persona is None:
            get_default = getattr(manager, "get_default_persona_v3", None)
            if callable(get_default):
                try:
                    persona = await get_default(umo)
                except Exception:
                    persona = None
            if persona is not None and not persona_id:
                persona_id = str(persona.get("name") or "default")

        if persona is None:
            persona = getattr(manager, "selected_default_persona_v3", None)
        return persona_id, persona

    async def _conversation_persona_id(self, umo: str) -> str:
        conversation_manager = getattr(self.context, "conversation_manager", None)
        if conversation_manager is None:
            return ""
        try:
            cid = await conversation_manager.get_curr_conversation_id(umo)
            if not cid:
                return ""
            conversation = await conversation_manager.get_conversation(umo, cid)
        except Exception:
            return ""
        return str(getattr(conversation, "persona_id", "") or "")

    async def _dispatch(self, name: str, method: str, *args: Any) -> None:
        service = self._service
        if service is None:
            return
        try:
            await getattr(service, method)(*args)
        except Exception:
            self.logger.exception("[llm_backup] %s 处理失败", name)

    @_hook("on_llm_request")
    async def on_llm_request(self, event: AstrMessageEvent, request: Any) -> None:
        await self._dispatch("on_llm_request", "handle_llm_request", event, request)

    @_hook("on_agent_begin")
    async def on_agent_begin(self, event: AstrMessageEvent, run_context: Any) -> None:
        await self._dispatch("on_agent_begin", "handle_agent_begin", event, run_context)

    @_hook("on_using_llm_tool")
    async def on_using_llm_tool(
        self,
        event: AstrMessageEvent,
        tool: Any,
        tool_args: Any,
    ) -> None:
        await self._dispatch(
            "on_using_llm_tool", "handle_tool_start", event, tool, tool_args
        )

    @_hook("on_llm_tool_respond")
    async def on_llm_tool_respond(
        self,
        event: AstrMessageEvent,
        tool: Any,
        tool_args: Any,
        tool_result: Any,
    ) -> None:
        await self._dispatch(
            "on_llm_tool_respond",
            "handle_tool_end",
            event,
            tool,
            tool_args,
            tool_result,
        )

    @_hook("on_llm_response")
    async def on_llm_response(self, event: AstrMessageEvent, response: Any) -> None:
        await self._dispatch("on_llm_response", "handle_llm_response", event, response)

    @_hook("on_agent_done")
    async def on_agent_done(
        self,
        event: AstrMessageEvent,
        run_context: Any,
        response: Any = None,
    ) -> None:
        await self._dispatch(
            "on_agent_done", "handle_agent_done", event, run_context, response
        )

    @filter.command("backup_status")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def backup_status(self, event: AstrMessageEvent):
        """查看备份运行状态。"""
        service = self._service
        if service is None:
            yield event.plain_result("备份服务尚未就绪。")
            return
        yield event.plain_result(service.status_text(event_umo(event)))

    @filter.command("backup_now")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def backup_now(self, event: AstrMessageEvent):
        """立即落盘当前缓存。"""
        service = self._service
        if service is None:
            yield event.plain_result("备份服务尚未就绪。")
            return
        await service.flush()
        yield event.plain_result(f"已落盘。\n{service.status_text(event_umo(event))}")

    @filter.command("backup_rotate")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def backup_rotate(self, event: AstrMessageEvent):
        """强制把当前会话切换到下一个逻辑日目录。"""
        service = self._service
        if service is None:
            yield event.plain_result("备份服务尚未就绪。")
            return
        day = await service.force_rotate(event_umo(event))
        yield event.plain_result(f"已切换到 {day}。")

    @filter.command("backup_doctor")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def backup_doctor(self, event: AstrMessageEvent):
        """检查备份目录与配置。"""
        service = self._service
        if service is None:
            yield event.plain_result("备份服务尚未就绪。")
            return
        yield event.plain_result(await service.doctor())
