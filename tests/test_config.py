"""配置解析与配置模板测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.config import CaptureSettings, PluginConfig

ROOT = Path(__file__).resolve().parents[1]


class DetailPresetTests(unittest.TestCase):
    def test_standard_preset(self) -> None:
        capture = PluginConfig({"detail_level": "standard"}).capture_settings()
        self.assertEqual(capture.user_input, "full")
        self.assertEqual(capture.reasoning, "summary")
        self.assertEqual(capture.tool_calls, "summary")
        self.assertFalse(capture.tool_results)

    def test_minimal_preset(self) -> None:
        capture = PluginConfig({"detail_level": "minimal"}).capture_settings()
        self.assertEqual(capture.reasoning, "off")
        self.assertEqual(capture.tool_calls, "off")
        self.assertFalse(capture.tool_results)

    def test_full_preset(self) -> None:
        capture = PluginConfig({"detail_level": "full"}).capture_settings()
        self.assertEqual(capture.system_prompt, "full")
        self.assertTrue(capture.tool_results)

    def test_explicit_override_wins(self) -> None:
        capture = PluginConfig(
            {
                "detail_level": "minimal",
                "capture": {"reasoning": "full", "tool_results": "on"},
            }
        ).capture_settings()
        self.assertEqual(capture.reasoning, "full")
        self.assertTrue(capture.tool_results)

    def test_unknown_level_falls_back_to_preset(self) -> None:
        capture = PluginConfig(
            {"detail_level": "full", "capture": {"reply": "nonsense"}}
        ).capture_settings()
        self.assertEqual(capture.reply, "full")

    def test_limits_are_read(self) -> None:
        capture = PluginConfig(
            {"limits": {"summary_chars": 42, "max_reply_chars": 1234}}
        ).capture_settings()
        self.assertEqual(capture.summary_chars, 42)
        self.assertEqual(capture.max_reply_chars, 1234)


class CaptureRenderTests(unittest.TestCase):
    def test_off_returns_empty(self) -> None:
        capture = CaptureSettings(reasoning="off")
        self.assertEqual(capture.render("reasoning", "内容"), "")

    def test_mask_patterns(self) -> None:
        capture = CaptureSettings(mask_patterns=(r"\d{11}",))
        self.assertEqual(capture.render("reply", "电话 13800000000"), "电话 ***")

    def test_invalid_regex_is_ignored(self) -> None:
        capture = CaptureSettings(mask_patterns=("(",))
        self.assertEqual(capture.render("reply", "abc"), "abc")


class ReaderTests(unittest.TestCase):
    def test_bool_variants(self) -> None:
        config = PluginConfig(
            {"a": "true", "b": "off", "c": 1, "d": False}
        )
        self.assertTrue(config.as_bool("a"))
        self.assertFalse(config.as_bool("b"))
        self.assertTrue(config.as_bool("c"))
        self.assertFalse(config.as_bool("d", True))
        self.assertTrue(config.as_bool("missing", True))

    def test_list_from_string(self) -> None:
        self.assertEqual(PluginConfig({"x": " a, b ,, c"}).as_list("x"), ["a", "b", "c"])

    def test_int_invalid(self) -> None:
        self.assertEqual(PluginConfig({"x": "abc"}).as_int("x", 7), 7)


class RootTests(unittest.TestCase):
    def test_default_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = PluginConfig({}).resolve_root(Path(temp))
            self.assertEqual(root, Path(temp) / "backup")

    def test_relative_root_uses_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = PluginConfig({"backup_root": "my-backup"}).resolve_root(Path(temp))
            self.assertEqual(root, (Path(temp) / "my-backup").resolve())

    def test_absolute_root_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "abs"
            root = PluginConfig({"backup_root": str(target)}).resolve_root(Path(temp))
            self.assertEqual(root, target.resolve())


class SchemaTests(unittest.TestCase):
    def test_schema_is_valid_json(self) -> None:
        payload = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertIsInstance(payload, dict)

    def test_required_keys_have_defaults(self) -> None:
        payload = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        required = [
            "enabled",
            "backup_root",
            "filter_mode",
            "umos",
            "senders",
            "detail_level",
            "capture",
            "media",
            "persona",
            "rotation",
            "storage",
        ]
        for key in required:
            self.assertIn(key, payload)
            item = payload[key]
            if item.get("type") == "object":
                self.assertIn("items", item)
            else:
                self.assertIn("default", item)

    def test_nested_items_have_types(self) -> None:
        payload = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        for group in ("capture", "media", "persona", "rotation", "storage", "limits"):
            for name, item in payload[group]["items"].items():
                self.assertIn("type", item, f"{group}.{name} 缺少 type")
                self.assertIn("default", item, f"{group}.{name} 缺少 default")

    def test_metadata_declares_version(self) -> None:
        text = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
        self.assertIn("astrbot_plugin_llm_backup", text)
        self.assertIn("version:", text)

    def test_metadata_version_matches_entry_point(self) -> None:
        import re

        text = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
        metadata_version = re.search(r'^version:\s*"([^"]+)"', text, re.M).group(1)
        main_source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn(f'"{metadata_version}"', main_source)


if __name__ == "__main__":
    unittest.main()
