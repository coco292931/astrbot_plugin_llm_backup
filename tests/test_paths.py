"""路径与文件名处理测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from core.paths import (
    extension_of,
    file_uri_to_path,
    is_file_uri,
    sanitize_component,
    safe_join,
    truncate_bytes,
    url_basename,
)


class SanitizeTests(unittest.TestCase):
    def test_replaces_windows_illegal_characters(self) -> None:
        self.assertEqual(sanitize_component('a<b>c:d"e/f\\g|h?i*j'), "a_b_c_d_e_f_g_h_i_j")

    def test_strips_trailing_dot_and_space(self) -> None:
        self.assertEqual(sanitize_component("name. "), "name")

    def test_reserved_names_are_escaped(self) -> None:
        self.assertEqual(sanitize_component("CON"), "_CON")
        self.assertEqual(sanitize_component("lpt9.txt"), "_lpt9.txt")

    def test_empty_uses_fallback(self) -> None:
        self.assertEqual(sanitize_component("   "), "unnamed")
        self.assertEqual(sanitize_component("", fallback="session"), "session")

    def test_dot_only_is_not_path_traversal(self) -> None:
        self.assertNotIn(sanitize_component(".."), {"..", "."})

    def test_truncates_by_utf8_bytes(self) -> None:
        result = sanitize_component("中" * 200)
        self.assertLessEqual(len(result.encode("utf-8")), 120)

    def test_keeps_cjk_and_emoji(self) -> None:
        self.assertEqual(sanitize_component("猫娘🐱"), "猫娘🐱")


class TruncateTests(unittest.TestCase):
    def test_short_text_untouched(self) -> None:
        self.assertEqual(truncate_bytes("abc", 10), "abc")

    def test_multibyte_cut_is_valid_utf8(self) -> None:
        result = truncate_bytes("中文内容", 7)
        self.assertLessEqual(len(result.encode("utf-8")), 7)
        self.assertTrue(result.encode("utf-8").decode("utf-8"))

    def test_suffix_reserved(self) -> None:
        result = truncate_bytes("abcdef", 4, suffix="_")
        self.assertEqual(result, "abc_")


class FileUriTests(unittest.TestCase):
    def test_detects_scheme(self) -> None:
        self.assertTrue(is_file_uri("file:///tmp/a.txt"))
        self.assertFalse(is_file_uri("https://example.com/a.txt"))

    def test_decodes_percent_encoding(self) -> None:
        self.assertTrue(file_uri_to_path("file:///tmp/a%20b.txt").endswith("a b.txt"))

    @unittest.skipUnless(os.name == "nt", "仅在 Windows 验证盘符还原")
    def test_windows_drive_letter(self) -> None:
        self.assertEqual(file_uri_to_path("file:///C:/data/x.png"), "C:\\data\\x.png")

    def test_rejects_other_scheme(self) -> None:
        with self.assertRaises(ValueError):
            file_uri_to_path("https://example.com/a.txt")


class SafeJoinTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_stays_inside_root(self) -> None:
        self.assertEqual(
            safe_join(self.root, "sessions", "a"),
            self.root / "sessions" / "a",
        )

    def test_sanitizes_each_component(self) -> None:
        self.assertEqual(safe_join(self.root, "a/b"), self.root / "a_b")


class ExtensionTests(unittest.TestCase):
    def test_extension_of(self) -> None:
        self.assertEqual(extension_of("photo.PNG"), "png")
        self.assertEqual(extension_of("C:\\x\\a.tar.gz"), "gz")
        self.assertEqual(extension_of("noext"), "")

    def test_url_basename(self) -> None:
        self.assertEqual(url_basename("https://a/b/c%20d.jpg?x=1"), "c d.jpg")


if __name__ == "__main__":
    unittest.main()
