"""执行策略校验：既要拦住越权写法，也不能误伤标准写法。"""

import unittest

from tools.sandbox_policy import check_code_policy


class TestAllowedCode(unittest.TestCase):
    def test_main_guard_is_allowed(self):
        """if __name__ == "__main__" 是标准写法，不能拦。"""
        code = 'def f():\n    return 1\n\n\nif __name__ == "__main__":\n    print(f())\n'
        self.assertIsNone(check_code_policy(code))

    def test_doc_dunder_allowed(self):
        self.assertIsNone(check_code_policy('def f():\n    """doc"""\n    return f"{f.__doc__}"\n'))

    def test_plain_code_allowed(self):
        self.assertIsNone(check_code_policy("import re\n\ndef f(s):\n    return re.sub(' ', '', s)\n"))


class TestDeniedCode(unittest.TestCase):
    def test_class_dunder_denied(self):
        reason = check_code_policy("x = ().__class__.__bases__")
        self.assertIsNotNone(reason)
        self.assertIn("双下划线属性", reason or "")

    def test_globals_denied(self):
        self.assertIsNotNone(check_code_policy("globals()"))

    def test_open_denied(self):
        self.assertIsNotNone(check_code_policy("open('x')"))

    def test_disallowed_import_denied(self):
        reason = check_code_policy("import os")
        self.assertIsNotNone(reason)
        self.assertIn("os", reason or "")

    def test_syntax_error_reported(self):
        reason = check_code_policy("def f(:\n    pass\n")
        self.assertIsNotNone(reason)
        self.assertIn("语法错误", reason or "")

    def test_empty_code_denied(self):
        self.assertIsNotNone(check_code_policy("   "))

    def test_too_long_denied(self):
        self.assertIsNotNone(check_code_policy("x = 1\n" * 20000))


if __name__ == "__main__":
    unittest.main()
