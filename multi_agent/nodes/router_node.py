"""入口路由节点：判断这次输入是要代码，还是要问答。

判定顺序（从便宜到贵）：
1. state["mode"] 已指定（CLI --mode）→ 直接沿用，不再猜
2. 规则层（intent.rule_classify）→ 零延迟、完全确定
3. 模型层（intent.llm_classify）→ 只有规则拿不准时才叫一次快模型
4. 兜底 → code（保持旧行为，不让既有用法失效）

这个节点只写 mode，不写业务数据；后续走哪条边由条件边读 mode 决定。
"""

from __future__ import annotations

from typing import Any

from multi_agent import llm_factory
from multi_agent.intent import MODE_CHAT, MODE_CODE, MODES, llm_classify, rule_classify
from multi_agent.messages import CODER, KIND_HANDOFF, RESPONDER, ROUTER, make_message
from multi_agent.state import WorkflowState

SOURCE_EXPLICIT = "explicit"
SOURCE_RULES = "rules"
SOURCE_LLM = "llm"
SOURCE_FALLBACK = "fallback"


def make_router_node(llm: Any | None = None):
    """构造路由节点；llm 只在"规则拿不准"时才会被用到。"""

    def node(state: WorkflowState) -> dict:
        return run_router(state, llm)

    return node


def run_router(state: WorkflowState, llm: Any | None = None) -> dict:
    """识别意图，写入 state["mode"]。"""
    text = (state.get("requirement") or "").strip()
    explicit = state.get("mode")

    if explicit in MODES:
        mode, source = str(explicit), SOURCE_EXPLICIT
    elif not text:
        mode, source = MODE_CHAT, SOURCE_RULES
    else:
        rule = rule_classify(text)
        if rule:
            mode, source = rule, SOURCE_RULES
        else:
            model = llm
            if model is None:
                try:
                    model = llm_factory.get_coder_llm()
                except Exception:  # noqa: BLE001 - 分类失败不能把入口堵死
                    model = None
            decided = llm_classify(text, model) if model is not None else None
            if decided:
                mode, source = decided, SOURCE_LLM
            else:
                mode, source = MODE_CODE, SOURCE_FALLBACK

    is_code = mode == MODE_CODE
    label = "代码任务" if is_code else "自由问答"
    return {
        "mode": mode,
        "status": "routing",
        "log": [f"[router] 识别为{label}（来源：{source}）"],
        "messages": [
            make_message(
                ROUTER,
                CODER if is_code else RESPONDER,
                KIND_HANDOFF,
                f"识别为{label}，交给 {'coder' if is_code else 'responder'}",
                payload={"mode": mode, "source": source},
            )
        ],
    }


# 不注入模型时的默认节点（分类才会去取模型）
router_node = make_router_node()
