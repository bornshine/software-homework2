"""编码 Agent 节点（deepseek 系列）。

输入：state["requirement"]
输出：state["current_code"] / code_history / messages

节点由工厂函数构造：make_coder_node(llm) 支持注入假模型，
不再通过改写全局 llm_factory 属性做依赖注入（旧写法会污染后续构建）。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from multi_agent import llm_factory
from multi_agent.messages import (
    CODER,
    KIND_ERROR,
    KIND_HANDOFF,
    REVIEWER,
    WORKFLOW,
    make_message,
)
from multi_agent.prompts import CODER_SYSTEM_PROMPT
from multi_agent.resilience import invoke_llm, make_error
from multi_agent.state import WorkflowState
from multi_agent.text_utils import clean_code_output


def make_coder_node(llm: Any | None = None):
    """构造 coder 节点；llm 为 None 时在调用时从工厂获取。"""

    def node(state: WorkflowState) -> dict:
        return run_coder(state, llm)

    return node


def run_coder(state: WorkflowState, llm: Any | None = None) -> dict:
    """根据需求生成首版代码。"""
    requirement = (state.get("requirement") or "").strip()
    if not requirement:
        return _fail("需求为空，无法生成代码")

    if llm is None:
        try:
            llm = llm_factory.get_coder_llm()
        except Exception as exc:  # noqa: BLE001 - 配置缺失时降级为致命错误
            return _fail(f"编码模型不可用：{type(exc).__name__}: {exc}")

    attempt = invoke_llm(
        llm,
        [
            SystemMessage(content=CODER_SYSTEM_PROMPT),
            HumanMessage(content=f"需求：{requirement}"),
        ],
    )
    if not attempt.ok:
        return _fail(f"编码模型调用失败：{attempt.error}")

    code = clean_code_output(attempt.value)
    if not code.strip():
        return _fail("编码模型返回了空代码")

    return {
        "current_code": code,
        "status": "coding",
        "code_history": [
            {"iteration": 0, "author": CODER, "chars": len(code), "code": code}
        ],
        "log": [f"[coder] 生成代码 {len(code)} 字符{attempt.retry_note}"],
        "messages": [
            make_message(
                CODER,
                REVIEWER,
                KIND_HANDOFF,
                f"首版代码就绪（{len(code)} 字符）",
                iteration=0,
                payload={"chars": len(code)},
            )
        ],
    }


def _fail(message: str) -> dict:
    """构造一次致命失败的状态更新（条件边会据此直接终止流程）。"""
    return {
        "status": "failed",
        "errors": [make_error(CODER, "llm_error", message, fatal=True)],
        "log": [f"[coder] 失败：{message}"],
        "messages": [make_message(CODER, WORKFLOW, KIND_ERROR, message)],
    }


# 不注入模型时的默认节点（LangGraph 直接使用）
coder_node = make_coder_node()
