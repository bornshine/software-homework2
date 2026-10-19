"""写文件工具：路径白名单、敏感文件拦截、每次写入确认。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from tools.file_writer import is_sensitive, make_write_file_tool, prepare_write


class WriteToolTestCase(unittest.TestCase):
    """把允许目录临时换成 tmp，避免污染真实工作区。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self._patch = patch.object(config, "ALLOWED_ROOTS", [self.root])
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(self._tmp.cleanup)

    def target(self, name: str = "out.py") -> Path:
        return self.root / name


class TestPrepareWrite(WriteToolTestCase):
    def test_new_file_preview(self):
        preview, error = prepare_write(str(self.target()), "print(1)")
        self.assertEqual(error, "")
        self.assertIsNotNone(preview)
        self.assertFalse(preview.exists)
        self.assertEqual(preview.diff, "")
        self.assertEqual(preview.action, "新建")

    def test_existing_file_preview_has_diff(self):
        path = self.target()
        path.write_text("print(1)\n", encoding="utf-8")

        preview, error = prepare_write(str(path), "print(2)\n", reason="改输出")

        self.assertEqual(error, "")
        self.assertTrue(preview.exists)
        self.assertIn("-print(1)", preview.diff)
        self.assertIn("+print(2)", preview.diff)
        self.assertEqual(preview.reason, "改输出")
        self.assertEqual(preview.action, "覆盖")

    def test_outside_allowed_root_rejected(self):
        outside = self.root.parent / "outside.py"
        preview, error = prepare_write(str(outside), "x")
        self.assertIsNone(preview)
        self.assertIn("不在允许的目录内", error)

    def test_parent_escape_rejected(self):
        preview, error = prepare_write(str(self.root / ".." / "escape.py"), "x")
        self.assertIsNone(preview)
        self.assertIn("不在允许的目录内", error)

    def test_sensitive_file_rejected(self):
        preview, error = prepare_write(str(self.target(".env")), "SECRET=1")
        self.assertIsNone(preview)
        self.assertIn("敏感文件", error)

    def test_directory_target_rejected(self):
        preview, error = prepare_write(str(self.root), "x")
        self.assertIsNone(preview)
        self.assertIn("目录", error)

    def test_empty_path_rejected(self):
        preview, error = prepare_write("   ", "x")
        self.assertIsNone(preview)
        self.assertIn("路径为空", error)


class TestIsSensitive(WriteToolTestCase):
    def test_sensitive_names(self):
        self.assertTrue(is_sensitive(self.target(".env")))
        self.assertTrue(is_sensitive(self.target("server.pem")))
        self.assertTrue(is_sensitive(self.target("id_rsa")))

    def test_normal_names(self):
        self.assertFalse(is_sensitive(self.target("main.py")))


class TestWriteFileTool(WriteToolTestCase):
    def _tool(self, approved: bool):
        seen: list = []

        def confirm(preview):
            seen.append(preview)
            return approved

        return make_write_file_tool(confirm=confirm), seen

    def test_writes_after_confirmation(self):
        tool, seen = self._tool(True)
        result = tool.invoke(
            {"path": str(self.target()), "content": "print(1)\n", "reason": "新建脚本"}
        )

        self.assertIn("已新建", result)
        self.assertEqual(self.target().read_text(encoding="utf-8"), "print(1)\n")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].reason, "新建脚本")

    def test_skips_when_rejected(self):
        tool, seen = self._tool(False)
        result = tool.invoke({"path": str(self.target()), "content": "print(1)"})

        self.assertIn("用户拒绝", result)
        self.assertFalse(self.target().exists())
        self.assertEqual(len(seen), 1)          # 确实问过了

    def test_overwrites_only_after_confirmation(self):
        self.target().write_text("old\n", encoding="utf-8")
        tool, _ = self._tool(True)

        result = tool.invoke({"path": str(self.target()), "content": "new\n"})

        self.assertIn("已覆盖", result)
        self.assertEqual(self.target().read_text(encoding="utf-8"), "new\n")

    def test_creates_parent_dirs(self):
        tool, _ = self._tool(True)
        nested = self.root / "pkg" / "mod.py"

        result = tool.invoke({"path": str(nested), "content": "x = 1\n"})

        self.assertIn("已新建", result)
        self.assertTrue(nested.exists())

    def test_outside_root_is_rejected_without_asking(self):
        tool, seen = self._tool(True)
        outside = self.root.parent / "nope.py"

        result = tool.invoke({"path": str(outside), "content": "x"})

        self.assertIn("已拒绝写入", result)
        self.assertEqual(seen, [])              # 连确认都不应该弹
        self.assertFalse(outside.exists())


if __name__ == "__main__":
    unittest.main()
