"""文本处理：代码块剥离、模型输出取文本、JSON 提取。"""

import unittest

from multi_agent.text_utils import (
    clean_code_output,
    extract_json_array,
    message_text,
    strip_code_fence,
    strip_model_artifacts,
)


class _Response:
    def __init__(self, content):
        self.content = content


class TestStripCodeFence(unittest.TestCase):
    def test_plain_code_untouched(self):
        self.assertEqual(strip_code_fence("def f():\n    return 1"), "def f():\n    return 1")

    def test_python_fence_removed(self):
        text = "```python\ndef f():\n    return 1\n```"
        self.assertEqual(strip_code_fence(text), "def f():\n    return 1")

    def test_bare_fence_removed(self):
        text = "```\ndef f():\n    return 1\n```"
        self.assertEqual(strip_code_fence(text), "def f():\n    return 1")

    def test_truncated_fence_still_strips_header(self):
        text = "```python\ndef f():\n    return 1"
        self.assertEqual(strip_code_fence(text), "def f():\n    return 1")

    def test_empty(self):
        self.assertEqual(strip_code_fence(""), "")


class TestStripModelArtifacts(unittest.TestCase):
    def test_trailing_tag_removed(self):
        # 实测：fixer 输出末尾多了一行 </parameter>，导致语法错误
        text = "def f():\n    return 1\n\n\n_demo()\n</parameter>\n"
        self.assertEqual(strip_model_artifacts(text), "def f():\n    return 1\n\n\n_demo()")

    def test_leading_tag_removed(self):
        text = "<response>\ndef f():\n    return 1"
        self.assertEqual(strip_model_artifacts(text), "def f():\n    return 1")

    def test_tag_in_the_middle_is_kept(self):
        # 中间出现的同名标签可能是字符串字面量里的真实内容，不动它
        text = 'html = """<p>a</p>"""\n<parameter>\nreturn html'
        self.assertIn("<parameter>", strip_model_artifacts(text))

    def test_plain_code_untouched(self):
        text = "def f():\n    return 1"
        self.assertEqual(strip_model_artifacts(text), text)

    def test_empty(self):
        self.assertEqual(strip_model_artifacts(""), "")


class TestCleanCodeOutput(unittest.TestCase):
    def test_fence_and_trailing_tag_both_cleaned(self):
        text = "```python\ndef f():\n    return 1\n```\n</parameter>"
        self.assertEqual(clean_code_output(text), "def f():\n    return 1")

    def test_plain_code_unchanged(self):
        text = "def f():\n    return 1"
        self.assertEqual(clean_code_output(text), text)


class TestMessageText(unittest.TestCase):
    def test_string_content(self):
        self.assertEqual(message_text(_Response("hi")), "hi")

    def test_block_content(self):
        response = _Response([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
        self.assertEqual(message_text(response), "ab")

    def test_list_of_strings(self):
        self.assertEqual(message_text(_Response(["x", "y"])), "xy")

    def test_none_content(self):
        self.assertEqual(message_text(_Response(None)), "")


class TestExtractJsonArray(unittest.TestCase):
    def test_pure_array(self):
        self.assertEqual(extract_json_array("[{\"a\": 1}]"), [{"a": 1}])

    def test_empty_array(self):
        self.assertEqual(extract_json_array("[]"), [])

    def test_fenced_array(self):
        text = "这是审查结果：\n```json\n[{\"issue\": \"x\"}]\n```\n以上。"
        self.assertEqual(extract_json_array(text), [{"issue": "x"}])

    def test_array_with_prose(self):
        self.assertEqual(extract_json_array("结论如下 [1, 2] 完毕"), [1, 2])

    def test_object_wrapper_yields_inner_array(self):
        text = '{"findings": [{"issue": "x"}]}'
        self.assertEqual(extract_json_array(text), [{"issue": "x"}])

    def test_object_without_array_returns_none(self):
        self.assertIsNone(extract_json_array('{"summary": "看起来没问题"}'))

    def test_garbage_returns_none(self):
        self.assertIsNone(extract_json_array("完全不是 JSON"))


if __name__ == "__main__":
    unittest.main()
