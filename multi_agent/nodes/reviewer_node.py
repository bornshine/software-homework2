"""审查 Agent 节点（glm 系列）。

输入：state["current_code"]（可选 state["self_check"]）
输出：state["findings"] / review_history / messages / errors
工具：ast_analyzer、code_executor（结构化调用 + 失败降级）

审查不依赖模型是否支持 tool calling：节点先把工具结果准备好，再连同代码
一起交给模型，模型只负责产出结构化 findings。工具挂掉只会降级成一条
可恢复错误，不会把整条 workflow 打崩。
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from multi_agent import llm_factory
from multi_agent.messages import (
    FIXER,
    KIND_ERROR,
    KIND_REPORT,
    KIND_REQUEST_FIX,
    KIND_VERDICT,
    REVIEWER,
    TOOLS,
    WORKFLOW,
    make_message,
)
from multi_agent.prompts import REVIEWER_SYSTEM_PROMPT
from multi_agent.resilience import invoke_llm, make_error, retry_call
from multi_agent.severity import blocking_findings, normalize_severity, summarize
from multi_agent.state import Finding, WorkflowState
from multi_agent.text_utils import extract_json_array
from tools.ast_analyzer import analyze_code
from tools.code_executor import execute_code, format_exec_result

# 模型有时会把 findings 包在对象里返回，这里兼容几种常见 key
_FINDING_KEYS = ("findings", "issues", "problems", "results")


def make_reviewer_node(llm: Any | None = None):
    """构造 reviewer 节点；llm 为 None 时在调用时从工厂获取。"""

    def node(state: WorkflowState) -> dict:
        return run_reviewer(state, llm)

    return node


def run_reviewer(state: WorkflowState, llm: Any | None = None) -> dict:
    """审查代码，产出结构化 findings。"""
    iteration = state.get("iteration", 0)

    # 上游已经失败时不要再浪费模型调用
    if state.get("status") == "failed":
        # 显式回写 findings，保证最终状态字段齐全（而不是缺 key）
        return {
            "findings": state.get("findings") or [],
            "log": ["[reviewer] 上游节点失败，跳过审查"],
            "messages": [
                make_message(
                    REVIEWER,
                    WORKFLOW,
                    KIND_REPORT,
                    "上游节点失败，跳过审查",
                    iteration=iteration,
                )
            ],
        }

    code = state.get("current_code") or ""
    if not code.strip():
        return _fail("当前代码为空，无法审查", iteration)

    # 1) 先跑工具。工具异常只记降级错误，流程继续。
    errors: list[dict] = []
    logs: list[str] = []

    ast_attempt = retry_call(lambda: analyze_code(code))
    if ast_attempt.ok:
        ast_text = ast_attempt.value
    else:
        ast_text = f"（AST 分析不可用：{ast_attempt.error}）"
        errors.append(
            make_error(REVIEWER, "tool_error", f"AST 分析不可用：{ast_attempt.error}", fatal=False)
        )
        logs.append(f"[reviewer] AST 工具降级：{ast_attempt.error}")

    exec_result = execute_code(code)
    exec_text = format_exec_result(exec_result)
    exec_env_error = str(exec_result.get("error") or "")
    if exec_env_error:
        errors.append(
            make_error(REVIEWER, "tool_error", f"运行验证不可用：{exec_env_error}", fatal=False)
        )
        logs.append(f"[reviewer] 运行验证降级：{exec_env_error}")

    # 2) 再调模型
    if llm is None:
        try:
            llm = llm_factory.get_reviewer_llm()
        except Exception as exc:  # noqa: BLE001 - 配置缺失时降级为致命错误
            return _fail(f"审查模型不可用：{type(exc).__name__}: {exc}", iteration)

    attempt = invoke_llm(
        llm,
        [
            SystemMessage(content=REVIEWER_SYSTEM_PROMPT),
            HumanMessage(
                content=_build_context(
                    code=code,
                    ast_text=ast_text,
                    exec_text=exec_text,
                    iteration=iteration,
                    state=state,
                )
            ),
        ],
    )
    if not attempt.ok:
        return _fail(f"审查模型调用失败：{attempt.error}", iteration)

    findings = _parse_findings(attempt.value)
    snapshot = _snapshot(findings, iteration)
    blocking = blocking_findings(findings)
    degraded = bool(errors)

    messages = [
        make_message(
            REVIEWER,
            TOOLS,
            KIND_REPORT,
            f"工具执行：AST={'降级' if not ast_attempt.ok else '正常'}，"
            f"运行验证={_exec_status_label(exec_result)}",
            iteration=iteration,
            payload={
                "ast_ok": ast_attempt.ok,
                "exec_ok": bool(exec_result.get("ok")),
                "exec_skipped": bool(exec_result.get("skipped")),
                "exec_env_error": exec_env_error,
                # 把"为什么没跑成"也记下来，否则运行记录里只能看到"被拒绝"三个字
                "exec_policy_reason": str(exec_result.get("policy_reason") or ""),
                "exec_returncode": exec_result.get("returncode"),
            },
        )
    ]
    if blocking:
        messages.append(
            make_message(
                REVIEWER,
                FIXER,
                KIND_REQUEST_FIX,
                f"发现 {snapshot['blocking']} 个阻断问题"
                f"（共 {snapshot['total']} 条，critical={snapshot['critical']}），请求修复",
                iteration=iteration,
                payload=snapshot,
            )
        )
    else:
        messages.append(
            make_message(
                REVIEWER,
                WORKFLOW,
                KIND_VERDICT,
                "审查通过，无阻断问题"
                + (f"，另有 {snapshot['total']} 条非阻断建议" if snapshot["total"] else "")
                + ("（工具降级，结论基于静态阅读）" if degraded else ""),
                iteration=iteration,
                payload={**snapshot, "degraded": degraded},
            )
        )

    logs.insert(
        0,
        f"[reviewer] 审查完成：{snapshot['blocking']} 个阻断问题 / "
        f"{snapshot['total']} 条记录{attempt.retry_note}",
    )

    return {
        "findings": findings,
        "status": "reviewing",
        "review_history": [snapshot],
        "messages": messages,
        "log": logs,
        **({"errors": errors} if errors else {}),
    }


def _build_context(
    *,
    code: str,
    ast_text: str,
    exec_text: str,
    iteration: int,
    state: WorkflowState,
) -> str:
    """组装给审查模型的上下文。"""
    parts = [
        f"【待审查代码】\n{code}",
        f"【AST 静态分析结果】\n{ast_text}",
        f"【运行验证结果】\n{exec_text}",
    ]
    if iteration:
        parts.append(f"【复审轮次】第 {iteration} 轮修复后的复审")

    self_check = state.get("self_check") or {}
    if self_check:
        verdict = "通过" if self_check.get("ok") else "未通过"
        parts.append(
            f"【上一轮 fixer 自验】{verdict}：{self_check.get('detail', '')}"
        )

    parts.append("请根据以上信息，输出结构化的 findings JSON 数组。")
    return "\n\n".join(parts)


def _exec_status_label(result: dict) -> str:
    if result.get("skipped"):
        return "被策略拒绝"
    if result.get("error"):
        return "环境异常"
    return "正常" if result.get("ok") else "异常退出"


def _snapshot(findings: list[Finding], iteration: int) -> dict:
    """统计 severity 分布与阻断数量（口径统一交给 severity 模块）。"""
    return summarize(findings, iteration)


def _fail(message: str, iteration: int) -> dict:
    """审查无法进行时的致命失败（条件边会直接终止）。"""
    return {
        "findings": [],
        "status": "failed",
        "errors": [make_error(REVIEWER, "llm_error", message, fatal=True)],
        "log": [f"[reviewer] 失败：{message}"],
        "messages": [
            make_message(REVIEWER, WORKFLOW, KIND_ERROR, message, iteration=iteration)
        ],
    }


def _parse_findings(text: str) -> list[Finding]:
    """从模型输出中提取 findings，尽量兼容各种包装形式。"""
    data = extract_json_array(text)
    if data is None:
        data = _findings_from_object(text)
    if data is None:
        # 解析失败：包成一条 warning，而不是抛异常
        return [
            {
                "severity": "warning",
                "location": "unknown",
                "issue": f"审查输出无法解析为 JSON：{text[:200]}",
                "suggestion": "请检查审查 Agent 的输出格式",
            }
        ]
    return [_normalize_finding(item) for item in data]


def _findings_from_object(text: str) -> list[Any] | None:
    """兼容 {"findings": [...]} 这类对象包装。"""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    for key in _FINDING_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return None


def _normalize_finding(item: object) -> Finding:
    """把模型返回的 dict 规整成 Finding 结构。"""
    if not isinstance(item, dict):
        return {"severity": "info", "location": "?", "issue": str(item), "suggestion": ""}
    return {
        "severity": normalize_severity(item.get("severity")),
        "location": str(item.get("location", "?")),
        "issue": str(item.get("issue", "")),
        "suggestion": str(item.get("suggestion", "")),
    }


# 不注入模型时的默认节点（LangGraph 直接使用）
reviewer_node = make_reviewer_node()
