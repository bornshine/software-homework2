"""Agent 间显式消息（消息总线）。

共享状态负责"数据在哪"，消息总线负责"谁对谁说了什么"。
两者互补：条件边读状态做路由，人要读消息时间线才能理解流程。

消息不参与路由判定，只做可观测与交接说明，因此即使消息缺失也不会破坏流程。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from multi_agent.state import AgentMessage

# 发送方 / 接收方角色
CODER = "coder"
REVIEWER = "reviewer"
FIXER = "fixer"
ROUTER = "router"
RESPONDER = "responder"
SYSTEM = "system"
TOOLS = "tools"
WORKFLOW = "workflow"

# 消息类型
KIND_HANDOFF = "handoff"          # 上游交付产物给下游
KIND_REQUEST_FIX = "request_fix"  # reviewer 要求 fixer 修复
KIND_VERDICT = "verdict"          # 审查结论（通过 / 不通过）
KIND_REPORT = "report"            # 过程报告（工具执行、跳过等）
KIND_ERROR = "error"              # 异常通知

KINDS = frozenset({KIND_HANDOFF, KIND_REQUEST_FIX, KIND_VERDICT, KIND_REPORT, KIND_ERROR})


def make_message(
    sender: str,
    recipient: str,
    kind: str,
    summary: str,
    *,
    iteration: int | None = None,
    payload: dict[str, Any] | None = None,
    message_id: str | None = None,
    created_at: str | None = None,
) -> AgentMessage:
    """构造一条结构化消息。"""
    message: AgentMessage = {
        "id": message_id or uuid.uuid4().hex[:8],
        "sender": sender,
        "recipient": recipient,
        "kind": kind,
        "summary": summary,
        "created_at": created_at or datetime.now().isoformat(timespec="seconds"),
        "payload": payload or {},
    }
    if iteration is not None:
        message["iteration"] = iteration
    return message


def format_message(message: AgentMessage) -> str:
    """渲染成一行人类可读文本（CLI / 日志用）。"""
    sender = message.get("sender", "?")
    recipient = message.get("recipient", "?")
    kind = message.get("kind", "?")
    summary = message.get("summary", "")
    suffix = f" · 轮次 {message['iteration']}" if message.get("iteration") is not None else ""
    return f"{sender} → {recipient} [{kind}] {summary}{suffix}"
