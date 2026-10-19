"""消息总线测试：消息构造与渲染。"""

import unittest

from multi_agent.messages import (
    CODER,
    FIXER,
    KIND_HANDOFF,
    KIND_REQUEST_FIX,
    REVIEWER,
    WORKFLOW,
    format_message,
    make_message,
)


class TestMakeMessage(unittest.TestCase):
    def test_required_fields_present(self):
        message = make_message(CODER, REVIEWER, KIND_HANDOFF, "首版代码就绪")
        self.assertTrue(message["id"])
        self.assertEqual(message["sender"], CODER)
        self.assertEqual(message["recipient"], REVIEWER)
        self.assertEqual(message["kind"], KIND_HANDOFF)
        self.assertTrue(message["created_at"])

    def test_ids_are_unique(self):
        first = make_message(CODER, REVIEWER, KIND_HANDOFF, "a")
        second = make_message(CODER, REVIEWER, KIND_HANDOFF, "b")
        self.assertNotEqual(first["id"], second["id"])

    def test_iteration_is_optional(self):
        without = make_message(REVIEWER, FIXER, KIND_REQUEST_FIX, "x")
        self.assertNotIn("iteration", without)
        with_iteration = make_message(REVIEWER, FIXER, KIND_REQUEST_FIX, "x", iteration=2)
        self.assertEqual(with_iteration["iteration"], 2)

    def test_payload_defaults_to_empty_dict(self):
        message = make_message(FIXER, WORKFLOW, KIND_HANDOFF, "x")
        self.assertEqual(message["payload"], {})

    def test_explicit_id_and_timestamp(self):
        message = make_message(
            CODER, REVIEWER, KIND_HANDOFF, "x", message_id="abc123", created_at="2026-01-01T00:00:00"
        )
        self.assertEqual(message["id"], "abc123")
        self.assertEqual(message["created_at"], "2026-01-01T00:00:00")


class TestFormatMessage(unittest.TestCase):
    def test_basic_line(self):
        message = make_message(CODER, REVIEWER, KIND_HANDOFF, "首版代码就绪")
        line = format_message(message)
        self.assertIn("coder → reviewer", line)
        self.assertIn("handoff", line)
        self.assertIn("首版代码就绪", line)

    def test_iteration_is_rendered(self):
        message = make_message(FIXER, REVIEWER, KIND_HANDOFF, "修完了", iteration=1)
        self.assertIn("轮次 1", format_message(message))

    def test_missing_fields_do_not_crash(self):
        self.assertTrue(format_message({}))


if __name__ == "__main__":
    unittest.main()
