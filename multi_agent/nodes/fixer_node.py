"""修复 Agent 节点（xiaomi 系列）。

输入：state["current_code"] + state["findings"]
输出：state["current_code"]（修复后）、self_check、iteration+1、messages

自验闭环：修复后先做一次结构化验证（策略校验 + 受限执行）。不通过就带着
错误信息再修一次，次数由 config.FIXER_SELF_REPAIR_ATTEMPTS 控制（默认 1）。
这样既能减少外层次数，也不再是"改完直接丢回 reviewer"。

注意区分两类失败：
- 代码本身有问题 → 值得再修一次
- 自验环境不可用（临时目录不可写等）→ 重试无意义，直接降级并记录
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

import config
from multi_agent import llm_factory
from multi_agent.messages import (
    FIXER,
    KIND_ERROR,
    KIND_HANDOFF,
    KIND_REPORT,
    REVIEWER,
    WORKFLOW,
    make_message,
)
from multi_agent.prompts import FIXER_SYSTEM_PROMPT
from multi_agent.resilience import invoke_llm, make_error
from multi_agent.state import Finding, WorkflowState
from multi_agent.text_utils import clean_code_output
from tools.code_executor import execute_code


def make_fixer_node(llm: Any | None = None):
    """构造 fixer 节点；llm 为 None 时在调用时从工厂获取。"""

    def node(state: WorkflowState) -> dict:
        return run_fixer(state, llm)

    return node


def run_fixer(state: WorkflowState, llm: Any | None = None) -> dict:
    """根据 findings 修复代码，返回修复后的完整代码。"""
    iteration = state.get("iteration", 0)
    next_iteration = iteration + 1

    if state.get("status") == "failed":
        return {
            "log": ["[fixer] 上游节点失败，跳过修复"],
            "messages": [
                make_message(FIXER, WORKFLOW, KIND_REPORT, "上游节点失败，跳过修复", iteration=iteration)
            ],
        }

    code = state.get("current_code") or ""
    findings = state.get("findings") or []
    if not code.strip() or not findings:
        return {
            "log": [f"[fixer] 跳过：{'代码为空' if not code.strip() else '没有问题需要修复'}"],
            "messages": [
                make_message(
                    FIXER,
                    WORKFLOW,
                    KIND_REPORT,
                    "无待修复内容，跳过修复",
                    iteration=iteration,
                )
            ],
        }

    if llm is None:
        try:
            llm = llm_factory.get_fixer_llm()
        except Exception as exc:  # noqa: BLE001 - 配置缺失时降级为致命错误
            return _fail(f"修复模型不可用：{type(exc).__name__}: {exc}", iteration)

    findings_text = _format_findings(findings)
    attempt = invoke_llm(
        llm,
        [
            SystemMessage(content=FIXER_SYSTEM_PROMPT),
            HumanMessage(content=_build_context(code, findings_text, next_iteration)),
        ],
    )
    if not attempt.ok:
        return _fail(f"修复模型调用失败：{attempt.error}", iteration)

    new_code = clean_code_output(attempt.value)
    if not new_code.strip():
        return _fail("修复模型返回了空代码", iteration)

    verify = _verify(new_code)
    repairs = 0
    # 仅当"代码本身有问题"时才带错误反馈再修一次；环境不可用不重试
    while (
        not verify["ok"]
        and not verify.get("env_failed")
        and repairs < config.FIXER_SELF_REPAIR_ATTEMPTS
    ):
        repairs += 1
        retry_attempt = invoke_llm(
            llm,
            [
                SystemMessage(content=FIXER_SYSTEM_PROMPT),
                HumanMessage(
                    content=_build_retry_context(
                        code=code,
                        candidate=new_code,
                        findings_text=findings_text,
                        verify_detail=verify["detail"],
                        iteration=next_iteration,
                    )
                ),
            ],
        )
        if not retry_attempt.ok:
            break
        candidate = clean_code_output(retry_attempt.value)
        if not candidate.strip():
            break
        new_code = candidate
        verify = _verify(new_code)

    logs = [f"[fixer] 第 {next_iteration} 轮修复完成，代码 {len(new_code)} 字符{attempt.retry_note}"]
    if verify["ok"]:
        logs.append(f"[fixer] 自验通过（{verify['detail']}）")
    else:
        logs.append(f"[fixer] 自验未通过（额外重塑 {repairs} 次）：{verify['detail'][:120]}")

    self_check = {
        "ok": verify["ok"],
        "detail": verify["detail"],
        "attempts": 1 + repairs,
        "iteration": next_iteration,
    }
    message = make_message(
        FIXER,
        REVIEWER,
        KIND_HANDOFF,
        f"第 {next_iteration} 轮修复完成，自验{'通过' if verify['ok'] else '未通过'}",
        iteration=next_iteration,
        payload={
            "chars": len(new_code),
            "self_check_ok": verify["ok"],
            "self_check_detail": verify["detail"][:200],
            "repair_attempts": repairs,
        },
    )

    update: dict[str, Any] = {
        "current_code": new_code,
        "iteration": next_iteration,
        "status": "fixing",
        "self_check": self_check,
        "code_history": [
            {"iteration": next_iteration, "author": FIXER, "chars": len(new_code), "code": new_code}
        ],
        "messages": [message],
        "log": logs,
    }
    if not verify["ok"]:
        update["errors"] = [
            make_error(FIXER, "self_check", f"自验未通过：{verify['detail']}", fatal=False)
        ]
    return update


def _format_findings(findings: list[Finding]) -> str:
    return "\n".join(
        f"{index + 1}. [{item.get('severity', 'info')}] {item.get('location', '?')}: "
        f"{item.get('issue', '')}（建议：{item.get('suggestion', '?')}）"
        for index, item in enumerate(findings)
    )


def _build_context(code: str, findings_text: str, iteration: int) -> str:
    return (
        f"【当前代码】\n{code}\n\n"
        f"【待修复问题清单】\n{findings_text}\n\n"
        f"这是第 {iteration} 轮修复，请输出修复后的完整代码。"
    )


def _build_retry_context(
    *,
    code: str,
    candidate: str,
    findings_text: str,
    verify_detail: str,
    iteration: int,
) -> str:
    return (
        f"【待修复的原始代码】\n{code}\n\n"
        f"【你上一轮给出的修复结果】\n{candidate}\n\n"
        f"【待修复问题清单】\n{findings_text}\n\n"
        f"【自验证失败信息】\n{verify_detail}\n\n"
        f"你的上一版修复没有通过运行验证。请修正后输出第 {iteration} 轮的完整代码。"
    )


def _verify(code: str) -> dict:
    """结构化自验：区分"代码有问题"和"验证环境不可用"。"""
    result = execute_code(code)
    if result.get("skipped"):
        return {"ok": False, "detail": f"代码被安全策略拒绝执行：{result.get('policy_reason', '')}"}
    if result.get("error"):
        return {
            "ok": False,
            "env_failed": True,
            "detail": f"自验环境不可用：{result['error']}",
        }
    if result.get("ok"):
        return {"ok": True, "detail": "运行验证通过"}

    stderr_lines = (result.get("stderr") or "").strip().splitlines()
    tail = stderr_lines[-1] if stderr_lines else "未知错误"
    return {"ok": False, "detail": f"退出码 {result.get('returncode')}：{tail}"}


def _fail(message: str, iteration: int) -> dict:
    """修复无法进行时的致命失败（条件边会直接终止）。"""
    return {
        "status": "failed",
        "errors": [make_error(FIXER, "llm_error", message, fatal=True)],
        "log": [f"[fixer] 失败：{message}"],
        "messages": [make_message(FIXER, WORKFLOW, KIND_ERROR, message, iteration=iteration)],
    }


# 不注入模型时的默认节点（LangGraph 直接使用）
fixer_node = make_fixer_node()
