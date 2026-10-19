"""执行器 + 运行记录测试：事件采集、JSON 落盘、断点续跑。"""

import tempfile
import unittest
from pathlib import Path

from multi_agent.graph import build_workflow
from multi_agent.recorder import RunRecorder, load_record, plan_resume
from multi_agent.runner import run_workflow, terminal_of
from tests.helpers import ScriptedModel


def _app(reviewer_responses, code="def f():\n    return 1", responder_responses=None):
    """所有 LLM 都注入假模型，避免测试打到真实接口。"""
    return build_workflow(
        coder_llm=ScriptedModel([code]),
        reviewer_llm=ScriptedModel(reviewer_responses),
        fixer_llm=ScriptedModel([code]),
        router_llm=ScriptedModel(["code"]),
        responder_llm=ScriptedModel(responder_responses or [""]),
    )


ONE_FINDING = '[{"severity": "warning", "location": "f", "issue": "缺注释", "suggestion": "补"}]'


class TestRunWorkflow(unittest.TestCase):
    def test_collects_events_and_metrics(self):
        events: list[tuple[str, object]] = []
        result = run_workflow(
            _app(["[]"]),
            {"requirement": "r", "iteration": 0, "max_iterations": 3},
            on_event=lambda kind, payload: events.append((kind, payload)),
        )

        self.assertEqual(result.terminal, "passed")
        self.assertTrue(result.ok)
        self.assertEqual(result.node_calls.get("coder"), 1)
        self.assertEqual(result.node_calls.get("reviewer"), 1)
        self.assertNotIn("fixer", result.node_calls)
        self.assertTrue(any(kind == "message" for kind, _ in events))
        self.assertTrue(any(kind == "log" for kind, _ in events))
        self.assertTrue(any(kind == "node" for kind, _ in events))

    def test_terminal_failed_when_findings_remain(self):
        result = run_workflow(
            _app([ONE_FINDING, ONE_FINDING, ONE_FINDING]),
            {"requirement": "r", "iteration": 0, "max_iterations": 1},
        )
        self.assertEqual(result.terminal, "failed")
        self.assertFalse(result.ok)
        self.assertTrue(result.state["findings"])

    def test_writes_json_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = RunRecorder("t1", "需求", directory=tmp, max_iterations=3)
            run_workflow(
                _app([ONE_FINDING, "[]"]),
                {"requirement": "需求", "iteration": 0, "max_iterations": 3},
                recorder=recorder,
            )

            document = load_record(Path(tmp) / "t1.json")
            self.assertEqual(document["run_id"], "t1")
            self.assertEqual(document["requirement"], "需求")
            self.assertEqual(document["result"]["terminal"], "passed")
            self.assertEqual(document["result"]["node_calls"]["fixer"], 1)
            self.assertEqual(document["last_node"], "reviewer")
            self.assertTrue(document["timeline"])
            self.assertIn("current_code", document["snapshot"])
            self.assertTrue(document["snapshot"]["messages"])

    def test_record_timeline_keeps_logs_and_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = RunRecorder("t2", "需求", directory=tmp, max_iterations=3)
            run_workflow(
                _app(["[]"]),
                {"requirement": "需求", "iteration": 0, "max_iterations": 3},
                recorder=recorder,
            )
            document = load_record(Path(tmp) / "t2.json")
            nodes = [entry["node"] for entry in document["timeline"]]
            self.assertEqual(nodes, ["router", "coder", "reviewer"])
            self.assertTrue(document["timeline"][0]["log"])
            self.assertTrue(document["timeline"][0]["messages"])


class TestResume(unittest.TestCase):
    def test_plan_resume_points_to_reviewer_after_coder(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = RunRecorder("r1", "需求", directory=tmp, max_iterations=3)
            recorder.record_node("coder", {"status": "coding", "log": ["ok"], "messages": []})
            recorder.update_snapshot(
                {
                    "requirement": "需求",
                    "current_code": "def f():\n    return 1",
                    "iteration": 0,
                    "max_iterations": 3,
                    "findings": [],
                }
            )
            state, entry, _ = plan_resume(Path(tmp) / "r1.json")

        self.assertEqual(entry, "reviewer")
        self.assertIn("return 1", state["current_code"])

    def test_plan_resume_points_to_fixer_after_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = RunRecorder("r2", "需求", directory=tmp, max_iterations=3)
            recorder.record_node("reviewer", {"status": "reviewing", "log": [], "messages": []})
            recorder.update_snapshot(
                {
                    "requirement": "需求",
                    "current_code": "def f():\n    return 1",
                    "iteration": 0,
                    "max_iterations": 3,
                    "findings": [{"severity": "warning", "issue": "x"}],
                }
            )
            _, entry, _ = plan_resume(Path(tmp) / "r2.json")

        self.assertEqual(entry, "fixer")

    def test_plan_resume_returns_none_when_finished(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = RunRecorder("r3", "需求", directory=tmp, max_iterations=3)
            recorder.record_node("reviewer", {"status": "reviewing", "log": [], "messages": []})
            recorder.update_snapshot(
                {"findings": [], "iteration": 0, "max_iterations": 3}
            )
            _, entry, _ = plan_resume(Path(tmp) / "r3.json")

        self.assertIsNone(entry)

    def test_resumed_run_continues_from_entry(self):
        """从 reviewer 入口续跑，能正常跑完并给出结论。"""
        with tempfile.TemporaryDirectory() as tmp:
            recorder = RunRecorder("r4", "需求", directory=tmp, max_iterations=3)
            recorder.record_node("coder", {"status": "coding", "log": [], "messages": []})
            recorder.update_snapshot(
                {
                    "requirement": "需求",
                    "current_code": "def f():\n    return 1",
                    "iteration": 0,
                    "max_iterations": 3,
                    "findings": [],
                }
            )
            state, entry, _ = plan_resume(Path(tmp) / "r4.json")

        app = build_workflow(
            entry=entry,
            coder_llm=ScriptedModel([""]),
            reviewer_llm=ScriptedModel(["[]"]),
            fixer_llm=ScriptedModel([""]),
            router_llm=ScriptedModel(["code"]),
            responder_llm=ScriptedModel([""]),
        )
        result = run_workflow(app, state)

        self.assertEqual(result.terminal, "passed")
        self.assertNotIn("coder", result.node_calls)
        self.assertEqual(result.node_calls["reviewer"], 1)


class TestTerminalOf(unittest.TestCase):
    def test_passed(self):
        self.assertEqual(terminal_of({"findings": []}), "passed")

    def test_failed_with_blocking_findings(self):
        self.assertEqual(terminal_of({"findings": [{"severity": "warning"}]}), "failed")

    def test_passed_with_info_only_findings(self):
        state = {"findings": [{"severity": "info", "issue": "建议"}]}
        self.assertEqual(terminal_of(state), "passed")

    def test_answered(self):
        state = {"status": "answered", "answer": "我可以…"}
        self.assertEqual(terminal_of(state), "answered")

    def test_failed_with_fatal_error(self):
        state = {"findings": [], "errors": [{"fatal": True, "message": "挂了"}]}
        self.assertEqual(terminal_of(state), "failed")

    def test_degraded_error_still_passes(self):
        state = {"findings": [], "errors": [{"fatal": False, "message": "降级"}]}
        self.assertEqual(terminal_of(state), "passed")


if __name__ == "__main__":
    unittest.main()
