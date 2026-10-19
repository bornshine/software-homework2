"""工作模式对话层：记忆裁剪、提案解析、ReAct 回路、结果回灌。"""

import unittest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from multi_agent.conversation import (
    PROPOSE_TOOL_NAME,
    build_conversation_agent,
    build_result_note,
    create_checkpointer,
    extract_proposal,
    inject_system_note,
    run_turn,
    trim_history,
)
from tests.helpers import ToolCallingModel, tool_call


class TestTrimHistory(unittest.TestCase):
    def test_short_history_untouched(self):
        messages = [HumanMessage(content="a")]
        self.assertEqual(trim_history(messages, 10), messages)

    def test_limit_zero_keeps_everything(self):
        messages = [HumanMessage(content="a")] * 3
        self.assertEqual(len(trim_history(messages, 0)), 3)

    def test_trim_aligned_to_human_message(self):
        """不能以孤儿 ToolMessage 开头（否则接口报 400）。"""
        messages = [
            ToolMessage(content="x", tool_call_id="1"),
            HumanMessage(content="a"),
            AIMessage(content="b"),
        ]
        trimmed = trim_history(messages, 2)
        self.assertIsInstance(trimmed[0], HumanMessage)


class TestExtractProposal(unittest.TestCase):
    def test_no_proposal(self):
        self.assertIsNone(extract_proposal([AIMessage(content="你好")]))

    def test_proposal_from_tool_call(self):
        message = AIMessage(
            content="",
            tool_calls=[
                tool_call(
                    PROPOSE_TOOL_NAME,
                    {"requirement": "写一个 median 函数", "summary": "单文件"},
                )
            ],
        )
        proposal = extract_proposal([message])
        self.assertIsNotNone(proposal)
        self.assertEqual(proposal.requirement, "写一个 median 函数")
        self.assertEqual(proposal.summary, "单文件")
        self.assertEqual(proposal.source, "model")

    def test_args_may_be_json_string(self):
        message = AIMessage(
            content="",
            tool_calls=[tool_call(PROPOSE_TOOL_NAME, {"requirement": "x"})],
        )
        message.tool_calls[0]["args"] = '{"requirement": "x"}'
        self.assertEqual(extract_proposal([message]).requirement, "x")

    def test_empty_requirement_ignored(self):
        message = AIMessage(content="", tool_calls=[tool_call(PROPOSE_TOOL_NAME, {})])
        self.assertIsNone(extract_proposal([message]))

    def test_last_proposal_wins(self):
        first = AIMessage(
            content="", tool_calls=[tool_call(PROPOSE_TOOL_NAME, {"requirement": "先的方案"}, "c1")]
        )
        second = AIMessage(
            content="", tool_calls=[tool_call(PROPOSE_TOOL_NAME, {"requirement": "后的方案"}, "c2")]
        )
        self.assertEqual(extract_proposal([first, second]).requirement, "后的方案")


class TestRunTurn(unittest.TestCase):
    def test_answer_only(self):
        model = ToolCallingModel([AIMessage(content="你好呀")])
        app = build_conversation_agent(llm=model, tools=[], checkpointer=create_checkpointer())

        turn = run_turn(app, "你好", "t-answer")

        self.assertEqual(turn.answer, "你好呀")
        self.assertIsNone(turn.proposal)
        self.assertFalse(turn.used_tools)

    def test_tool_round_trip(self):
        @tool("dummy")
        def dummy(text: str) -> str:
            """回声工具"""
            return f"echo:{text}"

        model = ToolCallingModel(
            [
                AIMessage(content="", tool_calls=[tool_call("dummy", {"text": "hi"})]),
                AIMessage(content="工具返回了 echo:hi"),
            ]
        )
        app = build_conversation_agent(
            llm=model, tools=[dummy], checkpointer=create_checkpointer()
        )
        events: list[tuple[str, object]] = []

        turn = run_turn(app, "试试工具", "t-tool", on_event=lambda k, p: events.append((k, p)))

        self.assertEqual(turn.answer, "工具返回了 echo:hi")
        self.assertIn("dummy", turn.tool_names)
        self.assertTrue(turn.used_tools)
        self.assertTrue(any(kind == "tool_call" for kind, _ in events))
        self.assertTrue(any(kind == "tool_result" for kind, _ in events))

    def test_propose_tool_is_always_bound(self):
        model = ToolCallingModel([AIMessage(content="ok")])
        build_conversation_agent(llm=model, tools=[], checkpointer=create_checkpointer())
        names = [getattr(item, "name", "") for item in model.bound_tools]
        self.assertIn(PROPOSE_TOOL_NAME, names)

    def test_memory_keeps_context_between_turns(self):
        model = ToolCallingModel([AIMessage(content="第一次"), AIMessage(content="第二次")])
        app = build_conversation_agent(llm=model, tools=[], checkpointer=create_checkpointer())
        thread = "t-memory"

        run_turn(app, "第一个问题", thread)
        run_turn(app, "第二个问题", thread)

        state = app.get_state({"configurable": {"thread_id": thread}})
        humans = [m for m in state.values["messages"] if isinstance(m, HumanMessage)]
        self.assertEqual([m.content for m in humans], ["第一个问题", "第二个问题"])

    def test_threads_are_isolated(self):
        model = ToolCallingModel([AIMessage(content="a"), AIMessage(content="b")])
        app = build_conversation_agent(llm=model, tools=[], checkpointer=create_checkpointer())

        run_turn(app, "线程一的输入", "t-a")
        run_turn(app, "线程二的输入", "t-b")

        state_a = app.get_state({"configurable": {"thread_id": "t-a"}})
        humans = [m.content for m in state_a.values["messages"] if isinstance(m, HumanMessage)]
        self.assertEqual(humans, ["线程一的输入"])

    def test_proposal_round_trip(self):
        model = ToolCallingModel(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        tool_call(PROPOSE_TOOL_NAME, {"requirement": "写个 median 函数"})
                    ],
                ),
                AIMessage(content="已经把提案给你了"),
            ]
        )
        app = build_conversation_agent(llm=model, tools=[], checkpointer=create_checkpointer())

        turn = run_turn(app, "帮我写个 median", "t-propose")

        self.assertIsNotNone(turn.proposal)
        self.assertEqual(turn.proposal.requirement, "写个 median 函数")
        self.assertIn(PROPOSE_TOOL_NAME, turn.tool_names)


class TestResultNote(unittest.TestCase):
    def test_lists_advice_and_code(self):
        note = build_result_note(
            requirement="r",
            terminal="passed",
            iteration=1,
            findings=[{"severity": "info", "issue": "可以加注释"}],
            code="print(1)",
            record_path="runs/a.json",
        )
        self.assertIn("runs/a.json", note)
        self.assertIn("print(1)", note)
        self.assertIn("非阻断建议", note)

    def test_lists_blocking_findings(self):
        note = build_result_note(
            requirement="r",
            terminal="failed",
            iteration=2,
            findings=[{"severity": "critical", "issue": "空列表会除零"}],
            code="",
        )
        self.assertIn("阻断问题", note)
        self.assertIn("空列表会除零", note)

    def test_inject_system_note_enters_history(self):
        model = ToolCallingModel([AIMessage(content="ok")])
        app = build_conversation_agent(llm=model, tools=[], checkpointer=create_checkpointer())
        thread = "t-note"
        run_turn(app, "你好", thread)

        inject_system_note(app, thread, "流水线跑完了：passed")

        state = app.get_state({"configurable": {"thread_id": thread}})
        contents = [m.content for m in state.values["messages"] if isinstance(m, HumanMessage)]
        self.assertTrue(any("【系统】" in text and "passed" in text for text in contents))


if __name__ == "__main__":
    unittest.main()
