"""节点级韧性工具：重试、降级、错误记录。

设计目标：单个模型调用或工具调用抖动，不应该把整条 workflow 打崩。
- retry_call    ：带指数退避的重试，把异常转成结构化 Attempt
- invoke_llm    ：调用 LLM 并统一取文本，失败重试后返回错误而不是抛异常
- call_tool     ：调用 LangChain 工具并吞掉异常，返回可读文本
- make_error    ：构造 WorkflowError，供条件边判断是否致命
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Sequence

from langchain_core.tools import BaseTool

import config
from multi_agent.state import WorkflowError
from multi_agent.text_utils import message_text


@dataclass(frozen=True)
class Attempt:
    """一次（可能经过重试的）调用结果。"""

    ok: bool
    value: Any = None
    error: str = ""
    attempts: int = 0

    @property
    def retry_note(self) -> str:
        """成功但重试过时返回说明，否则空串。"""
        if self.ok and self.attempts > 1:
            return f"（重试 {self.attempts - 1} 次后成功）"
        return ""


def _always_retry(exc: BaseException) -> bool:
    return True


def is_timeout_error(exc: BaseException) -> bool:
    """判断是否超时类错误。

    推理模型的超时往往意味着"它本来就很慢"，而不是瞬时抖动。
    实测 glm-5.3-flash 对一段 1.5KB 的代码审查提示要跑 121 秒，
    这种超时重试三次只会把失败推迟到几分钟后，所以默认不重试。
    """
    for klass in type(exc).__mro__:
        if "timeout" in klass.__name__.lower():
            return True
    return False


def retry_call(
    fn: Callable[[], Any],
    *,
    attempts: int | None = None,
    base_delay: float | None = None,
    max_delay: float | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    retry_if: Callable[[BaseException], bool] | None = None,
) -> Attempt:
    """调用 fn，失败则按指数退避重试，始终返回 Attempt 而不抛异常。"""
    total = max(1, attempts if attempts is not None else config.NODE_MAX_ATTEMPTS)
    base = config.NODE_RETRY_BASE_DELAY if base_delay is None else base_delay
    ceiling = config.NODE_RETRY_MAX_DELAY if max_delay is None else max_delay

    should_retry = retry_if or _always_retry
    last_error = ""
    attempts_made = 0
    for index in range(1, total + 1):
        attempts_made = index
        try:
            return Attempt(ok=True, value=fn(), attempts=index)
        except Exception as exc:  # noqa: BLE001 - 边界层统一兜底
            last_error = f"{type(exc).__name__}: {exc}"
            if index >= total or not should_retry(exc):
                break
            sleeper(min(base * (2 ** (index - 1)), ceiling))
    return Attempt(ok=False, error=last_error, attempts=attempts_made)


def invoke_llm(
    llm: Any,
    messages: Sequence[Any],
    *,
    attempts: int | None = None,
    retry_if: Callable[[BaseException], bool] | None = None,
) -> Attempt:
    """调用 LLM 并返回文本内容；失败重试后返回错误 Attempt。

    默认不重试超时错误（超时通常是"模型慢"而不是"抖动"）。
    """

    def call() -> str:
        return message_text(llm.invoke(list(messages)))

    predicate = retry_if or (lambda exc: not is_timeout_error(exc))
    return retry_call(call, attempts=attempts, retry_if=predicate)


def call_tool(tool: BaseTool, payload: dict[str, Any]) -> tuple[str, str | None]:
    """调用 LangChain 工具，返回 (文本结果, 错误信息)。

    工具异常不会向上抛：失败时文本是提示语，错误信息供调用方记录降级日志。
    """
    attempt = retry_call(
        lambda: tool.invoke(payload),
        attempts=config.TOOL_MAX_ATTEMPTS,
    )
    if attempt.ok:
        return (attempt.value if isinstance(attempt.value, str) else str(attempt.value)), None
    return f"⛔ 工具执行失败：{attempt.error}", attempt.error


def make_error(scope: str, kind: str, message: str, *, fatal: bool) -> WorkflowError:
    """构造一条错误记录。fatal=True 会让条件边终止流程。"""
    return {
        "scope": scope,
        "kind": kind,
        "message": message,
        "fatal": fatal,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def has_fatal_error(errors: Sequence[WorkflowError] | None) -> bool:
    """状态里的错误列表中是否存在致命错误。"""
    return any(bool(item.get("fatal")) for item in (errors or []))


def fatal_messages(errors: Sequence[WorkflowError] | None) -> list[str]:
    """取出所有致命错误的描述文本。"""
    return [str(item.get("message", "")) for item in (errors or []) if item.get("fatal")]
