"""端到端流程测试：钩子事件到落盘文件。"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.config import PluginConfig
from core.service import BackupService
from core.session_store import SessionStore

from .fakes import (
    FakeConversation,
    FakeEvent,
    FakeMessage,
    FakePart,
    FakeRequest,
    FakeResponse,
    FakeRunContext,
    FakeTool,
    FakeToolResult,
    FakeToolSet,
    FakeUsage,
)

PNG_A = b"\x89PNG\r\n\x1a\n" + b"A" * 40
PNG_B = b"\x89PNG\r\n\x1a\n" + b"B" * 40
PERSONA = {
    "prompt": "你是猫娘",
    "tools": None,
    "skills": [],
    "begin_dialogs": [],
}


class ServiceFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        self.source = self.root / "source"
        self.source.mkdir()
        self.input_image = self.source / "cat.png"
        self.input_image.write_bytes(PNG_A)
        self.tool_image = self.source / "out.png"
        self.tool_image.write_bytes(PNG_B)
        self.service: BackupService | None = None

    async def asyncTearDown(self) -> None:
        if self.service is not None:
            await self.service.stop()
        self._temp.cleanup()

    async def _start(self, extra: dict | None = None) -> BackupService:
        raw = {"enabled": True, "detail_level": "full"}
        raw.update(extra or {})
        service = BackupService(PluginConfig(raw), self.data_dir)

        async def resolver(event: Any, umo: str) -> tuple[str, Any]:
            return "猫娘", PERSONA

        service.set_persona_resolver(resolver)
        await service.start()
        self.service = service
        return service

    @property
    def backup_root(self) -> Path:
        return self.data_dir / "backup"

    def _records(self) -> list[dict]:
        files = sorted((self.backup_root / "sessions").rglob("*.jsonl"))
        records: list[dict] = []
        for path in files:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    records.append(json.loads(line))
        return records

    def _event(self, **overrides) -> FakeEvent:
        defaults = {
            "message": [
                FakePart("Plain", text="你好"),
                FakePart("Image", path=str(self.input_image)),
            ]
        }
        defaults.update(overrides)
        return FakeEvent(**defaults)

    async def test_full_turn_is_recorded(self) -> None:
        service = await self._start()
        event = self._event()
        event.set_extra("_trace_persona_id", "猫娘")
        request = FakeRequest(
            prompt="你好",
            contexts=[{"role": "user", "content": "旧消息"}],
            conversation=FakeConversation("cid-1", "闲聊"),
            func_tool=FakeToolSet(["send_message_to_user", "web_search"]),
        )

        await service.handle_llm_request(event, request)
        run_context = FakeRunContext(
            [FakeMessage("system", "sys"), FakeMessage("user", "你好")]
        )
        await service.handle_agent_begin(event, run_context)

        tool = FakeTool("send_message_to_user")
        args = {"messages": [{"type": "image", "path": str(self.tool_image)}]}
        await service.handle_tool_start(event, tool, args)
        await service.handle_tool_end(event, tool, args, FakeToolResult(["sent"]))
        run_context.messages.append(
            FakeMessage(
                "assistant",
                [FakePart("think", think="先发图"), FakePart("text", text="发你了")],
                [object()],
            )
        )
        run_context.messages.append(FakeMessage("tool", "sent"))

        await service.handle_llm_response(
            event,
            FakeResponse(reasoning="最终思考", text="好的", usage=FakeUsage(12, 34)),
        )
        await service.handle_agent_done(
            event, run_context, FakeResponse(reasoning="最终思考", text="好的")
        )
        await service._writer.flush()

        records = self._records()
        self.assertEqual(len(records), 1)
        record = records[0]

        self.assertEqual(record["who"], {"id": "10001"})
        self.assertNotIn("gid", record)
        self.assertEqual(record["conv"], SessionStore.conversation_short("cid-1"))
        self.assertEqual(record["in"]["t"], "你好")
        self.assertEqual(len(record["in"]["m"]), 1)
        self.assertTrue((self.backup_root / record["in"]["m"][0]).is_file())
        self.assertEqual(record["req"]["model"], "test-model")
        self.assertEqual(
            record["req"]["tools"], ["send_message_to_user", "web_search"]
        )
        self.assertEqual(record["req"]["sp"]["t"], "you are a bot")

        self.assertEqual(len(record["steps"]), 1)
        step = record["steps"][0]
        self.assertEqual(step["r"], "先发图")
        self.assertEqual(step["t"], "发你了")
        self.assertEqual(step["tools"][0]["n"], "send_message_to_user")
        self.assertEqual(len(step["tools"][0]["m"]), 1)
        self.assertTrue((self.backup_root / step["tools"][0]["m"][0]).is_file())
        self.assertEqual(step["tools"][0]["r"], "sent")

        self.assertEqual(record["out"]["r"], "最终思考")
        self.assertEqual(record["out"]["t"], "好的")
        self.assertEqual(record["u"], {"i": 12, "o": 34})
        self.assertNotIn("st", record)

        self.assertEqual(len(list((self.backup_root / "files").rglob("*.png"))), 2)
        persona_files = list((self.backup_root / "personas").rglob("*.md"))
        self.assertEqual(len(persona_files), 1)
        self.assertIn("你是猫娘", persona_files[0].read_text(encoding="utf-8"))
        self.assertTrue(
            list((self.backup_root / "sessions").rglob("_session.json"))
        )

    async def test_cron_event_is_skipped_by_default(self) -> None:
        service = await self._start()
        event = self._event(platform="cron")
        await service.handle_llm_request(event, FakeRequest())
        self.assertEqual(service.status()["active"], 0)
        self.assertFalse((self.backup_root / "sessions").exists())

    async def test_group_chat_is_skipped_by_default(self) -> None:
        service = await self._start()
        event = self._event(
            umo="aiocqhttp:GroupMessage:20002",
            msg_type="GroupMessage",
            group_id="20002",
        )
        await service.handle_llm_request(event, FakeRequest())
        self.assertEqual(service.status()["active"], 0)

    async def test_group_chat_recorded_when_enabled(self) -> None:
        service = await self._start({"include_groups": True})
        event = self._event(
            umo="aiocqhttp:GroupMessage:20002",
            msg_type="GroupMessage",
            group_id="20002",
        )
        event.set_extra("_trace_persona_id", "猫娘")
        await service.handle_llm_request(event, FakeRequest(conversation=FakeConversation()))
        await service.handle_agent_done(
            event, FakeRunContext([FakeMessage("system", "sys")]), None
        )
        await service._writer.flush()

        records = self._records()
        self.assertEqual(records[0]["gid"], "20002")
        self.assertEqual(records[0]["who"]["n"], "alice")

    async def test_second_request_supersedes_first(self) -> None:
        service = await self._start()
        event = self._event()
        await service.handle_llm_request(event, FakeRequest())
        await service.handle_agent_begin(
            event,
            FakeRunContext(
                [FakeMessage("system", "sys"), FakeMessage("user", "你好")]
            ),
        )
        await service.handle_llm_request(event, FakeRequest())
        await service.handle_agent_done(
            event,
            FakeRunContext(
                [FakeMessage("system", "sys"), FakeMessage("user", "你好")]
            ),
            None,
        )
        await service._writer.flush()

        records = self._records()
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].get("st"), "superseded")
        self.assertNotIn("st", records[1])

    async def test_whitelist_filters_other_sessions(self) -> None:
        service = await self._start(
            {"filter_mode": "whitelist", "umos": ["aiocqhttp:FriendMessage:99999"]}
        )
        await service.handle_llm_request(self._event(), FakeRequest())
        self.assertEqual(service.status()["active"], 0)

    async def test_sender_filter(self) -> None:
        service = await self._start({"senders": ["88888"]})
        await service.handle_llm_request(self._event(), FakeRequest())
        self.assertEqual(service.status()["active"], 0)

    async def test_degraded_when_context_is_replaced(self) -> None:
        service = await self._start()
        event = self._event()
        await service.handle_llm_request(event, FakeRequest())
        await service.handle_agent_begin(
            event,
            FakeRunContext(
                [FakeMessage("system", "sys"), FakeMessage("user", "你好")]
            ),
        )
        await service.handle_agent_done(
            event,
            FakeRunContext([FakeMessage("system", "压缩后的提示")]),
            None,
        )
        await service._writer.flush()

        record = self._records()[0]
        self.assertEqual(record["st"], "degraded")
        self.assertEqual(record["note"], "steps_unavailable")

    async def test_media_capture_can_be_disabled(self) -> None:
        service = await self._start({"media": {"enabled": False}})
        event = self._event()
        await service.handle_llm_request(event, FakeRequest())
        await service.handle_agent_done(
            event, FakeRunContext([FakeMessage("system", "sys")]), None
        )
        await service._writer.flush()

        record = self._records()[0]
        self.assertNotIn("m", record.get("in", {}))
        self.assertFalse((self.backup_root / "files").exists())

    async def test_flush_finalizes_active_turn(self) -> None:
        service = await self._start()
        event = self._event()
        await service.handle_llm_request(event, FakeRequest())
        await service.flush()

        records = self._records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["st"], "manual")

    async def test_pending_file_removed_after_finalize(self) -> None:
        service = await self._start()
        event = self._event()
        await service.handle_llm_request(event, FakeRequest())
        self.assertTrue(list((self.backup_root / "state" / "pending").glob("*.json")))
        await service.handle_agent_done(
            event, FakeRunContext([FakeMessage("system", "sys")]), None
        )
        self.assertFalse(list((self.backup_root / "state" / "pending").glob("*.json")))

    async def test_status_text_contains_root(self) -> None:
        service = await self._start()
        text = service.status_text("aiocqhttp:FriendMessage:10001")
        self.assertIn(str(self.backup_root), text)
        self.assertIn("运行状态", text)
        self.assertIn("当前逻辑日", text)

    async def test_two_turns_share_one_day_file(self) -> None:
        service = await self._start()
        first = self._event()
        await service.handle_llm_request(first, FakeRequest())
        await service.handle_agent_done(
            first, FakeRunContext([FakeMessage("system", "sys")]), None
        )
        second = self._event(message_id="msg-2")
        await service.handle_llm_request(second, FakeRequest())
        await service.handle_agent_done(
            second, FakeRunContext([FakeMessage("system", "sys")]), None
        )
        await service._writer.flush()

        files = list((self.backup_root / "sessions").rglob("*.jsonl"))
        self.assertEqual(len(files), 1)
        self.assertEqual(len(files[0].read_text(encoding="utf-8").strip().splitlines()), 2)

    async def test_rotation_creates_new_day_folder_after_idle(self) -> None:
        service = await self._start({"rotation": {"idle_minutes": 120}})
        umo = "aiocqhttp:FriendMessage:10001"

        def at(hour: int, day: int) -> None:
            service._now = lambda: datetime(
                2026, 11, day, hour, 0, tzinfo=timezone.utc
            )

        at(23, 3)
        first = self._event(message_id="a")
        await service.handle_llm_request(first, FakeRequest())
        await service.handle_agent_done(
            first, FakeRunContext([FakeMessage("system", "sys")]), None
        )

        at(1, 4)
        second = self._event(message_id="b")
        await service.handle_llm_request(second, FakeRequest())
        await service.handle_agent_done(
            second, FakeRunContext([FakeMessage("system", "sys")]), None
        )

        at(5, 4)
        third = self._event(message_id="c")
        await service.handle_llm_request(third, FakeRequest())
        await service.handle_agent_done(
            third, FakeRunContext([FakeMessage("system", "sys")]), None
        )
        await service._writer.flush()

        days = sorted(
            path.name
            for path in (self.backup_root / "sessions").glob("*/*")
            if path.is_dir()
        )
        self.assertEqual(days, ["2026-11-03", "2026-11-04"])
        self.assertEqual(service.status(umo)["day"], "2026-11-04")

    async def test_doctor_reports_ok(self) -> None:
        service = await self._start()
        text = await service.doctor()
        self.assertIn("检查结果", text)
        self.assertFalse((self.backup_root / ".write_probe").exists())

    async def test_force_rotate_moves_to_next_day(self) -> None:
        service = await self._start()
        umo = "aiocqhttp:FriendMessage:10001"
        event = self._event()
        await service.handle_llm_request(event, FakeRequest())
        current = service.status(umo)["day"]
        await service.flush()
        nxt = await service.force_rotate(umo)
        self.assertNotEqual(nxt, current)

    async def test_empty_repeat_request_writes_single_record(self) -> None:
        service = await self._start()
        event = self._event()
        await service.handle_llm_request(event, FakeRequest())
        await service.handle_llm_request(event, FakeRequest())
        await service.handle_agent_done(
            event, FakeRunContext([FakeMessage("system", "sys")]), None
        )
        await service._writer.flush()

        records = self._records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["in"]["t"], "你好")

    async def test_rotation_scheduled_mode_ignores_write_path(self) -> None:
        service = await self._start(
            {"rotation": {"mode": "scheduled", "idle_minutes": 0}}
        )
        umo = "aiocqhttp:FriendMessage:10001"
        service._rotation.force(umo, "2020-01-01")
        event = self._event()
        await service.handle_llm_request(event, FakeRequest())
        await service.handle_agent_done(
            event, FakeRunContext([FakeMessage("system", "sys")]), None
        )
        await service._writer.flush()

        day_dirs = [
            path.name for path in (self.backup_root / "sessions").rglob("*") if path.is_dir()
        ]
        self.assertIn("2020-01-01", day_dirs)


if __name__ == "__main__":
    unittest.main()
