"""Workflow 执行器：驱动图、采集事件、写运行记录。

把"怎么跑"从 CLI 里拆出来：CLI 只负责把事件渲染成人看的界面，
测试可以直接调用 run_workflow 断言事件流与最终状态。

事件回调 on_event(kind, payload) 的 kind：
- "node"    某个节点开始产出更新（payload=节点名）
- "log"     一行节点日志（payload=字符串）
- "message" 一条 Agent 间消息（payload=AgentMessage）
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from langgraph.errors import GraphRecursionError

import config
from multi_agent.messages import SYSTEM, WORKFLOW, KIND_ERROR, make_message
from multi_agent.recorder import RunRecorder
from multi_agent.resilience import has_fatal_error, make_error
from multi_agent.severity import blocking_findings

TERMINAL_PASSED = "passed"
TERMINAL_FAILED = "failed"
TERMINAL_ABORTED = "aborted"
TERMINAL_ANSWERED = "answered"

EventCallback = Callable[[str, Any], None]


@dataclass
class RunResult:
    """一次运行的结论。"""

    state: dict[str, Any]
    terminal: str
    node_calls: dict[str, int] = field(default_factory=dict)
    elapsed: float = 0.0
    steps: int = 0
    record_path: str | None = None

    @property
    def ok(self) -> bool:
        """通过和问答都算"正常结束"；只有 failed / aborted 才算失败。"""
        return self.terminal in (TERMINAL_PASSED, TERMINAL_ANSWERED)


def run_workflow(
    app: Any,
    initial_state: dict[str, Any],
    *,
    recursion_limit: int | None = None,
    recorder: RunRecorder | None = None,
    on_event: EventCallback | None = None,
) -> RunResult:
    """跑一次 workflow，返回最终状态与运行指标。"""
    limit = recursion_limit or config.RECURSION_LIMIT
    started = time.monotonic()
    state: dict[str, Any] = dict(initial_state)
    node_calls: dict[str, int] = {}
    steps = 0
    aborted_reason = ""

    try:
        stream: Iterable[Any] = app.stream(
            state,
            config={"recursion_limit": limit},
            stream_mode=["updates", "values"],
        )
        for mode, payload in stream:
            if mode == "updates":
                steps += 1
                for node_name, update in payload.items():
                    node_calls[node_name] = node_calls.get(node_name, 0) + 1
                    _emit(on_event, "node", node_name)
                    for line in update.get("log") or []:
                        _emit(on_event, "log", line)
                    for message in update.get("messages") or []:
                        _emit(on_event, "message", message)
                    if recorder is not None:
                        recorder.record_node(node_name, update)
            else:
                # values 模式给出的是合并后的完整状态
                state = dict(payload)
                if recorder is not None:
                    recorder.update_snapshot(state)
    except GraphRecursionError:
        aborted_reason = f"达到最大图步数（{limit}），流程中止"
    except KeyboardInterrupt:
        if recorder is not None:
            recorder.update_snapshot(state)
        raise

    elapsed = time.monotonic() - started

    if aborted_reason:
        state = _mark_aborted(state, aborted_reason, on_event)
        terminal = TERMINAL_ABORTED
    else:
        terminal = terminal_of(state)

    if recorder is not None:
        recorder.finish(state, terminal=terminal, node_calls=node_calls, elapsed=elapsed)

    return RunResult(
        state=state,
        terminal=terminal,
        node_calls=node_calls,
        elapsed=elapsed,
        steps=steps,
        record_path=str(recorder) if recorder is not None and recorder.enabled else None,
    )


def terminal_of(state: dict[str, Any]) -> str:
    """根据最终状态判断结论。"""
    if has_fatal_error(state.get("errors")) or state.get("status") == "failed":
        return TERMINAL_FAILED
    if state.get("status") == "answered":
        return TERMINAL_ANSWERED
    # 只有阻断级别的问题才算未通过；只剩 info 级建议算通过
    if blocking_findings(state.get("findings")):
        return TERMINAL_FAILED
    return TERMINAL_PASSED


def _mark_aborted(
    state: dict[str, Any],
    reason: str,
    on_event: EventCallback | None,
) -> dict[str, Any]:
    state = dict(state)
    state["status"] = "failed"
    state["errors"] = list(state.get("errors") or []) + [
        make_error(WORKFLOW, "recursion_limit", reason, fatal=True)
    ]
    log_line = f"[workflow] {reason}"
    state["log"] = list(state.get("log") or []) + [log_line]
    state["messages"] = list(state.get("messages") or []) + [
        make_message(SYSTEM, WORKFLOW, KIND_ERROR, reason)
    ]
    _emit(on_event, "log", log_line)
    return state


def _emit(callback: EventCallback | None, kind: str, payload: Any) -> None:
    if callback is not None:
        callback(kind, payload)
