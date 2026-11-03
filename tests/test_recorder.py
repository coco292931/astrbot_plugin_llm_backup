"""轮次记录构建与中间步骤还原测试。"""

from __future__ import annotations

import unittest
from datetime import datetime

from core.config import CaptureSettings
from core.recorder import (
    ToolCallRecord,
    TurnState,
    build_turn_record,
    collect_steps,
    locate_baseline,
    message_signature,
)

from .fakes import FakeMessage, FakePart


def make_state(**overrides) -> TurnState:
    defaults = {
        "key": "umo#1",
        "umo": "aiocqhttp:FriendMessage:10001",
        "day": "2026-11-03",
        "started_at": datetime(2026, 11, 3, 14, 0, 0),
        "private": True,
        "conversation": "ab12cd34",
        "sender_id": "10001",
        "user_text": "你好",
        "prompt": "你好",
        "reply": "你好呀",
    }
    defaults.update(overrides)
    return TurnState(**defaults)


class RecordShapeTests(unittest.TestCase):
    def test_private_chat_omits_group_and_name(self) -> None:
        record = build_turn_record(make_state(), CaptureSettings())
        self.assertEqual(record["who"], {"id": "10001"})
        self.assertNotIn("gid", record)
        self.assertEqual(record["conv"], "ab12cd34")
        self.assertNotIn("src", record)

    def test_group_chat_keeps_group_and_name(self) -> None:
        state = make_state(
            private=False,
            group_id="20002",
            sender_name="alice",
        )
        record = build_turn_record(state, CaptureSettings())
        self.assertEqual(record["gid"], "20002")
        self.assertEqual(record["who"], {"id": "10001", "n": "alice"})

    def test_minimal_detail_drops_reasoning(self) -> None:
        state = make_state(reasoning="想了很多", reply="答案")
        capture = CaptureSettings(reasoning="off", reply="summary", summary_chars=5)
        record = build_turn_record(state, capture)
        self.assertNotIn("r", record["out"])
        self.assertEqual(record["out"]["t"], "答案")

    def test_summary_truncates(self) -> None:
        state = make_state(reply="x" * 100)
        capture = CaptureSettings(reply="summary", summary_chars=10)
        record = build_turn_record(state, capture)
        self.assertEqual(record["out"]["t"], f"{'x' * 10}…")

    def test_prompt_recorded_only_when_different(self) -> None:
        state = make_state(prompt="改写后的提示")
        record = build_turn_record(state, CaptureSettings())
        self.assertEqual(record["in"]["p"], "改写后的提示")

    def test_request_metadata(self) -> None:
        state = make_state(
            model="gpt-test",
            context_count=12,
            tools=["send_message_to_user", "web_search"],
            system_prompt_hash="abcdef0123456789",
        )
        capture = CaptureSettings(tool_calls="full", system_prompt="hash")
        record = build_turn_record(state, capture)
        self.assertEqual(record["req"]["model"], "gpt-test")
        self.assertEqual(record["req"]["ctx"], 12)
        self.assertEqual(record["req"]["tools"], ["send_message_to_user", "web_search"])
        self.assertEqual(record["req"]["sp"], "abcdef0123456789")

    def test_system_prompt_full(self) -> None:
        state = make_state(
            system_prompt_hash="h",
            system_prompt_text="完整提示词",
        )
        record = build_turn_record(state, CaptureSettings(system_prompt="full"))
        self.assertEqual(record["req"]["sp"], {"h": "h", "t": "完整提示词"})

    def test_usage_and_status(self) -> None:
        state = make_state(usage_in=11, usage_out=22, status="degraded")
        record = build_turn_record(state, CaptureSettings())
        self.assertEqual(record["u"], {"i": 11, "o": 22})
        self.assertEqual(record["st"], "degraded")

    def test_media_paths_and_errors(self) -> None:
        state = make_state(
            input_media=[{"p": "files/图片/2026-11-03/a.png"}, {"e": "missing"}],
        )
        record = build_turn_record(state, CaptureSettings())
        self.assertEqual(record["in"]["m"], ["files/图片/2026-11-03/a.png"])
        self.assertEqual(record["note"], "media_error=1")


class BaselineTests(unittest.TestCase):
    def test_signature_match(self) -> None:
        messages = [FakeMessage("system", "sys"), FakeMessage("user", "你好")]
        start = locate_baseline(messages, 2, message_signature(messages[1]), "你好")
        self.assertEqual(start, 2)

    def test_fallback_to_user_text_after_compaction(self) -> None:
        messages = [
            FakeMessage("system", "sys"),
            FakeMessage("user", "别的"),
            FakeMessage("user", "你好"),
        ]
        start = locate_baseline(messages, 4, "user:2", "你好")
        self.assertEqual(start, 3)

    def test_unknown_returns_negative(self) -> None:
        messages = [FakeMessage("system", "sys")]
        self.assertEqual(locate_baseline(messages, 9, "user:2", "你好"), -1)


class StepCollectionTests(unittest.TestCase):
    def test_assistant_tool_calls_are_paired(self) -> None:
        messages = [
            FakeMessage("system", "sys"),
            FakeMessage("user", "你好"),
            FakeMessage(
                "assistant",
                [FakePart("think", think="需要查一下"), FakePart("text", text="稍等")],
                [object()],
            ),
            FakeMessage("tool", "结果"),
        ]
        tool = ToolCallRecord(name="web_search", args={"q": "x"}, duration_ms=88)
        steps, degraded = collect_steps(messages, 2, [tool], CaptureSettings())
        self.assertFalse(degraded)
        self.assertEqual(steps[0]["r"], "需要查一下")
        self.assertEqual(steps[0]["t"], "稍等")
        self.assertEqual(steps[0]["tools"][0]["n"], "web_search")
        self.assertEqual(steps[0]["tools"][0]["ms"], 88)

    def test_leftover_tool_calls_become_extra_step(self) -> None:
        messages = [FakeMessage("system", "sys"), FakeMessage("user", "hi")]
        tool = ToolCallRecord(name="only_tool")
        steps, degraded = collect_steps(messages, 2, [tool], CaptureSettings())
        self.assertTrue(degraded)
        self.assertEqual(steps[0]["tools"][0]["n"], "only_tool")

    def test_tool_calls_off_skips_tools(self) -> None:
        messages = [
            FakeMessage("user", "hi"),
            FakeMessage("assistant", [FakePart("think", think="想")], [object()]),
        ]
        capture = CaptureSettings(tool_calls="off", reasoning="full")
        steps, degraded = collect_steps(messages, 1, [ToolCallRecord(name="x")], capture)
        self.assertFalse(degraded)
        self.assertNotIn("tools", steps[0])
        self.assertEqual(steps[0]["r"], "想")

    def test_invalid_start_is_degraded(self) -> None:
        steps, degraded = collect_steps([], -1, [], CaptureSettings())
        self.assertEqual(steps, [])
        self.assertTrue(degraded)

    def test_tool_result_included_only_when_enabled(self) -> None:
        record = ToolCallRecord(name="x", result="返回内容")
        messages = [FakeMessage("assistant", [], [object()])]
        with_result, _ = collect_steps(
            messages, 0, [record], CaptureSettings(tool_calls="summary", tool_results=True)
        )
        without_result, _ = collect_steps(
            messages, 0, [record], CaptureSettings(tool_calls="summary", tool_results=False)
        )
        self.assertEqual(with_result[0]["tools"][0]["r"], "返回内容")
        self.assertNotIn("r", without_result[0]["tools"][0])


if __name__ == "__main__":
    unittest.main()
