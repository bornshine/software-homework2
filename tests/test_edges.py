"""条件边路由测试：入口分支、审后分支、续跑推算。"""

import unittest

from multi_agent.edges import next_node_after, route_after_review, route_after_router
from multi_agent.state import WorkflowState


class TestRouteAfterRouter(unittest.TestCase):
    def test_chat_branch(self):
        self.assertEqual(route_after_router({"mode": "chat"}), "chat")

    def test_code_branch(self):
        self.assertEqual(route_after_router({"mode": "code"}), "code")

    def test_defaults_to_code(self):
        self.assertEqual(route_after_router({}), "code")


class TestRouteAfterReview(unittest.TestCase):
    def test_passed_when_no_findings(self):
        state: WorkflowState = {"findings": [], "iteration": 0}
        self.assertEqual(route_after_review(state), "passed")

    def test_fix_when_findings_and_under_limit(self):
        state: WorkflowState = {
            "findings": [{"severity": "critical", "issue": "越界"}],
            "iteration": 0,
            "max_iterations": 3,
        }
        self.assertEqual(route_after_review(state), "fix")

    def test_give_up_when_findings_and_over_limit(self):
        state: WorkflowState = {
            "findings": [{"severity": "critical", "issue": "越界"}],
            "iteration": 3,
            "max_iterations": 3,
        }
        self.assertEqual(route_after_review(state), "give_up")

    def test_fix_on_last_allowed_round(self):
        # iteration=2 且 max=3：还有最后一次修复机会
        state: WorkflowState = {
            "findings": [{"severity": "warning", "issue": "x"}],
            "iteration": 2,
            "max_iterations": 3,
        }
        self.assertEqual(route_after_review(state), "fix")

    def test_passed_when_findings_none(self):
        state: WorkflowState = {"findings": None, "iteration": 0}  # type: ignore[typeddict-item]
        self.assertEqual(route_after_review(state), "passed")

    def test_give_up_on_fatal_error(self):
        """致命错误不能被当成"审查通过"。"""
        state: WorkflowState = {
            "findings": [],
            "iteration": 0,
            "errors": [{"scope": "coder", "kind": "llm_error", "message": "挂了", "fatal": True}],
        }
        self.assertEqual(route_after_review(state), "give_up")

    def test_give_up_on_failed_status(self):
        state: WorkflowState = {"findings": [], "status": "failed"}
        self.assertEqual(route_after_review(state), "give_up")

    def test_degraded_error_does_not_block(self):
        """非致命（已降级）错误不应阻断流程。"""
        state: WorkflowState = {
            "findings": [],
            "iteration": 0,
            "errors": [{"scope": "reviewer", "kind": "tool_error", "message": "降级", "fatal": False}],
        }
        self.assertEqual(route_after_review(state), "passed")


class TestBlockingThreshold(unittest.TestCase):
    """通过 = 无阻断问题，不等于零建议。"""

    def test_info_only_passes(self):
        state: WorkflowState = {
            "findings": [
                {"severity": "info", "issue": "可以加注释"},
                {"severity": "info", "issue": "命名可以更好"},
            ],
            "iteration": 0,
            "max_iterations": 3,
        }
        self.assertEqual(route_after_review(state), "passed")

    def test_warning_still_blocks(self):
        state: WorkflowState = {
            "findings": [{"severity": "warning", "issue": "没处理边界"}],
            "iteration": 0,
            "max_iterations": 3,
        }
        self.assertEqual(route_after_review(state), "fix")

    def test_info_only_with_max_rounds_used_still_passes(self):
        """即使轮到上限，只要剩下的是非阻断建议，也算通过。"""
        state: WorkflowState = {
            "findings": [{"severity": "info", "issue": "建议"}],
            "iteration": 3,
            "max_iterations": 3,
        }
        self.assertEqual(route_after_review(state), "passed")

    def test_unknown_severity_is_treated_as_blocking(self):
        state: WorkflowState = {
            "findings": [{"severity": "high", "issue": "模型乱写级别"}],
            "iteration": 0,
            "max_iterations": 3,
        }
        self.assertEqual(route_after_review(state), "fix")


class TestNextNodeAfter(unittest.TestCase):
    def test_start_from_scratch(self):
        self.assertEqual(next_node_after(None, {}), "router")

    def test_after_router_code(self):
        self.assertEqual(next_node_after("router", {"mode": "code"}), "coder")

    def test_after_router_chat(self):
        self.assertEqual(next_node_after("router", {"mode": "chat"}), "responder")

    def test_after_responder_is_terminal(self):
        self.assertIsNone(next_node_after("responder", {"mode": "chat"}))

    def test_after_coder(self):
        self.assertEqual(next_node_after("coder", {}), "reviewer")

    def test_after_fixer(self):
        self.assertEqual(next_node_after("fixer", {}), "reviewer")

    def test_after_reviewer_with_findings(self):
        state: WorkflowState = {
            "findings": [{"severity": "critical", "issue": "x"}],
            "iteration": 0,
            "max_iterations": 3,
        }
        self.assertEqual(next_node_after("reviewer", state), "fixer")

    def test_after_reviewer_passed(self):
        self.assertIsNone(next_node_after("reviewer", {"findings": [], "iteration": 0}))

    def test_after_reviewer_info_only(self):
        state: WorkflowState = {
            "findings": [{"severity": "info", "issue": "x"}],
            "iteration": 0,
            "max_iterations": 3,
        }
        self.assertIsNone(next_node_after("reviewer", state))

    def test_after_reviewer_give_up(self):
        state: WorkflowState = {
            "findings": [{"severity": "critical", "issue": "x"}],
            "iteration": 3,
            "max_iterations": 3,
        }
        self.assertIsNone(next_node_after("reviewer", state))

    def test_unknown_node(self):
        self.assertIsNone(next_node_after("nope", {}))


if __name__ == "__main__":
    unittest.main()
