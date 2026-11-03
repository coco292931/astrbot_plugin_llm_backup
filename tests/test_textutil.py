"""各类消息组件与文本工具的兼容性测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.textutil import (
    brief_source,
    chain_plain_text,
    component_kind,
    json_safe,
    media_display_name,
    media_kind,
    media_source,
    split_message_content,
    tool_result_text,
    truncate_text,
)

from .fakes import FakeEnumType, FakeFileComponent, FakeMessage, FakePart, FakeToolResult


class ComponentKindTests(unittest.TestCase):
    def test_string_type(self) -> None:
        component = type("Plain", (), {"type": "Plain"})()
        self.assertEqual(component_kind(component), "Plain")

    def test_enum_like_type(self) -> None:
        component = type("Image", (), {"type": FakeEnumType("Image")})()
        self.assertEqual(component_kind(component), "Image")

    def test_missing_type_falls_back_to_class_name(self) -> None:
        component = type("CustomSegment", (), {})()
        self.assertEqual(component_kind(component), "CustomSegment")

    def test_media_kind_mapping(self) -> None:
        self.assertEqual(media_kind(FakePart("Image")), "image")
        self.assertEqual(media_kind(FakePart("Record")), "audio")
        self.assertEqual(media_kind(FakePart("Video")), "video")
        self.assertEqual(media_kind(FakePart("File")), "file")
        self.assertIsNone(media_kind(FakePart("Plain")))


class MediaSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.sample = self.root / "sample.png"
        self.sample.write_bytes(b"\x89PNG\r\n\x1a\npayload")

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_prefers_existing_local_path(self) -> None:
        component = FakePart(
            "Image",
            path=str(self.sample),
            file="https://example.com/remote.png",
            url="https://example.com/remote.png",
        )
        self.assertEqual(media_source(component), str(self.sample))

    def test_falls_back_to_url_when_path_missing(self) -> None:
        component = FakePart(
            "Image",
            path=str(self.root / "missing.png"),
            file="https://example.com/remote.png",
            url="https://example.com/remote.png",
        )
        self.assertEqual(media_source(component), "https://example.com/remote.png")

    def test_base64_source_is_kept(self) -> None:
        component = FakePart("Image", file="base64://AAAA", url="")
        self.assertEqual(media_source(component), "base64://AAAA")

    def test_file_component_never_touches_property(self) -> None:
        component = FakeFileComponent("报告.pdf", file_=str(self.sample))
        self.assertEqual(media_source(component), str(self.sample))

    def test_file_component_uses_url_without_local_file(self) -> None:
        component = FakeFileComponent(
            "报告.pdf",
            file_="",
            url="https://example.com/report.pdf",
        )
        self.assertEqual(media_source(component), "https://example.com/report.pdf")

    def test_record_uses_file_field(self) -> None:
        component = FakePart("Record", file=str(self.sample), url="")
        self.assertEqual(media_source(component), str(self.sample))

    def test_video_uses_path(self) -> None:
        component = FakePart("Video", path=str(self.sample), file="", url="")
        self.assertEqual(media_source(component), str(self.sample))

    def test_missing_media_returns_empty(self) -> None:
        component = FakePart("Image", path="", file="", url="")
        self.assertEqual(media_source(component), "")


class MediaNameTests(unittest.TestCase):
    def test_file_component_prefers_declared_name(self) -> None:
        component = FakeFileComponent("报告.pdf", url="https://example.com/abc")
        self.assertEqual(media_display_name(component, "fallback"), "报告.pdf")

    def test_url_basename(self) -> None:
        component = FakePart("Image", url="https://example.com/a/b/cat.png?x=1")
        self.assertEqual(media_display_name(component, "fallback"), "cat.png")

    def test_local_basename(self) -> None:
        component = FakePart("Image", path="C:\\data\\dog.jpg")
        self.assertEqual(media_display_name(component, "fallback"), "dog.jpg")

    def test_base64_uses_fallback(self) -> None:
        component = FakePart("Image", file="base64://AAAA", url="")
        self.assertEqual(media_display_name(component, "image_1"), "image_1")


class ChainTextTests(unittest.TestCase):
    def test_mixed_components(self) -> None:
        chain = [
            FakePart("Plain", text="hello "),
            FakePart("At", qq="10001"),
            FakePart("Plain", text="world"),
            FakePart("Image", file="x"),
        ]
        self.assertEqual(chain_plain_text(chain), "hello world")

    def test_message_chain_object(self) -> None:
        chain = type("Chain", (), {"chain": [FakePart("Plain", text="ok")]})()
        self.assertEqual(chain_plain_text(chain), "ok")

    def test_empty(self) -> None:
        self.assertEqual(chain_plain_text(None), "")
        self.assertEqual(chain_plain_text([]), "")


class MessageContentTests(unittest.TestCase):
    def test_string_content(self) -> None:
        self.assertEqual(split_message_content(FakeMessage("user", "hi")), ("", "hi"))

    def test_parts_content(self) -> None:
        message = FakeMessage(
            "assistant",
            [
                FakePart("think", think="先想一想"),
                FakePart("text", text="答案"),
                FakePart("image_url", image_url="x"),
            ],
        )
        self.assertEqual(split_message_content(message), ("先想一想", "答案"))

    def test_missing_content(self) -> None:
        self.assertEqual(split_message_content(FakeMessage("tool")), ("", ""))


class ToolResultTests(unittest.TestCase):
    def test_flattens_text_parts(self) -> None:
        result = FakeToolResult(["ok", "done"])
        self.assertEqual(tool_result_text(result), "ok\ndone")

    def test_image_part_is_placeholder(self) -> None:
        result = type(
            "Result",
            (),
            {"content": [FakePart("image", data="AAAA")]},
        )()
        self.assertEqual(tool_result_text(result), "[FakePart]")

    def test_none(self) -> None:
        self.assertEqual(tool_result_text(None), "")


class JsonSafeTests(unittest.TestCase):
    def test_nested_structures(self) -> None:
        payload = {"a": [1, {"b": ("x", 2)}], "c": {3, 4}}
        safe = json_safe(payload)
        self.assertEqual(safe["a"][0], 1)
        self.assertEqual(sorted(safe["c"]), [3, 4])

    def test_bytes_and_objects(self) -> None:
        class Opaque:
            def __str__(self) -> str:
                return "opaque"

        safe = json_safe({"blob": b"1234", "obj": Opaque()})
        self.assertEqual(safe["blob"], "<4 bytes>")
        self.assertEqual(safe["obj"], "opaque")

    def test_model_dump_is_used(self) -> None:
        class Model:
            def model_dump(self) -> dict:
                return {"k": 1}

        self.assertEqual(json_safe(Model()), {"k": 1})


class TextHelperTests(unittest.TestCase):
    def test_truncate_text(self) -> None:
        self.assertEqual(truncate_text("abcdef", 3), "abc…")
        self.assertEqual(truncate_text("abc", 10), "abc")
        self.assertEqual(truncate_text(None, 10), "")

    def test_brief_source_hides_payload(self) -> None:
        self.assertEqual(brief_source("base64://" + "A" * 100), "base64://<100 chars>")
        self.assertIn("https://example.com", brief_source("https://example.com/x"))


if __name__ == "__main__":
    unittest.main()
