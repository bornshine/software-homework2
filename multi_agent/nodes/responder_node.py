"""问答节点：回答自由提问（不走代码流水线）。

和 coder 的区别：这里产出的是自然语言回答，不进入审查/修复循环。
模型默认复用 coder 的配置（快），角色与模型解耦。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from multi_agent import llm_factory
from multi_agent.messages import KIND_ERROR, KIND_VERDICT, RESPONDER, WORKFLOW, make_message
from multi_agent.prompts import RESPONDER_SYSTEM_PROMPT
from multi_agent.resilience import invoke_llm, make_error
from multi_agent.state import WorkflowState
from multi_agent.text_utils import strip_model_artifacts

EMPTY_HINT = (
    "请直接说出你的需求或问题，例如：\n"
    "- 代码任务：写一个函数 median(values)，返回列表的中位数\n"
    "- 自由提问：你能做什么"
)


def make_responder_node(llm: Any | None = None):
    """构造问答节点；llm 为 None 时用 coder 的模型配置。"""

    def node(state: WorkflowState) -> dict:
        return run_responder(state, llm)

    return node


def run_responder(state: WorkflowState, llm: Any | None = None) -> dict:
    """回答用户提问。"""
    question = (state.get("requirement") or "").strip()
    if not question:
        return _answered(EMPTY_HINT, note="输入为空，已返回用法提示")

    if llm is None:
        try:
            llm = llm_factory.get_coder_llm()
        except Exception as exc:  # noqa: BLE001 - 配置缺失时降级为致命错误
            return _fail(f"问答模型不可用：{type(exc).__name__}: {exc}")

    attempt = invoke_llm(
        llm,
        [
            SystemMessage(content=RESPONDER_SYSTEM_PROMPT),
            HumanMessage(content=question),
        ],
    )
    if not attempt.ok:
        return _fail(f"问答模型调用失败：{attempt.error}")

    answer = strip_model_artifacts(attempt.value).strip()
    if not answer:
        return _fail("问答模型返回了空内容")

    return _answered(answer, note=f"已回答（{len(answer)} 字符{attempt.retry_note}）")


def _answered(answer: str, *, note: str) -> dict:
    return {
        "answer": answer,
        "status": "answered",
        "log": [f"[responder] {note}"],
        "messages": [
            make_message(
                RESPONDER,
                WORKFLOW,
                KIND_VERDICT,
                "已给出回答",
                payload={"chars": len(answer)},
            )
        ],
    }


def _fail(message: str) -> dict:
    return {
        "status": "failed",
        "errors": [make_error(RESPONDER, "llm_error", message, fatal=True)],
        "log": [f"[responder] 失败：{message}"],
        "messages": [make_message(RESPONDER, WORKFLOW, KIND_ERROR, message)],
    }


# 不注入模型时的默认节点
responder_node = make_responder_node()
