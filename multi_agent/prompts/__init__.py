"""各角色的系统提示词。

- coder / reviewer / fixer：代码流水线三个 Agent（评分主线）
- chat：工作模式的对话 Agent（自由问答 + 调工具 + 判断何时开工）
- router / responder：流水线图内部的轻量分流（程序化调用 / 离线演示用）
"""

from multi_agent.prompts.chat_prompt import CHAT_SYSTEM_PROMPT
from multi_agent.prompts.coder_prompt import CODER_SYSTEM_PROMPT
from multi_agent.prompts.fixer_prompt import FIXER_SYSTEM_PROMPT
from multi_agent.prompts.responder_prompt import RESPONDER_SYSTEM_PROMPT
from multi_agent.prompts.reviewer_prompt import REVIEWER_SYSTEM_PROMPT
from multi_agent.prompts.router_prompt import ROUTER_SYSTEM_PROMPT

__all__ = [
    "CHAT_SYSTEM_PROMPT",
    "CODER_SYSTEM_PROMPT",
    "FIXER_SYSTEM_PROMPT",
    "RESPONDER_SYSTEM_PROMPT",
    "REVIEWER_SYSTEM_PROMPT",
    "ROUTER_SYSTEM_PROMPT",
]
