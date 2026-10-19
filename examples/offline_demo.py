#!/usr/bin/env python3
"""离线演示：不需要 API Key、不联网，也能看到完整流程跑通。

用法：
    python examples/offline_demo.py

演两条分支（跑的是和生产完全一样的图、节点、条件边与运行记录）：
1. 代码任务：编码 → 审查 →（修复 → 复审）→ 通过
2. 自由提问："你能做什么" → 直接得到回答，不会被塞进代码流水线

只把 LLM 换成脚本化假模型，所以输出可复现、也不烧 token。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage  # noqa: E402

from multi_agent.graph import build_workflow  # noqa: E402
from multi_agent.messages import format_message  # noqa: E402
from multi_agent.runner import run_workflow  # noqa: E402


class ScriptedModel:
    """按脚本依次返回预设内容，用来模拟一个真实模型。"""

    def __init__(self, name: str, responses: list[str]) -> None:
        self.name = name
        self._responses = list(responses)
        self.calls = 0

    def invoke(self, messages: Any) -> AIMessage:
        self.calls += 1
        index = min(self.calls - 1, len(self._responses) - 1)
        return AIMessage(content=self._responses[index])


BUGGY_CODE = '''def average(values):
    return sum(values) / len(values)


print(average([1, 2, 3]))
'''

FIXED_CODE = '''def average(values):
    """求平均值；空列表返回 0。"""
    if not values:
        return 0
    return sum(values) / len(values)


print(average([1, 2, 3]))
'''

FINDING = (
    '[{"severity": "critical", "location": "average", '
    '"issue": "空列表会触发 ZeroDivisionError", '
    '"suggestion": "调用 len/sum 之前先判断 values 是否为空"}]'
)

QUESTION_ANSWER = """我能做这几件事：

1. 写代码：给一句中文需求，我就走「编码 → 审查 → 修复 → 复审」，
   产出完整可运行的 Python 代码。
2. 真验证：审查会做 AST 静态分析，并在受限环境里真跑一遍。
3. 修完自验：fixer 改完自己跑一次，不过关就带着错误再修一次。
4. 全程可追踪：每一轮都写进 runs/<run_id>.json，可以 /resume 续跑。
5. 自由问答：也就是现在这个模式，可以问概念、问设计、问用法。"""


def render(kind: str, payload: Any) -> None:
    if kind == "node":
        print(f"\n▶ {payload}")
    elif kind == "log":
        print(f"    {payload}")
    elif kind == "message":
        print(f"    ✉ {format_message(payload)}")


def _app() -> Any:
    models = {
        "router": ScriptedModel("router", ["code", "chat"]),
        "coder": ScriptedModel("coder", [BUGGY_CODE]),
        "reviewer": ScriptedModel("reviewer", [FINDING, "[]"]),
        "fixer": ScriptedModel("fixer", [FIXED_CODE]),
        "responder": ScriptedModel("responder", [QUESTION_ANSWER]),
    }
    app = build_workflow(
        router_llm=models["router"],
        coder_llm=models["coder"],
        reviewer_llm=models["reviewer"],
        fixer_llm=models["fixer"],
        responder_llm=models["responder"],
    )
    return app, models


def _state(requirement: str, run_id: str) -> dict[str, Any]:
    return {
        "requirement": requirement,
        "iteration": 0,
        "max_iterations": 3,
        "run_id": run_id,
        "findings": [],
        "messages": [],
        "errors": [],
        "log": [],
        "code_history": [],
        "review_history": [],
    }


def _run_scenario(app: Any, requirement: str, run_id: str, title: str) -> Any:
    print("\n" + "#" * 66)
    print(f"# {title}")
    print(f"# 输入：{requirement}")
    print("#" * 66)
    result = run_workflow(app, _state(requirement, run_id), on_event=render)
    state = result.state

    print("\n" + "-" * 66)
    print(f"结论        ：{result.terminal}")
    print(f"模式        ：{state.get('mode')}")
    print(f"节点调用    ：{result.node_calls}")
    print(f"Agent 消息  ：{len(state.get('messages') or [])} 条")
    if result.terminal == "answered":
        print("\n回答：")
        print(state.get("answer", ""))
    else:
        print(f"修复轮次    ：{state.get('iteration', 0)}")
        print(f"剩余问题    ：{len(state.get('findings') or [])}")
        print(f"代码版本    ：{[item['author'] for item in state.get('code_history') or []]}")
        print(f"审查快照    ：{state.get('review_history')}")
        degraded = [item for item in state.get("errors") or [] if not item.get("fatal")]
        print(f"降级记录    ：{len(degraded)} 条")
        print("\n最终代码：")
        print(state.get("current_code", ""))
    return result


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    app, _models = _app()

    first = _run_scenario(
        app,
        "写一个求平均值的函数，要处理空列表",
        "demo-code",
        "第 1 幕：代码任务（编码 → 审查 → 修复 → 复审）",
    )
    second = _run_scenario(
        app,
        "你能做什么",
        "demo-chat",
        "第 2 幕：自由提问（直接回答，不走代码流水线）",
    )

    print("\n" + "=" * 66)
    print("两条分支都跑通了：代码任务走流水线，自由提问直接回答。")
    return 0 if (first.ok and second.ok) else 1


if __name__ == "__main__":
    sys.exit(main())
