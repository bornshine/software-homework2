"""节点行为测试：审查降级、fixer 自验与自修复、跳过逻辑。"""

import unittest
from unittest.mock import patch

from multi_agent.nodes import run_fixer, run_responder, run_reviewer, run_router
from tests.helpers import BoomModel, ScriptedModel


def _exec_result(**overrides):
    base = {
        "ok": False,
        "skipped": False,
        "timed_out": False,
        "policy_reason": "",
        "error": "",
        "returncode": 1,
        "stdout": "",
        "stderr": "ValueError: boom",
    }
    base.update(overrides)
    return base


class TestReviewerNode(unittest.TestCase):
    @patch("multi_agent.nodes.reviewer_node.analyze_code")
    def test_degrades_when_ast_tool_fails(self, mock_analyze):
        mock_analyze.side_effect = RuntimeError("AST 挂了")
        update = run_reviewer(
            {"current_code": "def f():\n    return 1", "iteration": 0},
            ScriptedModel(["[]"]),
        )

        self.assertEqual(update["status"], "reviewing")
        self.assertEqual(update["findings"], [])
        # 工具挂掉只是可恢复错误，不能是致命的
        self.assertTrue(update["errors"])
        self.assertTrue(all(not item["fatal"] for item in update["errors"]))
        # 结论里要能看到"工具降级"
        verdict = update["messages"][-1]
        self.assertEqual(verdict["kind"], "verdict")
        self.assertIn("降级", verdict["summary"])

    def test_skips_when_upstream_failed(self):
        model = ScriptedModel(["[]"])
        update = run_reviewer({"status": "failed", "iteration": 1}, model)
        self.assertEqual(model.calls, 0)
        self.assertIn("跳过审查", update["messages"][0]["summary"])

    def test_fatal_when_code_empty(self):
        model = ScriptedModel(["[]"])
        update = run_reviewer({"current_code": "   ", "iteration": 0}, model)
        self.assertEqual(update["status"], "failed")
        self.assertTrue(update["errors"][0]["fatal"])
        self.assertEqual(model.calls, 0)

    def test_findings_wrapped_in_object_are_parsed(self):
        update = run_reviewer(
            {"current_code": "def f():\n    return 1", "iteration": 0},
            ScriptedModel(['{"findings": [{"severity": "warning", "issue": "缺注释"}]}']),
        )
        self.assertEqual(len(update["findings"]), 1)
        self.assertEqual(update["findings"][0]["issue"], "缺注释")


class TestFixerNode(unittest.TestCase):
    def test_self_repairs_once_after_failed_verification(self):
        model = ScriptedModel(["def f():\n    return 1", "def f():\n    return 2"])
        with patch(
            "multi_agent.nodes.fixer_node.execute_code",
            side_effect=[_exec_result(), _exec_result(ok=True, returncode=0, stderr="")],
        ):
            update = run_fixer(
                {
                    "current_code": "def f():\n    return 0",
                    "findings": [{"severity": "critical", "issue": "返回值不对"}],
                    "iteration": 0,
                },
                model,
            )

        self.assertEqual(model.calls, 2)
        self.assertTrue(update["self_check"]["ok"])
        self.assertEqual(update["self_check"]["attempts"], 2)
        self.assertEqual(update["iteration"], 1)
        self.assertIn("return 2", update["current_code"])

    def test_does_not_retry_when_env_unavailable(self):
        model = ScriptedModel(["def f():\n    return 1"])
        env_failure = _exec_result(error="无法创建临时目录：PermissionError")
        with patch("multi_agent.nodes.fixer_node.execute_code", return_value=env_failure):
            update = run_fixer(
                {
                    "current_code": "def f():\n    return 0",
                    "findings": [{"severity": "critical", "issue": "x"}],
                    "iteration": 0,
                },
                model,
            )

        self.assertEqual(model.calls, 1)          # 环境问题不值得再叫一次模型
        self.assertFalse(update["self_check"]["ok"])
        self.assertIn("自验环境不可用", update["self_check"]["detail"])
        self.assertTrue(update["errors"])
        self.assertFalse(update["errors"][0]["fatal"])
        self.assertEqual(update["iteration"], 1)  # 仍然交付给 reviewer 复审

    def test_skips_without_findings(self):
        model = ScriptedModel(["def f():\n    return 1"])
        update = run_fixer(
            {"current_code": "def f():\n    return 1", "findings": [], "iteration": 0}, model
        )
        self.assertEqual(model.calls, 0)
        self.assertNotIn("current_code", update)

    def test_fatal_when_model_down(self):
        model = ScriptedModel(["def f():\n    return 1"])
        model.invoke = lambda messages: (_ for _ in ()).throw(RuntimeError("模型挂了"))
        with patch("config.NODE_MAX_ATTEMPTS", 1):
            update = run_fixer(
                {
                    "current_code": "def f():\n    return 0",
                    "findings": [{"severity": "warning", "issue": "x"}],
                    "iteration": 0,
                },
                model,
            )
        self.assertEqual(update["status"], "failed")
        self.assertTrue(update["errors"][0]["fatal"])


class TestRouterNode(unittest.TestCase):
    def test_rule_decides_without_calling_llm(self):
        model = BoomModel("不应该被调用")
        update = run_router({"requirement": "你能做什么"}, model)
        self.assertEqual(update["mode"], "chat")
        self.assertEqual(model.calls, 0)
        self.assertEqual(update["messages"][0]["recipient"], "responder")

    def test_code_task_routes_to_coder(self):
        update = run_router({"requirement": "写一个函数 add(a, b)"}, ScriptedModel(["chat"]))
        self.assertEqual(update["mode"], "code")
        self.assertEqual(update["messages"][0]["recipient"], "coder")

    def test_explicit_mode_wins(self):
        update = run_router(
            {"requirement": "你能做什么", "mode": "code"}, ScriptedModel(["chat"])
        )
        self.assertEqual(update["mode"], "code")


class TestResponderNode(unittest.TestCase):
    def test_answers_question(self):
        update = run_responder(
            {"requirement": "你能做什么"},
            ScriptedModel(["我能生成代码、审查代码、修复代码。"]),
        )
        self.assertEqual(update["status"], "answered")
        self.assertIn("我能生成代码", update["answer"])

    def test_strips_control_tags(self):
        update = run_responder(
            {"requirement": "你能做什么"}, ScriptedModel(["答案在这里\n</parameter>"])
        )
        self.assertEqual(update["answer"], "答案在这里")

    def test_empty_input_returns_hint_without_calling_model(self):
        model = ScriptedModel([""])
        update = run_responder({"requirement": "   "}, model)
        self.assertEqual(model.calls, 0)
        self.assertIn("请直接说出", update["answer"])

    def test_fatal_when_model_returns_empty(self):
        update = run_responder({"requirement": "你能做什么"}, ScriptedModel([""]))
        self.assertEqual(update["status"], "failed")
        self.assertTrue(update["errors"][0]["fatal"])


if __name__ == "__main__":
    unittest.main()
