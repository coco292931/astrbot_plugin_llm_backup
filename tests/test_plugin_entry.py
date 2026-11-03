"""插件入口测试：用 AstrBot API 桩验证钩子注册与生命周期。"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_NAME = "astrbot_plugin_llm_backup"

REGISTERED_HOOKS: list[str] = []
REGISTERED_COMMANDS: list[str] = []
DATA_DIR = Path(tempfile.mkdtemp(prefix="llm_backup_entry_"))
_PATCHED = False


def _decorator(name: str):
    def factory(*args: Any, **kwargs: Any):
        if name in {"command", "event_message_type", "permission_type"}:
            if name == "command" and args:
                REGISTERED_COMMANDS.append(str(args[0]))
        else:
            REGISTERED_HOOKS.append(name)

        def decorator(function):
            return function

        return decorator

    return factory


def _install_stubs() -> None:
    filter_module = types.ModuleType("astrbot.api.event.filter")
    for hook in (
        "on_llm_request",
        "on_agent_begin",
        "on_using_llm_tool",
        "on_llm_tool_respond",
        "on_llm_response",
        "on_agent_done",
        "on_decorating_result",
        "after_message_sent",
    ):
        setattr(filter_module, hook, _decorator(hook))
    for name in ("command", "event_message_type", "permission_type"):
        setattr(filter_module, name, _decorator(name))
    filter_module.PermissionType = type(
        "PermissionType", (), {"ADMIN": "admin", "MEMBER": "member"}
    )
    filter_module.EventMessageType = type(
        "EventMessageType", (), {"ALL": "all", "GROUP_MESSAGE": "group"}
    )

    event_module = types.ModuleType("astrbot.api.event")
    event_module.filter = filter_module
    event_module.AstrMessageEvent = type("AstrMessageEvent", (), {})
    event_module.MessageChain = type("MessageChain", (), {})

    class Star:
        def __init__(self, context: Any, config: Any = None) -> None:
            self.context = context
            self.logger = logging.getLogger("stub.star")

        async def initialize(self) -> None:
            return None

        async def terminate(self) -> None:
            return None

    class StarTools:
        @staticmethod
        def get_data_dir(plugin_name: str | None = None) -> str:
            return str(DATA_DIR)

    registered: list[tuple[str, str]] = []

    def register(name, author, desc, version, repo=None):
        def decorator(cls):
            registered.append((name, version))
            cls.__plugin_meta__ = (name, author, desc, version, repo)
            return cls

        return decorator

    star_module = types.ModuleType("astrbot.api.star")
    star_module.Context = type("Context", (), {})
    star_module.Star = Star
    star_module.StarTools = StarTools
    star_module.register = register

    api_module = types.ModuleType("astrbot.api")
    api_module.event = event_module
    api_module.star = star_module
    api_module.logger = logging.getLogger("astrbot")
    api_module.AstrBotConfig = dict

    root_module = types.ModuleType("astrbot")
    root_module.api = api_module
    root_module.logger = logging.getLogger("astrbot")

    sys.modules.update(
        {
            "astrbot": root_module,
            "astrbot.api": api_module,
            "astrbot.api.event": event_module,
            "astrbot.api.event.filter": filter_module,
            "astrbot.api.star": star_module,
        }
    )


_ALL_STUBS = [
    "astrbot",
    "astrbot.api",
    "astrbot.api.event",
    "astrbot.api.event.filter",
    "astrbot.api.star",
]


def setUpModule() -> None:
    global _PATCHED
    if not _PATCHED:
        _install_stubs()
        if str(PACKAGE_ROOT) not in sys.path:
            sys.path.insert(0, str(PACKAGE_ROOT))
        _PATCHED = True


def tearDownModule() -> None:
    for name in _ALL_STUBS:
        sys.modules.pop(name, None)
    sys.modules.pop(PACKAGE_NAME, None)
    sys.modules.pop(f"{PACKAGE_NAME}.main", None)


class EntryPointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = importlib.import_module(f"{PACKAGE_NAME}.main")
        cls.plugin_class = cls.module.LlmBackupPlugin

    def test_plugin_declares_metadata(self) -> None:
        name, author, desc, version, repo = self.plugin_class.__plugin_meta__
        self.assertEqual(name, PACKAGE_NAME)
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        self.assertTrue(desc)
        self.assertTrue(repo.startswith("https://"))

    def test_all_hooks_are_registered(self) -> None:
        for hook in (
            "on_llm_request",
            "on_agent_begin",
            "on_using_llm_tool",
            "on_llm_tool_respond",
            "on_llm_response",
            "on_agent_done",
        ):
            self.assertIn(hook, REGISTERED_HOOKS)
            self.assertTrue(
                asyncio.iscoroutinefunction(getattr(self.plugin_class, hook)),
                f"{hook} 必须是异步函数",
            )

    def test_commands_are_registered(self) -> None:
        for command in (
            "backup_status",
            "backup_now",
            "backup_rotate",
            "backup_doctor",
        ):
            self.assertIn(command, REGISTERED_COMMANDS)

    def test_hook_fallback_returns_decorator(self) -> None:
        decorator = self.module._hook("nonexistent_hook")
        self.assertTrue(callable(decorator(lambda x: x)))

    def test_no_plain_message_handler(self) -> None:
        self.assertFalse(
            hasattr(self.plugin_class, "on_any_message"),
            "注册普通消息处理器会改变 AstrBot 的唤醒判定",
        )


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = importlib.import_module(f"{PACKAGE_NAME}.main")

    async def test_initialize_and_terminate(self) -> None:
        context = type(
            "Ctx",
            (),
            {
                "persona_manager": type(
                    "PM",
                    (),
                    {
                        "selected_default_persona_v3": {
                            "name": "default",
                            "prompt": "你是助手",
                            "tools": None,
                            "skills": None,
                            "begin_dialogs": [],
                        }
                    },
                )(),
                "get_config": lambda self, umo=None: {"timezone": "Asia/Shanghai"},
            },
        )()

        plugin = self.module.LlmBackupPlugin(context, {"enabled": True})
        await plugin.initialize()
        self.assertIsNotNone(plugin._service)
        self.assertTrue(Path(plugin._service.root).exists())

        text = plugin._service.status_text("aiocqhttp:FriendMessage:10001")
        self.assertIn("备份目录", text)

        await plugin.terminate()
        self.assertIsNone(plugin._service)

    async def test_persona_resolver_without_manager(self) -> None:
        plugin = self.module.LlmBackupPlugin(type("Ctx", (), {"get_config": lambda self, umo=None: {}})(), {})
        persona_id, persona = await plugin._resolve_persona(None, "aiocqhttp:FriendMessage:1")
        self.assertEqual(persona_id, "")
        self.assertIsNone(persona)

    async def test_persona_resolver_reads_trace_extra(self) -> None:
        persona = {"name": "猫娘", "prompt": "喵"}

        manager = type(
            "PM",
            (),
            {"get_persona_v3_by_id": lambda self, pid: persona if pid == "猫娘" else None},
        )()
        context = type("Ctx", (), {"persona_manager": manager})()
        plugin = self.module.LlmBackupPlugin(context, {})

        event = type(
            "Event", (), {"get_extra": lambda self, key, default=None: "猫娘"}
        )()
        persona_id, resolved = await plugin._resolve_persona(event, "umo")
        self.assertEqual(persona_id, "猫娘")
        self.assertIs(resolved, persona)


if __name__ == "__main__":
    unittest.main()
