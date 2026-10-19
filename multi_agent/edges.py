"""条件边路由 + 断点续跑辅助。

两条分支：
- 入口路由 route_after_router：code → coder，chat → responder
- 审后路由 route_after_review：passed / fix / give_up

审后路由的关键点：**只有"阻断级别"的 findings 才算问题**。
分级由 config.BLOCKING_SEVERITIES 决定（默认 critical, warning）。
没有这个阈值时，模型审查会持续产出 style nit，流程永远收敛不到"通过"。
"""

from __future__ import annotations

from typing import Literal

import config
from multi_agent.intent import MODE_CHAT, MODE_CODE
from multi_agent.resilience import has_fatal_error
from multi_agent.severity import blocking_findings
from multi_agent.state import WorkflowState

RouteName = Literal["passed", "fix", "give_up"]
BranchName = Literal["code", "chat"]

NODE_ROUTER = "router"
NODE_CODER = "coder"
NODE_REVIEWER = "reviewer"
NODE_FIXER = "fixer"
NODE_RESPONDER = "responder"

ENTRY_NODES = (NODE_ROUTER, NODE_CODER, NODE_REVIEWER, NODE_FIXER, NODE_RESPONDER)


def route_after_router(state: WorkflowState) -> BranchName:
    """入口分支：代码任务走流水线，其它输入走问答。"""
    return MODE_CHAT if state.get("mode") == MODE_CHAT else MODE_CODE


def route_after_review(state: WorkflowState) -> RouteName:
    """审查完成后的路由函数。"""
    # 致命错误优先：上游模型/审查本身挂掉时不应该被当成"审查通过"
    if has_fatal_error(state.get("errors")) or state.get("status") == "failed":
        return "give_up"

    # 只剩非阻断建议（默认 info 级）→ 算通过，建议照样记录在 findings 里
    if not blocking_findings(state.get("findings")):
        return "passed"

    iteration = state.get("iteration", 0)
    max_iterations = state.get("max_iterations", config.MAX_FIX_ROUNDS)
    if iteration >= max_iterations:
        return "give_up"

    return "fix"


def next_node_after(last_node: str | None, state: WorkflowState) -> str | None:
    """根据最后一个完成的节点推算下一步；返回 None 表示已到终点。"""
    if not last_node:
        return NODE_ROUTER
    if last_node == NODE_ROUTER:
        return NODE_RESPONDER if route_after_router(state) == MODE_CHAT else NODE_CODER
    if last_node == NODE_CODER:
        return NODE_REVIEWER
    if last_node == NODE_FIXER:
        return NODE_REVIEWER
    if last_node == NODE_REVIEWER:
        return NODE_FIXER if route_after_review(state) == "fix" else None
    # responder 之后就是终点
    return None
