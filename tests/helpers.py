"""测试用的假模型（不联网、不烧 token）。"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage


class ScriptedModel:
    """按脚本依次返回预设 AIMessage；脚本用完后重复最后一条。"""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self._call_count = 0

    @property
    def calls(self) -> int:
        return self._call_count

    def invoke(self, messages: Any) -> AIMessage:
        self._call_count += 1
        index = min(self._call_count - 1, len(self._responses) - 1)
        return AIMessage(content=self._responses[index])


class BoomModel:
    """总是抛异常的模型，用于验证重试 / 降级 / 失败路径。"""

    def __init__(self, message: str = "模型不可用"):
        self._message = message
        self.calls = 0

    def invoke(self, messages: Any) -> AIMessage:
        self.calls += 1
        raise RuntimeError(self._message)


class ToolCallingModel:
    """支持 bind_tools 的脚本化模型：按脚本依次返回预设的 AIMessage。

    用来测"模型发起工具调用 → 工具执行 → 模型总结"这条 ReAct 回路。
    """

    def __init__(self, responses: list[AIMessage]):
        self._responses = list(responses)
        self.calls = 0
        self.bound_tools: list[Any] = []

    def bind_tools(self, tools: Any) -> "ToolCallingModel":
        self.bound_tools = list(tools)
        return self

    def invoke(self, messages: Any) -> AIMessage:
        self.calls += 1
        index = min(self.calls - 1, len(self._responses) - 1)
        return self._responses[index]


def tool_call(name: str, args: dict[str, Any], call_id: str = "call_1") -> dict[str, Any]:
    """构造 AIMessage.tool_calls 需要的那条结构。"""
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}
