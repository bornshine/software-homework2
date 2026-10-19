"""节点级韧性工具测试：重试、工具降级、LLM 调用封装、错误标记。"""

import unittest
from unittest.mock import patch

import config
from multi_agent.resilience import (
    call_tool,
    has_fatal_error,
    invoke_llm,
    is_timeout_error,
    make_error,
    retry_call,
)
from tests.helpers import BoomModel, ScriptedModel


class TestRetryCall(unittest.TestCase):
    def test_success_first_try(self):
        attempt = retry_call(lambda: 42, sleeper=lambda _: None)
        self.assertTrue(attempt.ok)
        self.assertEqual(attempt.value, 42)
        self.assertEqual(attempt.attempts, 1)
        self.assertEqual(attempt.retry_note, "")

    def test_retries_then_succeeds(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("boom")
            return "ok"

        attempt = retry_call(flaky, attempts=3, sleeper=lambda _: None)
        self.assertTrue(attempt.ok)
        self.assertEqual(attempt.attempts, 3)
        self.assertEqual(attempt.retry_note, "（重试 2 次后成功）")

    def test_gives_up_after_all_attempts(self):
        def always_fail():
            raise RuntimeError("boom")

        attempt = retry_call(always_fail, attempts=2, sleeper=lambda _: None)
        self.assertFalse(attempt.ok)
        self.assertIn("boom", attempt.error)
        self.assertEqual(attempt.attempts, 2)

    def test_backoff_is_capped(self):
        delays: list[float] = []

        def always_fail():
            raise RuntimeError("x")

        retry_call(always_fail, attempts=4, base_delay=1.0, max_delay=2.0, sleeper=delays.append)
        self.assertEqual(delays, [1.0, 2.0, 2.0])


class TestInvokeLlm(unittest.TestCase):
    def test_returns_text(self):
        attempt = invoke_llm(ScriptedModel(["hello"]), [], attempts=1)
        self.assertTrue(attempt.ok)
        self.assertEqual(attempt.value, "hello")

    @patch.object(config, "NODE_RETRY_BASE_DELAY", 0.0)
    def test_returns_error_instead_of_raising(self):
        model = BoomModel("模型挂了")
        attempt = invoke_llm(model, [], attempts=3)
        self.assertFalse(attempt.ok)
        self.assertIn("模型挂了", attempt.error)
        self.assertEqual(model.calls, 3)


class TestCallTool(unittest.TestCase):
    def test_returns_text_on_success(self):
        class OkTool:
            def invoke(self, payload):
                return "分析完成"

        text, error = call_tool(OkTool(), {})
        self.assertEqual(text, "分析完成")
        self.assertIsNone(error)

    @patch.object(config, "NODE_RETRY_BASE_DELAY", 0.0)
    def test_swallows_exception(self):
        class BoomTool:
            def invoke(self, payload):
                raise RuntimeError("工具炸了")

        text, error = call_tool(BoomTool(), {})
        self.assertIn("工具执行失败", text)
        self.assertIn("工具炸了", error or "")


class TestErrorHelpers(unittest.TestCase):
    def test_fatal_detection(self):
        errors = [
            make_error("reviewer", "tool_error", "降级", fatal=False),
            make_error("coder", "llm_error", "挂了", fatal=True),
        ]
        self.assertTrue(has_fatal_error(errors))
        self.assertFalse(has_fatal_error([errors[0]]))
        self.assertFalse(has_fatal_error([]))
        self.assertFalse(has_fatal_error(None))

    def test_error_carries_timestamp(self):
        error = make_error("fixer", "self_check", "自验失败", fatal=False)
        self.assertEqual(error["scope"], "fixer")
        self.assertTrue(error["created_at"])


class RequestTimeoutError(Exception):
    """名字里带 Timeout，模拟 openai.APITimeoutError 这类异常。"""


class TestTimeoutClassification(unittest.TestCase):
    def test_builtin_timeout_detected(self):
        self.assertTrue(is_timeout_error(TimeoutError("slow")))

    def test_openai_style_timeout_detected(self):
        self.assertTrue(is_timeout_error(RequestTimeoutError("Request timed out")))

    def test_other_errors_are_not_timeout(self):
        self.assertFalse(is_timeout_error(RuntimeError("boom")))


class TestTimeoutRetryPolicy(unittest.TestCase):
    def test_invoke_llm_does_not_retry_timeout(self):
        """推理模型的超时通常是"本来就慢"，重试只会推迟失败。"""

        class SlowModel:
            def __init__(self):
                self.calls = 0

            def invoke(self, messages):
                self.calls += 1
                raise RequestTimeoutError("Request timed out")

        model = SlowModel()
        attempt = invoke_llm(model, [], attempts=3)

        self.assertFalse(attempt.ok)
        self.assertEqual(model.calls, 1)
        self.assertEqual(attempt.attempts, 1)

    @patch.object(config, "NODE_RETRY_BASE_DELAY", 0.0)
    def test_non_timeout_errors_are_still_retried(self):
        model = BoomModel("500 server error")
        attempt = invoke_llm(model, [], attempts=3)
        self.assertEqual(model.calls, 3)
        self.assertFalse(attempt.ok)

    def test_retry_call_can_opt_out(self):
        calls = {"n": 0}

        def fail():
            calls["n"] += 1
            raise RuntimeError("x")

        attempt = retry_call(
            fail, attempts=5, sleeper=lambda _: None, retry_if=lambda exc: False
        )
        self.assertEqual(calls["n"], 1)
        self.assertEqual(attempt.attempts, 1)


if __name__ == "__main__":
    unittest.main()
