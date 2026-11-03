"""写入队列与文件格式测试。"""

from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

from core.config import StorageConfig
from core.writer import Writer


class WriterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    async def test_appends_in_order(self) -> None:
        writer = Writer(self.root, StorageConfig())
        writer.start()
        for index in range(3):
            writer.append("sessions/a/b.jsonl", {"i": index})
        await writer.flush()
        await writer.stop()

        lines = (
            (self.root / "sessions" / "a" / "b.jsonl")
            .read_text(encoding="utf-8")
            .strip()
            .splitlines()
        )
        self.assertEqual(len(lines), 3)
        self.assertEqual([json.loads(line)["i"] for line in lines], [0, 1, 2])

    async def test_compact_json_has_no_spaces(self) -> None:
        writer = Writer(self.root, StorageConfig())
        writer.start()
        writer.append("x.jsonl", {"a": 1, "b": "中文"})
        await writer.flush()
        await writer.stop()
        raw = (self.root / "x.jsonl").read_text(encoding="utf-8").strip()
        self.assertEqual(raw, '{"a":1,"b":"中文"}')

    async def test_gzip_mode(self) -> None:
        writer = Writer(self.root, StorageConfig(compress_jsonl=True))
        writer.start()
        writer.append("x.jsonl.gz", {"a": 1})
        writer.append("x.jsonl.gz", {"a": 2})
        await writer.flush()
        await writer.stop()

        with gzip.open(self.root / "x.jsonl.gz", "rt", encoding="utf-8") as handle:
            lines = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual([line["a"] for line in lines], [1, 2])

    async def test_write_json_is_atomic_and_valid(self) -> None:
        writer = Writer(self.root, StorageConfig())
        writer.start()
        writer.write_json("state/meta.json", {"v": 1, "name": "测试"})
        await writer.flush()
        await writer.stop()

        payload = json.loads((self.root / "state" / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["name"], "测试")
        self.assertFalse(list((self.root / "state").glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
