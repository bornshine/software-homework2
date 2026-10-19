

"""共享状态定义：三个 Agent 之间的共享黑板 + 显式消息。

每个节点只读写自己关心的字段：
- coder    写 current_code，读 requirement
- reviewer 写 findings / review_history，读 current_code
- fixer    写 current_code / self_check，读 findings + current_code
- edges    读 findings / iteration / max_iterations / errors 决定路由

列表字段用 operator.add 做累加 reducer：多个节点写入不会互相覆盖。
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

# 本模块包含：
#   Finding          单个审查问题
#   AgentMessage     Agent 之间的显式消息（消息总线）
#   WorkflowError    降级/失败记录，fatal=True 会被条件边当成终止信号
#   CodeVersion      每轮代码快照
#   ReviewSnapshot   每轮审查的 severity 统计
#   SelfCheck        fixer 自验结果
#   WorkflowState    三 Agent 共享状态



class Finding(TypedDict, total=False):
    """审查发现的一个问题。"""

    severity: str       # "critical" | "warning" | "info"
    location: str       # 函数名 / 行号提示
    issue: str          # 问题描述
    suggestion: str     # 修复建议


class WorkflowState(TypedDict, total=False):
    """三 Agent 共享状态。

    所有字段都是可选的，因为不同节点在不同阶段写入。
    带 Annotated[..., operator.add] 的字段是累加语义。
    """

    # 输入
    requirement: str
    """用户的原始需求描述。"""
    run_id: str
    """本次运行的短 ID，用于日志与运行记录归档。"""

    # 入口路由
    mode: str
    """本次输入走哪条分支：code（代码任务）/ chat（自由问答）。"""
    answer: str
    """问答模式下 responder 产出的自然语言回答。"""

    # 代码黑板
    current_code: str
    """当前最新的代码（coder 首写，fixer 覆写）。"""
    code_history: Annotated[list[CodeVersion], operator.add]
    """每一版代码的快照，展示改动轨迹。"""

    # 审查结果
    findings: list[Finding]
    """reviewer 产出的结构化问题清单；空列表表示通过。"""
    review_history: Annotated[list[ReviewSnapshot], operator.add]
    """每次审查的问题数量统计（含 severity 分布）。"""
    self_check: SelfCheck
    """fixer 最新一次自验结果（后一轮覆盖前一轮）。"""

    # 循环控制
    iteration: int
    """已修复轮次（fixer 每跑一次 +1）。"""
    max_iterations: int
    """最大修复轮次，默认从 config.MAX_FIX_ROUNDS 注入。"""

    # Agent 间通信（累加语义）
    messages: Annotated[list[AgentMessage], operator.add]
    """Agent 之间的显式消息时间线（谁发给谁、发生了什么）。"""
    errors: Annotated[list[WorkflowError], operator.add]
    """降级/失败记录；fatal=True 的记录会让条件边直接终止流程。"""

    # 执行追踪
    status: str
    """当前阶段：routing / answered / coding / reviewing / fixing / passed / failed。"""
    log: Annotated[list[str], operator.add]
    """节点进入/退出时追加的日志行（CLI 用于渲染时间线）。"""


class AgentMessage(TypedDict, total=False):
    """Agent 之间的一条显式消息（消息总线上的一个信封）。"""

    id: str                  # 短 ID，便于日志引用
    sender: str              # coder / reviewer / fixer / system
    recipient: str           # 下游 Agent，或 workflow / tools
    kind: str                # handoff / request_fix / verdict / report / error
    summary: str             # 一句话说明
    iteration: int           # 产生该消息时的修复轮次
    created_at: str          # ISO 时间戳
    payload: dict[str, Any]  # 结构化附加信息


class WorkflowError(TypedDict, total=False):
    """一次降级或失败记录。

    fatal=True  表示流程无法继续，条件边会直接终止
    fatal=False 表示已降级处理（例如某个工具不可用），流程继续
    """

    scope: str       # 出错的节点
    kind: str        # tool_error / llm_error / invalid_input / self_check
    message: str     # 错误描述
    fatal: bool
    created_at: str


class CodeVersion(TypedDict, total=False):
    """代码黑板的一次版本快照，用于展示每轮改动。"""

    iteration: int
    author: str      # coder / fixer
    chars: int
    code: str


class ReviewSnapshot(TypedDict, total=False):
    """一次审查的统计快照。"""

    iteration: int
    total: int
    critical: int
    warning: int
    info: int


class SelfCheck(TypedDict, total=False):
    """fixer 修复后的自验结果（结构化，避免解析字符串）。"""

    ok: bool
    detail: str
    attempts: int
    iteration: int
