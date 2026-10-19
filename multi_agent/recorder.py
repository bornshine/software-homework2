"""运行记录（JSON）：执行时间线 + 状态快照，支持中断后续跑。

每次 run 写一份 runs/<run_id>.json：
- timeline：每个节点完成后追加一条（节点、日志、消息、时间）
- snapshot：最近一次完整状态（--resume 时直接拿来当初始状态）
- result：结束结论（passed / failed / aborted）、耗时、各节点调用次数

写入采用"临时文件 + 原子替换"，中途崩溃不会留下半截 JSON。
如果目录不可写（例如受限沙箱），记录功能自动降级，不影响主流程。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import config

SCHEMA_VERSION = 1


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _jsonable(value: Any) -> Any:
    """保证内容可被 JSON 序列化（遇到未知类型退化成字符串）。"""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


class RunRecorder:
    """把一次 workflow 的执行过程落到磁盘。"""

    def __init__(
        self,
        run_id: str,
        requirement: str,
        *,
        directory: str | os.PathLike[str] | None = None,
        max_iterations: int = 0,
        entry: str = "coder",
        resumed_from: str | None = None,
    ) -> None:
        self.directory = Path(directory or config.RUN_RECORD_DIR)
        self.path = self.directory / f"{run_id}.json"
        self.warning: str = ""
        self.document: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "created_at": now(),
            "updated_at": now(),
            "requirement": requirement,
            "max_iterations": max_iterations,
            "entry": entry,
            "resumed_from": resumed_from,
            "last_node": None,
            "timeline": [],
            "snapshot": {},
            "result": None,
        }
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.warning = f"运行记录目录不可用（{exc}），本次不落盘"
        self._flush()

    def record_node(self, node_name: str, update: dict[str, Any]) -> None:
        """追加一个节点的执行记录。"""
        self.document["last_node"] = node_name
        self.document["timeline"].append(
            {
                "node": node_name,
                "at": now(),
                "status": update.get("status", ""),
                "log": list(update.get("log") or []),
                "messages": list(update.get("messages") or []),
            }
        )
        self._flush()

    def update_snapshot(self, state: dict[str, Any]) -> None:
        """记录一次完整状态快照（每个 superstep 覆盖一次）。"""
        self.document["snapshot"] = _jsonable(state)
        self._flush()

    def finish(
        self,
        state: dict[str, Any],
        *,
        terminal: str,
        node_calls: dict[str, int],
        elapsed: float,
    ) -> None:
        self.document["snapshot"] = _jsonable(state)
        self.document["result"] = {
            "terminal": terminal,
            "node_calls": dict(node_calls),
            "elapsed_seconds": round(elapsed, 3),
            "iteration": state.get("iteration", 0),
            "findings": len(state.get("findings") or []),
            "errors": len(state.get("errors") or []),
        }
        self._flush()

    @property
    def enabled(self) -> bool:
        return not self.warning

    def _flush(self) -> None:
        if self.warning:
            return
        self.document["updated_at"] = now()
        tmp = self.path.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(self.document, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except OSError as exc:
            self.warning = f"写入运行记录失败（{exc}），本次不落盘"

    def __str__(self) -> str:
        return str(self.path)


def load_record(path: str | os.PathLike[str]) -> dict[str, Any]:
    """读取一份运行记录。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def plan_resume(path: str | os.PathLike[str]) -> tuple[dict[str, Any], str | None, dict[str, Any]]:
    """读取运行记录并推算续跑方案。

    返回 (恢复用的初始状态, 下一个节点, 原始记录)；下一个节点为 None 表示已到终点。
    """
    from multi_agent.edges import next_node_after

    document = load_record(path)
    state = dict(document.get("snapshot") or {})
    entry = next_node_after(document.get("last_node"), state)
    return state, entry, document
