"""会话目录与文件命名测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.config import PluginConfig
from core.session_store import SessionStore


class UmoParsingTests(unittest.TestCase):
    def test_standard_umo(self) -> None:
        self.assertEqual(
            SessionStore.parse_umo("aiocqhttp:FriendMessage:10001"),
            ("aiocqhttp", "FriendMessage", "10001"),
        )

    def test_session_id_keeps_colons(self) -> None:
        self.assertEqual(
            SessionStore.parse_umo("webchat:GroupMessage:guild:channel:1")[2],
            "guild:channel:1",
        )

    def test_incomplete_umo(self) -> None:
        platform, message_type, session_id = SessionStore.parse_umo("only")
        self.assertEqual(platform, "only")
        self.assertEqual(message_type, "unknown")
        self.assertEqual(session_id, "only")


class ConversationIdTests(unittest.TestCase):
    def test_deterministic(self) -> None:
        first = SessionStore.conversation_short("cid-1")
        second = SessionStore.conversation_short("cid-1")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 8)

    def test_empty_cid(self) -> None:
        self.assertEqual(SessionStore.conversation_short(""), "_default")
        self.assertEqual(SessionStore.conversation_short(None), "_default")


class SessionStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.config = PluginConfig({})
        self.umo = "aiocqhttp:FriendMessage:10001"
        self.store = SessionStore(self.root, self.config)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_session_key_sanitizes(self) -> None:
        key = self.store.session_key("aiocqhttp:FriendMessage:10001")
        self.assertEqual(key, "aiocqhttp_FriendMessage_10001")

    def test_record_path_contains_day_and_stem(self) -> None:
        path = self.store.record_path(
            self.umo, "2026-11-03", "ab12cd34", "闲聊"
        )
        self.assertEqual(path.name, "ab12cd34.jsonl")
        self.assertEqual(path.parent.name, "2026-11-03")

    def test_title_placeholder_can_be_added(self) -> None:
        store = SessionStore(
            self.root,
            PluginConfig({"conversation_file_template": "{conv_short}_{conv_title}"}),
        )
        path = store.record_path(self.umo, "2026-11-03", "ab12cd34", "闲聊")
        self.assertEqual(path.name, "ab12cd34_闲聊.jsonl")

    def test_empty_title_is_trimmed(self) -> None:
        store = SessionStore(
            self.root,
            PluginConfig({"conversation_file_template": "{conv_short}_{conv_title}"}),
        )
        path = store.record_path(self.umo, "2026-11-03", "ab12cd34", "")
        self.assertEqual(path.name, "ab12cd34.jsonl")

    def test_title_change_keeps_default_filename(self) -> None:
        first = self.store.record_path(self.umo, "2026-11-03", "ab12cd34", "闲聊")
        second = self.store.record_path(self.umo, "2026-11-03", "ab12cd34", "改名了")
        self.assertEqual(first, second)

    def test_max_turns_creates_new_part(self) -> None:
        first = self.store.record_path(
            self.umo, "2026-11-03", "ab12cd34", "闲聊", max_turns=2
        )
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_text('{"a":1}\n{"a":2}\n', encoding="utf-8")

        fresh = SessionStore(self.root, self.config)
        second = fresh.record_path(
            self.umo, "2026-11-03", "ab12cd34", "闲聊", max_turns=2
        )
        self.assertNotEqual(first, second)
        self.assertTrue(second.name.endswith("_2.jsonl"), second.name)

    def test_gzip_suffix(self) -> None:
        store = SessionStore(
            self.root,
            PluginConfig({"storage": {"compress_jsonl": True}}),
        )
        path = store.record_path(self.umo, "2026-11-03", "ab12cd34", "闲聊")
        self.assertTrue(path.name.endswith(".jsonl.gz"))

    async def test_meta_round_trip(self) -> None:
        short = self.store.register_conversation(
            self.umo, "cid-1", "闲聊", "猫娘"
        )
        self.store.set_identity(self.umo, private=True, name="alice")
        await self.store.flush()

        meta_path = self.store.meta_path(self.umo)
        self.assertTrue(meta_path.is_file())
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual(data["conversations"][short]["title"], "闲聊")
        self.assertEqual(data["conversations"][short]["persona"], "猫娘")
        self.assertTrue(data["private"])
        self.assertEqual(data["name"], "alice")

        reloaded = SessionStore(self.root, self.config)
        self.assertEqual(reloaded.meta(self.umo)["name"], "alice")

    def test_day_file_count(self) -> None:
        path = self.store.record_path(self.umo, "2026-11-03", "ab12cd34", "闲聊")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"a":1}\n{"a":2}\n{"a":3}\n', encoding="utf-8")
        self.assertEqual(self.store.day_file_count(self.umo, "2026-11-03"), 3)


if __name__ == "__main__":
    unittest.main()
