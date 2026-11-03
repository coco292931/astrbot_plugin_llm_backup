"""媒体落盘、分类与去重测试。"""

from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

from core.config import PluginConfig
from core.media_store import MediaStore, sniff_extension

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
AMR_BYTES = b"#!AMR\n" + b"\x00" * 16


class MediaStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.source_dir = self.root / "source"
        self.source_dir.mkdir()
        self.store = MediaStore(self.root, PluginConfig({"media": {"enabled": True}}))

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _write(self, name: str, data: bytes) -> Path:
        path = self.source_dir / name
        path.write_bytes(data)
        return path

    async def test_store_local_image(self) -> None:
        source = self._write("photo.png", PNG_BYTES)
        ref = await self.store.store_source(
            str(source), name="photo.png", kind="image", day="2026-11-03"
        )
        self.assertIn("p", ref)
        self.assertIn("图片", ref["p"])
        self.assertIn("2026-11-03", ref["p"])
        self.assertTrue((self.root / ref["p"]).is_file())
        self.assertEqual(ref["b"], len(PNG_BYTES))

    async def test_deduplicates_same_content(self) -> None:
        first = self._write("a.png", PNG_BYTES)
        second = self._write("b.png", PNG_BYTES)
        ref_one = await self.store.store_source(
            str(first), name="a.png", kind="image", day="2026-11-03"
        )
        ref_two = await self.store.store_source(
            str(second), name="b.png", kind="image", day="2026-11-04"
        )
        self.assertEqual(ref_one["p"], ref_two["p"])
        self.assertEqual(
            len(list((self.root / "files").rglob("*.png"))),
            1,
        )

    async def test_custom_category_override(self) -> None:
        store = MediaStore(
            self.root,
            PluginConfig({"media": {"categories": {"自定义": ["png"]}}}),
        )
        source = self._write("x.png", PNG_BYTES)
        ref = await store.store_source(
            str(source), name="x.png", kind="image", day="2026-11-03"
        )
        self.assertIn("自定义", ref["p"])

    async def test_base64_without_name_sniffs_extension(self) -> None:
        payload = f"base64://{base64.b64encode(PNG_BYTES).decode()}"
        ref = await self.store.store_source(
            payload, name="", kind="image", day="2026-11-03"
        )
        self.assertTrue(ref["p"].endswith(".png"), ref["p"])

    async def test_data_uri_amr(self) -> None:
        payload = f"data:audio/amr;base64,{base64.b64encode(AMR_BYTES).decode()}"
        ref = await self.store.store_source(
            payload, name="", kind="audio", day="2026-11-03"
        )
        self.assertIn("音频", ref["p"])
        self.assertTrue(ref["p"].endswith(".amr"))

    async def test_missing_file_returns_error(self) -> None:
        ref = await self.store.store_source(
            str(self.root / "nope.png"), name="nope.png", kind="image", day="2026-11-03"
        )
        self.assertEqual(ref.get("e"), "missing")

    async def test_file_uri_source(self) -> None:
        source = self._write("uri.png", PNG_BYTES)
        ref = await self.store.store_source(
            source.as_uri(), name="uri.png", kind="image", day="2026-11-03"
        )
        self.assertIn("p", ref)

    async def test_too_large_is_rejected(self) -> None:
        store = MediaStore(
            self.root,
            PluginConfig({"media": {"max_file_mb": 1}}),
        )
        source = self._write("big.bin", b"\x00" * (1024 * 1024 + 10))
        ref = await store.store_source(
            str(source), name="big.bin", kind="file", day="2026-11-03"
        )
        self.assertEqual(ref.get("e"), "too_large")

    async def test_store_bytes_disabled_keeps_reference_only(self) -> None:
        store = MediaStore(
            self.root,
            PluginConfig({"media": {"store_bytes": False}}),
        )
        source = self._write("skip.png", PNG_BYTES)
        ref = await store.store_source(
            str(source), name="skip.png", kind="image", day="2026-11-03"
        )
        self.assertIn("s", ref)
        self.assertNotIn("p", ref)

    async def test_index_round_trip(self) -> None:
        source = self._write("persist.png", PNG_BYTES)
        ref = await self.store.store_source(
            str(source), name="persist.png", kind="image", day="2026-11-03"
        )
        await self.store.flush()

        reloaded = MediaStore(self.root, PluginConfig({}))
        reloaded.load()
        again = await reloaded.store_source(
            str(source), name="persist.png", kind="image", day="2026-11-05"
        )
        self.assertEqual(ref["p"], again["p"])


class SniffTests(unittest.TestCase):
    def test_common_formats(self) -> None:
        self.assertEqual(sniff_extension(PNG_BYTES), "png")
        self.assertEqual(sniff_extension(b"\xff\xd8\xff\xe0rest"), "jpg")
        self.assertEqual(sniff_extension(b"%PDF-1.7"), "pdf")
        self.assertEqual(sniff_extension(AMR_BYTES), "amr")
        self.assertEqual(sniff_extension(b"unknown"), "bin")


if __name__ == "__main__":
    unittest.main()
