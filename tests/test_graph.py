"""端到端图测试：用脚本化假模型替换所有 LLM，不烧 token、不联网。"""

import unittest
from unittest.mock import patch

import config
from multi_agent.graph import build_workflow
from tests.helpers import BoomModel, ScriptedModel


def _app(*, coder, reviewer=None, fixer=None, router=None, responder=None, entry=None):
    """所有 LLM 位置都注入假模型，避免测试偷偷打到真实接口。"""
    kwargs = {
        "coder_llm": coder,
        "reviewer_llm": reviewer or ScriptedModel(["[]"]),
        "fixer_llm": fixer or ScriptedModel([""]),
        "router_llm": router or ScriptedModel(["code"]),
        "responder_llm": responder or ScriptedModel([""]),
    }
    if entry:
        kwargs["entry"] = entry
    return build_workflow(**kwargs)


def _state(requirement="写个函数", **extra):
    state = {"requirement": requirement, "iteration": 0, "max_iterations": 3}
    state.update(extra)
    return state


class TestCodePipeline(unittest.TestCase):
    def test_full_flow_passed(self):
        """coder 生成代码 → reviewer 返回空 findings → END。"""
        app = _app(coder=ScriptedModel(["def add(a, b):\n    return a + b"]))
        result = app.invoke(_state("写一个加法函数"))

        self.assertIn("def add", result["current_code"])
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["iteration"], 0)
        self.assertEqual(result["status"], "reviewing")
        self.assertEqual(result["mode"], "code")

    def test_fix_loop_then_pass(self):
        """coder 生成 → reviewer 报 1 个阻断问题 → fixer 修 → reviewer 再审通过。"""
        app = _app(
            coder=ScriptedModel(["def average(values):\n    return sum(values) / len(values)"]),
            reviewer=ScriptedModel(
                [
                    '[{"severity": "critical", "location": "average", '
                    '"issue": "空列表会除零", "suggestion": "加判断"}]',
                    "[]",
                ]
            ),
            fixer=ScriptedModel(
                [
                    "def average(values):\n    if not values:\n        return 0\n"
                    "    return sum(values) / len(values)"
                ]
            ),
        )
        result = app.invoke(_state("写一个求平均值的函数"))

        self.assertIn("if not values", result["current_code"])
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["iteration"], 1)

    def test_give_up_after_max_rounds(self):
        """reviewer 一直报阻断问题 → 超轮次 give_up。"""
        app = _app(
            coder=ScriptedModel(["def buggy():\n    return 1 / 0"]),
            reviewer=ScriptedModel(
                [
                    '[{"severity": "critical", "issue": "除零"}]',
                    '[{"severity": "critical", "issue": "还是除零"}]',
                    '[{"severity": "critical", "issue": "仍除零"}]',
                ]
            ),
            fixer=ScriptedModel(["def buggy():\n    return 1", "def buggy():\n    return 2"]),
        )
        result = app.invoke(_state("写个函数", max_iterations=2))

        self.assertEqual(result["iteration"], 2)
        self.assertTrue(len(result["findings"]) > 0)

    def test_info_only_findings_still_pass(self):
        """只有 info 级建议时算通过：建议照常记录，但不触发修复。"""
        app = _app(
            coder=ScriptedModel(["def f():\n    return 1"]),
            reviewer=ScriptedModel(
                ['[{"severity": "info", "location": "f", "issue": "可以加注释"}]']
            ),
            fixer=ScriptedModel([""]),  # 不应该被调用
        )
        result = app.invoke(_state("写个函数"))

        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["findings"][0]["severity"], "info")
        self.assertEqual(result["iteration"], 0)          # fixer 没跑
        self.assertEqual(result["status"], "reviewing")
        # 消息里应该是一条"通过 + 非阻断建议"，而不是请求修复
        kinds = [message["kind"] for message in result["messages"]]
        self.assertIn("verdict", kinds)
        self.assertNotIn("request_fix", kinds)

    def test_message_timeline_is_recorded(self):
        """Agent 之间的消息要能被完整追踪。"""
        app = _app(
            coder=ScriptedModel(["def f():\n    return 1"]),
            reviewer=ScriptedModel(
                [
                    '[{"severity": "warning", "location": "f", "issue": "缺文档", "suggestion": "补"}]',
                    "[]",
                ]
            ),
            fixer=ScriptedModel(['def f():\n    """doc"""\n    return 1']),
        )
        result = app.invoke(_state("写个函数"))

        pairs = [(m["sender"], m["recipient"]) for m in result["messages"]]
        self.assertIn(("router", "coder"), pairs)
        self.assertIn(("coder", "reviewer"), pairs)
        self.assertIn(("reviewer", "fixer"), pairs)
        self.assertIn(("fixer", "reviewer"), pairs)

        for message in result["messages"]:
            self.assertTrue(message["id"])
            self.assertTrue(message["created_at"])

    def test_review_history_snapshot(self):
        app = _app(
            coder=ScriptedModel(["def f():\n    return 1"]),
            reviewer=ScriptedModel(
                [
                    '[{"severity": "critical", "issue": "a"}, {"severity": "info", "issue": "b"}]',
                    "[]",
                ]
            ),
            fixer=ScriptedModel(["def f():\n    return 2"]),
        )
        result = app.invoke(_state("写个函数"))

        history = result["review_history"]
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["total"], 2)
        self.assertEqual(history[0]["critical"], 1)
        self.assertEqual(history[0]["info"], 1)
        self.assertEqual(history[0]["blocking"], 1)
        self.assertEqual(history[1]["total"], 0)

    def test_code_history_tracks_every_version(self):
        app = _app(
            coder=ScriptedModel(["def f():\n    return 1"]),
            reviewer=ScriptedModel(['[{"severity": "warning", "issue": "x"}]', "[]"]),
            fixer=ScriptedModel(["def f():\n    return 2"]),
        )
        result = app.invoke(_state("写个函数"))

        authors = [item["author"] for item in result["code_history"]]
        self.assertEqual(authors, ["coder", "fixer"])

    @patch.object(config, "NODE_MAX_ATTEMPTS", 1)
    def test_fatal_coder_error_stops_flow(self):
        """coder 挂掉时不能被误判成"审查通过"。"""
        app = _app(coder=BoomModel())
        result = app.invoke(_state("写个函数"))

        self.assertEqual(result["status"], "failed")
        self.assertTrue(any(item["fatal"] for item in result["errors"]))
        self.assertEqual(result["findings"], [])

    def test_model_can_be_injected_repeatedly_without_leaking(self):
        """连续两次 build 用不同假模型，互不干扰（旧实现会全局串味）。"""
        app_a = _app(coder=ScriptedModel(["def a():\n    return 'A'"]))
        app_b = _app(coder=ScriptedModel(["def b():\n    return 'B'"]))

        result_a = app_a.invoke(_state("写个函数"))
        result_b = app_b.invoke(_state("写个函数"))
        result_a_again = app_a.invoke(_state("写个函数"))

        self.assertIn("def a", result_a["current_code"])
        self.assertIn("def b", result_b["current_code"])
        self.assertIn("def a", result_a_again["current_code"])


class TestChatBranch(unittest.TestCase):
    def test_question_is_answered_without_code_pipeline(self):
        """“你能做什么”应该得到一段回答，而不是被塞进代码流水线。"""
        app = _app(
            coder=ScriptedModel(["def should_not_be_used():\n    return 1"]),
            responder=ScriptedModel(["我可以生成代码，也可以回答你的问题。"]),
        )
        result = app.invoke(_state("你能做什么"))

        self.assertEqual(result["mode"], "chat")
        self.assertEqual(result["status"], "answered")
        self.assertIn("可以生成代码", result["answer"])
        self.assertNotIn("current_code", result)      # 没有代码产物
        self.assertNotIn("findings", result)

    def test_router_message_points_to_responder(self):
        app = _app(
            coder=ScriptedModel([""]),
            responder=ScriptedModel(["回答内容"]),
        )
        result = app.invoke(_state("你是谁"))

        first = result["messages"][0]
        self.assertEqual(first["sender"], "router")
        self.assertEqual(first["recipient"], "responder")
        self.assertEqual(first["payload"]["mode"], "chat")

    def test_explicit_mode_overrides_rules(self):
        """--mode code 时，即使是"你能做什么"也走代码流水线。"""
        app = _app(coder=ScriptedModel(["def forced():\n    return 1"]))
        result = app.invoke(_state("你能做什么", mode="code"))

        self.assertEqual(result["mode"], "code")
        self.assertIn("def forced", result["current_code"])

    def test_chat_terminal_is_answered(self):
        from multi_agent.runner import run_workflow

        app = _app(
            coder=ScriptedModel([""]),
            responder=ScriptedModel(["我是这个流水线里的问答角色。"]),
        )
        result = run_workflow(app, _state("你能做什么"))

        self.assertEqual(result.terminal, "answered")
        self.assertTrue(result.ok)
        self.assertIn("responder", result.node_calls)
        self.assertNotIn("coder", result.node_calls)


if __name__ == "__main__":
    unittest.main()
