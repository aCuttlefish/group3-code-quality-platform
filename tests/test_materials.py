import base64
import time
import unittest
from unittest.mock import patch

from fr04.materials import decode_upload, inspect_source, normalize_spec


def encoded(raw):
    return base64.b64encode(raw).decode("ascii")


class MaterialsTests(unittest.TestCase):
    def test_collects_docstrings_and_comments_in_source_order(self):
        code = '''#!/usr/bin/env python
# coding: utf-8
"""模块说明。"""
# R1：输入整数。
def identity(value):
    """R2：返回输入。
    保留数值。
    """
    text = "# 这是字符串，不是注释"
    # 边界同样适用。
    return value  # 返回原值。
'''
        result = inspect_source(code, r"C:\fakepath\identity.py")
        self.assertEqual(result["filename"], "identity.py")
        self.assertEqual(result["function_name"], "identity")
        self.assertEqual(result["code"], code)
        self.assertEqual([item["line"] for item in result["comment_items"]], [3, 4, 6, 10, 11])
        self.assertEqual([item["kind"] for item in result["comment_items"]], ["module_docstring", "comment", "function_docstring", "comment", "comment"])
        self.assertEqual(result["comments_text"], "模块说明。\nR1：输入整数。\nR2：返回输入。\n保留数值。\n边界同样适用。\n返回原值。")
        self.assertNotIn("字符串", result["comments_text"])
        self.assertEqual(result["metadata"]["parameters"][0]["name"], "value")

    def test_empty_comments_and_no_comments_return_empty_text(self):
        result = inspect_source('#\n#   \n###\ndef identity(value):\n    """   """\n    return value\n')
        self.assertEqual(result["comments_text"], "")
        self.assertEqual(result["comment_items"], [])
        self.assertIsNone(result["filename"])
        with self.assertRaisesRegex(ValueError, "说明为空"):
            normalize_spec(result["comments_text"])

    def test_comment_after_one_line_docstring_keeps_order(self):
        result = inspect_source('"""模块""" # 补充\ndef identity(value):\n    return value\n')
        self.assertEqual(result["comments_text"], "模块\n补充")

    def test_encoding_word_outside_cookie_is_a_comment(self):
        result = inspect_source('def identity(value): # coding: utf-8\n    return value\n')
        self.assertEqual(result["comments_text"], "coding: utf-8")

    def test_base64_utf8_bom_and_safe_filename(self):
        text = '# 说明\ndef identity(value):\n    return value\n'
        decoded = decode_upload("../../upload.PY", encoded(text.encode("utf-8-sig")))
        self.assertEqual(decoded, text)
        self.assertEqual(inspect_source(decoded, "../../upload.PY")["filename"], "upload.PY")

    def test_gbk_cookie_is_canonicalized_and_compiles_as_utf8_bytes(self):
        code = '#!/usr/bin/python\r\n# -*- coding: gbk -*-\r\n"""中文说明"""\r\ndef identity(value):\r\n    return value\r\n'
        decoded = decode_upload("sample.py", encoded(code.encode("gbk")))
        self.assertIn("coding: utf-8", decoded)
        self.assertIn("中文说明", decoded)
        self.assertEqual(decoded, code.replace("coding: gbk", "coding: utf-8"))
        compile(decoded.encode("utf-8"), "subject.py", "exec")
        self.assertEqual(inspect_source(decoded)["comments_text"], "中文说明")

    def test_upload_rejects_suffix_base64_encoding_and_size(self):
        invalid = [("x.txt", encoded(b"def f(x): return x")), (".py", encoded(b"x")),
                   ("x.py", "%%not-base64"), ("x.py", "YQ==\n"), ("x.py", "你好"),
                   ("x.py", ""), ("x.py", encoded(b"\xff\xff")),
                   ("x.py", encoded(b"# coding: unknown-encoding\ndef f(x): return x")),
                   ("x.py", encoded(b"x" * 80001))]
        for filename, content in invalid:
            with self.subTest(filename=filename, length=len(content)), self.assertRaises(ValueError):
                decode_upload(filename, content)

    def test_source_limits_and_function_selection(self):
        invalid = ["", "  ", "# comments only", "async def f(x): return x",
                   "def f(x): return x\ndef g(x): return x", "def bad(:\n    return 1",
                   "import os\ndef f(x): return x", "def f(x):\n    def g(y): return y\n    return x",
                   "#\n" * 200 + "def f(x): return x", "#" + "x" * 20000 + "\ndef f(x): return x"]
        for code in invalid:
            with self.subTest(code=code[:60]), self.assertRaises(ValueError):
                inspect_source(code)

    def test_inspection_never_executes_function_or_module(self):
        started = time.monotonic()
        with patch("builtins.exec", side_effect=AssertionError("must not execute")):
            result = inspect_source("def forever(value):\n    # 持续等待\n    while True:\n        pass\n")
            self.assertEqual(result["function_name"], "forever")
            with self.assertRaises(ValueError):
                inspect_source("raise ValueError('top-level')\ndef f(x): return x")
        self.assertLess(time.monotonic() - started, 1)

    def test_normalize_preserves_numbered_rules_and_wraps_multiline(self):
        original = "R1：输入为整数。\nR2: 返回原值。"
        self.assertEqual(normalize_spec("  " + original + "\n"), original)
        self.assertEqual(normalize_spec("输入为整数。\n返回原值。"), "R1：输入为整数。\n返回原值。")
        self.assertEqual(normalize_spec("r1: 原有编号"), "r1: 原有编号")
        self.assertEqual(len(normalize_spec("x" * 1997)), 2000)

    def test_normalize_rejects_duplicate_empty_and_overlong(self):
        for text in ("", " \n ", "R1：一条\nr1: 另一条", "x" * 1998, "R1：" + "x" * 1998, None):
            with self.subTest(text=str(text)[:60]), self.assertRaises(ValueError):
                normalize_spec(text)


if __name__ == "__main__":
    unittest.main()
