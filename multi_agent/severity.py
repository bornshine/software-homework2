"""问题严重级别的统一处理：归一化、过滤、统计。

“通过”的语义是“无阻断问题”，而不是“零建议”：
只有 config.BLOCKING_SEVERITIES 里的级别会触发修复循环，
其余级别照常记录进 findings 与运行记录，但不阻止流程通过。
"""

from __future__ import annotations

from typing import Sequence

import config
from multi_agent.state import Finding


class FindingSeverity:
    """级别的字符串常量（避免满代码库写魔法字符串）。"""

    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


def normalize_severity(value: object) -> str:
    """把模型给的级别归一到三种已知级别。

    未知级别一律当 warning（阻断）处理：宁多多修一轮，也不静默放过一个
    可能严重的问题。反过来映射成 info 会让“模型乱写级别”变成“永远通过”。
    """
    text = str(value or "").strip().lower()
    if text in config.KNOWN_SEVERITIES:
        return text
    return FindingSeverity.WARNING


def is_blocking(finding: Finding) -> bool:
    """这个 finding 是否算阻断问题。"""
    return normalize_severity(finding.get("severity")) in config.BLOCKING_SEVERITIES


def blocking_findings(findings: Sequence[Finding] | None) -> list[Finding]:
    return [item for item in (findings or []) if is_blocking(item)]


def suggestions(findings: Sequence[Finding] | None) -> list[Finding]:
    """非阻断的建议（默认就是 info 级）。"""
    return [item for item in (findings or []) if not is_blocking(item)]


def summarize(findings: Sequence[Finding] | None, iteration: int | None = None) -> dict:
    """统计 total / 各级别数量 / 阻断数量。"""
    items = list(findings or [])
    snapshot: dict = {
        "total": len(items),
        "blocking": len(blocking_findings(items)),
    }
    for severity in config.KNOWN_SEVERITIES:
        snapshot[severity] = sum(
            1 for item in items if normalize_severity(item.get("severity")) == severity
        )
    if iteration is not None:
        snapshot["iteration"] = iteration
    return snapshot
