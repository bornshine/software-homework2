"""多 Agent 图组装。

图结构：
    START → router ──[code]──→ coder → reviewer ──[无阻断问题]──────→ END
                 │                      ├──[有阻断问题 & 未超轮次]──→ fixer ──→ reviewer
                 │                      └──[超轮次 / 致命错误]──────→ END
                 └──[chat]──→ responder ─────────────────────────────→ END

三个设计点：
1. 依赖注入走节点工厂参数，不再改写 llm_factory 的全局属性
   （旧写法会让一次 build 污染后续所有 build，测试之间互相串味）。
2. entry 参数允许图从任意节点开始，配合运行记录实现中断续跑。
3. 入口先过 router：要代码走流水线，其它提问走 responder，
   所以"你能做什么"这类输入不会被硬塞进"生成代码→审查"的固定格式里。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from multi_agent.edges import (
    ENTRY_NODES,
    NODE_CODER,
    NODE_FIXER,
    NODE_RESPONDER,
    NODE_REVIEWER,
    NODE_ROUTER,
    route_after_review,
    route_after_router,
)
from multi_agent.nodes import (
    make_coder_node,
    make_fixer_node,
    make_responder_node,
    make_reviewer_node,
    make_router_node,
)
from multi_agent.state import WorkflowState


def build_workflow(
    coder_llm: Any | None = None,
    reviewer_llm: Any | None = None,
    fixer_llm: Any | None = None,
    router_llm: Any | None = None,
    responder_llm: Any | None = None,
    entry: str = NODE_ROUTER,
    checkpointer: Any | None = None,
):
    """构建并编译协作图。

    - 五个 LLM 位置都可注入假模型；不传则运行时从工厂取
      （responder / router 默认复用 coder 的模型配置）
    - entry：起始节点（默认 router），用于断点续跑
    - checkpointer：可选 LangGraph checkpointer（默认不用，运行记录自己负责持久化）
    """
    if entry not in ENTRY_NODES:
        raise ValueError(f"entry 必须是 {ENTRY_NODES} 之一，收到：{entry!r}")

    workflow = StateGraph(WorkflowState)

    workflow.add_node(NODE_ROUTER, make_router_node(router_llm))
    workflow.add_node(NODE_RESPONDER, make_responder_node(responder_llm))
    workflow.add_node(NODE_CODER, make_coder_node(coder_llm))
    workflow.add_node(NODE_REVIEWER, make_reviewer_node(reviewer_llm))
    workflow.add_node(NODE_FIXER, make_fixer_node(fixer_llm))

    workflow.add_edge(START, entry)
    workflow.add_conditional_edges(
        NODE_ROUTER,
        route_after_router,
        {
            "code": NODE_CODER,
            "chat": NODE_RESPONDER,
        },
    )
    workflow.add_edge(NODE_RESPONDER, END)
    workflow.add_edge(NODE_CODER, NODE_REVIEWER)
    workflow.add_conditional_edges(
        NODE_REVIEWER,
        route_after_review,
        {
            "passed": END,
            "fix": NODE_FIXER,
            "give_up": END,
        },
    )
    workflow.add_edge(NODE_FIXER, NODE_REVIEWER)

    return workflow.compile(checkpointer=checkpointer)


def describe_workflow_graph(entry: str = NODE_ROUTER) -> str:
    """导出图结构的 Mermaid 文本（不需要真实 LLM）。"""

    class _StubModel:
        """结构图模式不会真正调用模型。"""

        def invoke(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("结构图模式不会真正调用模型。")

    app = build_workflow(
        coder_llm=_StubModel(),
        reviewer_llm=_StubModel(),
        fixer_llm=_StubModel(),
        router_llm=_StubModel(),
        responder_llm=_StubModel(),
        entry=entry,
    )
    return app.get_graph().draw_mermaid()
