"""多 Agent 协作 Workflow 包。

三 Agent 角色：
- coder   （deepseek 系列）根据需求生成代码
- reviewer（glm 系列）     审查代码并产出结构化 findings
- fixer   （xiaomi 系列）  根据 findings 修复代码，并做结构化自验

三者既通过共享状态 WorkflowState 协作（数据黑板），
也通过 AgentMessage 显式通信（消息总线），见 state.py 与 messages.py。
"""

from multi_agent.graph import build_workflow, describe_workflow_graph
from multi_agent.messages import format_message, make_message
from multi_agent.recorder import RunRecorder, load_record, plan_resume
from multi_agent.runner import RunResult, run_workflow
from multi_agent.state import AgentMessage, WorkflowError, WorkflowState

__all__ = [
    "AgentMessage",
    "RunRecorder",
    "RunResult",
    "WorkflowError",
    "WorkflowState",
    "build_workflow",
    "describe_workflow_graph",
    "format_message",
    "load_record",
    "make_message",
    "plan_resume",
    "run_workflow",
]
