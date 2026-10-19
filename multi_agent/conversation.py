"""工作模式的对话层：带记忆、带工具的 ReAct Agent。

和流水线的区别：
- 状态用 MessagesState（消息列表）+ MemorySaver（多轮记忆，**进程内**，关掉就没了）
- 模型是单独的 mimo-v2.6-pro，可以自由问答
- 能调工具：读文件 / 跑代码 / 静态分析 / 写文件（写前必须用户确认）

它**不执行流水线**：要代码时调用 `propose_code_task` 提交提案，
由 CLI 请用户确认后再把需求交给 编码→审查→修复 那条图。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Sequence

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import MessagesState
from langgraph.prebuilt import ToolNode

import config
from multi_agent.prompts import CHAT_SYSTEM_PROMPT
from multi_agent.text_utils import sanitize_text

PROPOSE_TOOL_NAME = "propose_code_task"
CONTEXT_PREFIX = "【系统】"

NodeName = Literal["tools", "end"]
EventCallback = Callable[[str, Any], None]


@dataclass(frozen=True)
class TaskProposal:
    """一份待用户确认的代码任务提案。"""

    requirement: str
    summary: str = ""
    source: str = "model"      # model（模型主动提的）/ manual（/task 指定的）


@dataclass
class TurnResult:
    """一轮对话的结果。"""

    answer: str = ""
    proposal: TaskProposal | None = None
    tool_names: list[str] = field(default_factory=list)
    messages: list[BaseMessage] = field(default_factory=list)

    @property
    def used_tools(self) -> bool:
        return bool(self.tool_names)


@tool(PROPOSE_TOOL_NAME)
def propose_code_task(requirement: str, summary: str = "") -> str:
    """把代码任务提交给后台流水线，等待用户确认后执行。

    只在"用户要一份代码产物、且需求已经说清楚"时调用。参数：
    - requirement: 完整、可独立读懂的需求描述（把前面聊出来的约束都写进去）
    - summary: 一句话说明你打算怎么做
    """
    return f"任务提案已提交，等待用户确认：{requirement}"


def trim_history(
    messages: Sequence[BaseMessage],
    limit: int | None = None,
) -> list[BaseMessage]:
    """只保留最近若干条消息；截断点对齐到最近的 HumanMessage。

    否则会出现「有 ToolMessage 却没有对应 tool_call」的非法序列，
    OpenAI 兼容接口会直接报 400。
    """
    max_messages = config.MAX_HISTORY_MESSAGES if limit is None else limit
    items = list(messages)
    if max_messages <= 0 or len(items) <= max_messages:
        return items

    tail = items[-max_messages:]
    start = next(
        (index for index, message in enumerate(tail) if isinstance(message, HumanMessage)),
        None,
    )
    if start is None:
        start = next(
            (index for index, message in enumerate(tail) if not isinstance(message, ToolMessage)),
            0,
        )
    return tail[start:]


def should_use_tools(state: MessagesState) -> NodeName:
    """有 tool_calls 就去执行工具，否则本轮结束。"""
    last = state["messages"][-1]
    return "tools" if getattr(last, "tool_calls", None) else "end"


def create_checkpointer() -> MemorySaver:
    """多轮记忆：进程内保存，关掉窗口即失效（按需求不做落盘）。"""
    return MemorySaver()


def build_conversation_agent(
    llm: Any | None = None,
    tools: Sequence[BaseTool] | None = None,
    checkpointer: Any = None,
    max_history: int | None = None,
):
    """构建工作模式的对话 Agent（ReAct：chat ⇄ tools）。"""
    if llm is None:
        from multi_agent import llm_factory

        llm = llm_factory.get_chat_llm()

    tool_list = list(tools) if tools is not None else []
    if PROPOSE_TOOL_NAME not in {getattr(item, "name", "") for item in tool_list}:
        tool_list.append(propose_code_task)

    model_with_tools = llm.bind_tools(tool_list)
    tool_node = ToolNode(tool_list)

    def call_model(state: MessagesState) -> dict[str, list[BaseMessage]]:
        history = trim_history(state["messages"], max_history)
        messages: list[BaseMessage] = [SystemMessage(content=CHAT_SYSTEM_PROMPT), *history]
        response = model_with_tools.invoke(messages)
        return {"messages": [response]}

    workflow = StateGraph(MessagesState)
    workflow.add_node("chat", call_model)
    workflow.add_node("tools", tool_node)
    workflow.add_edge(START, "chat")
    workflow.add_conditional_edges(
        "chat",
        should_use_tools,
        {"tools": "tools", "end": END},
    )
    workflow.add_edge("tools", "chat")
    return workflow.compile(checkpointer=checkpointer)


def run_turn(
    app: Any,
    user_input: str,
    thread_id: str,
    *,
    on_event: EventCallback | None = None,
    recursion_limit: int | None = None,
) -> TurnResult:
    """跑一轮对话，返回回答、本轮工具轨迹与（可能的）任务提案。"""
    run_config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": recursion_limit or config.RECURSION_LIMIT,
    }
    result = TurnResult()

    for chunk in app.stream(
        # 入口先清洗：管道输入可能带孤立代理字符，直接发出去会编码失败
        {"messages": [HumanMessage(content=sanitize_text(user_input))]},
        config=run_config,
        stream_mode="updates",
    ):
        for update in chunk.values():
            for message in update.get("messages", []) or []:
                result.messages.append(message)
                for call in getattr(message, "tool_calls", None) or []:
                    name = str(call.get("name", ""))
                    if name and name not in result.tool_names:
                        result.tool_names.append(name)
                    _emit(on_event, "tool_call", {"name": name, "args": call.get("args")})
                if isinstance(message, ToolMessage):
                    _emit(
                        on_event,
                        "tool_result",
                        {
                            "name": getattr(message, "name", "tool"),
                            "chars": len(str(message.content)),
                            "content": str(message.content),
                        },
                    )

    result.answer = last_ai_text(result.messages)
    result.proposal = extract_proposal(result.messages)
    return result


def last_ai_text(messages: Sequence[BaseMessage]) -> str:
    """取最后一条有正文的 AI 回复。"""
    for message in reversed(list(messages)):
        if isinstance(message, AIMessage):
            text = as_text(message.content).strip()
            if text:
                return text
    return ""


def as_text(content: Any) -> str:
    """把消息内容统一转成字符串（兼容 str 与 content block 列表）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(parts)
    return "" if content is None else str(content)


def extract_proposal(messages: Sequence[BaseMessage]) -> TaskProposal | None:
    """从本轮消息里找出模型提交的任务提案（取最后一次）。"""
    proposal: TaskProposal | None = None
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            if call.get("name") != PROPOSE_TOOL_NAME:
                continue
            args = _normalize_args(call.get("args"))
            requirement = str(args.get("requirement") or "").strip()
            if not requirement:
                continue
            proposal = TaskProposal(
                requirement=requirement,
                summary=str(args.get("summary") or "").strip(),
                source="model",
            )
    return proposal


def _normalize_args(args: Any) -> dict[str, Any]:
    """tool_calls 的 args 可能是 dict，也可能是 JSON 字符串。"""
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except json.JSONDecodeError:
            return {"requirement": args}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def build_result_note(
    *,
    requirement: str,
    terminal: str,
    iteration: int,
    findings: Sequence[dict] | None,
    code: str,
    record_path: str | None = None,
) -> str:
    """把一次流水线执行整理成可注入对话历史的通知。"""
    lines = [
        "代码流水线执行完毕。",
        f"- 需求：{requirement}",
        f"- 结论：{terminal}（修复轮次 {iteration}）",
    ]
    if record_path:
        lines.append(f"- 运行记录：{record_path}")

    items = list(findings or [])
    blocking = [item for item in items if str(item.get("severity")) in config.BLOCKING_SEVERITIES]
    if blocking:
        lines.append(f"- 仍有 {len(blocking)} 个阻断问题：")
        lines.extend(
            f"  · [{item.get('severity')}] {item.get('issue', '')}" for item in blocking
        )
    advice = [item for item in items if item not in blocking]
    if advice:
        lines.append(f"- 另有 {len(advice)} 条非阻断建议（info 级）")

    if code:
        lines.append(f"- 最终代码（{len(code)} 字符）：")
        lines.append("```python")
        lines.append(code)
        lines.append("```")
    lines.append("用户接下来可能会针对这份结果继续提问或要求修改。")
    return "\n".join(lines)


def inject_system_note(app: Any, thread_id: str, note: str) -> None:
    """把一条系统通知写进对话历史，让后续追问有上下文。

    用 HumanMessage + 【系统】前缀而不是 SystemMessage：多数 OpenAI 兼容接口
    期望 system 消息出现在开头，插到中间不保险；这个前缀对模型同样清晰。
    """
    app.update_state(
        {"configurable": {"thread_id": thread_id}},
        {"messages": [HumanMessage(content=f"{CONTEXT_PREFIX} {note}")]},
    )


def _emit(callback: EventCallback | None, kind: str, payload: Any) -> None:
    if callback is not None:
        callback(kind, payload)
