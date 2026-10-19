"""写文件工具（对话 Agent 专用）：白名单 + 敏感文件拦截 + 每次写入人工确认。

三道闸：
1. 路径闸：目标必须在 config.ALLOWED_ROOTS 内，且 `resolve()` 之后仍在（防 `..` 与软链接逃逸）
2. 敏感闸：`.env` / `*.pem` / `id_rsa` 之类直接拒绝
3. 确认闸：每次写入（新建或覆盖）都回调 confirm()，由 CLI 展示预览/diff 并让用户拍板

工具本身不关心 UI 长什么样，只把"要写什么"整理成 WritePreview 交出去。
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from langchain_core.tools import tool

import config


@dataclass(frozen=True)
class WritePreview:
    """一次写入的预览信息，供 CLI 渲染与用户判断。"""

    path: Path
    exists: bool
    old_text: str
    new_text: str
    diff: str          # 覆盖已有文件时的 unified diff；新建时为空串
    reason: str        # 模型给出的"为什么写"

    @property
    def action(self) -> str:
        return "覆盖" if self.exists else "新建"


ConfirmFn = Callable[[WritePreview], bool]


def is_sensitive(path: Path) -> bool:
    """敏感文件：不允许写（和 file_reader 的黑名单保持一致口径）。"""
    if path.name.lower() in {name.lower() for name in config.DENIED_FILE_NAMES}:
        return True
    return path.suffix.lower() in config.DENIED_FILE_SUFFIXES


def resolve_target(path_str: str) -> Path:
    """把用户/模型给的路径解析成绝对路径（不做白名单判断）。"""
    raw = Path(path_str).expanduser()
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    return raw.resolve()


def prepare_write(path_str: str, content: str, *, reason: str = "") -> tuple[WritePreview | None, str]:
    """校验目标路径并组装预览。

    返回 (preview, error)：error 非空表示这次写入被拒绝，preview 为 None。
    """
    if not (path_str or "").strip():
        return None, "路径为空"

    try:
        target = resolve_target(path_str)
    except OSError as exc:
        return None, f"路径无法解析：{type(exc).__name__}: {exc}"

    if not config.is_path_allowed(target):
        return None, (
            f"目标不在允许的目录内：{target}"
            f"（当前允许：{config.describe_allowed_roots()}；可用 --root 追加）"
        )
    if is_sensitive(target):
        return None, f"拒绝写入敏感文件：{target.name}"
    if target.exists() and target.is_dir():
        return None, f"目标是目录而不是文件：{target}"

    old_text = ""
    if target.exists():
        try:
            old_text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # 读不出来就不敢覆盖：可能是二进制文件或没权限
            return None, f"无法读取原文件，拒绝覆盖：{type(exc).__name__}: {exc}"

    diff = make_diff(old_text, content, target) if target.exists() else ""
    preview = WritePreview(
        path=target,
        exists=target.exists(),
        old_text=old_text,
        new_text=content,
        diff=diff,
        reason=reason,
    )
    return preview, ""


def make_diff(old_text: str, new_text: str, path: Path) -> str:
    """生成 unified diff（行级），供用户判断改动范围。"""
    return "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{path.name}",
            tofile=f"b/{path.name}",
        )
    )


def make_write_file_tool(confirm: ConfirmFn):
    """构造 write_file 工具；confirm 返回 True 才真正落盘。"""

    @tool("write_file")
    def write_file(path: str, content: str, reason: str = "") -> str:
        """把内容写入指定文件（每次都会先请用户确认）。

        用于新建或覆盖代码/文档文件。参数：
        - path: 目标路径，相对路径按当前工作目录解析，且必须在允许的目录内
        - content: 要写入的完整文件内容（不是片段、不是 diff）
        - reason: 一句话说明为什么写这个文件，会展示给用户看
        """
        preview, error = prepare_write(path, content, reason=reason)
        if error or preview is None:
            return f"⛔ 已拒绝写入：{error}"

        if not confirm(preview):
            return "用户拒绝了这次写入，文件未改动。请先问清楚用户的意图再决定下一步。"

        try:
            preview.path.parent.mkdir(parents=True, exist_ok=True)
            preview.path.write_text(content, encoding="utf-8")
        except OSError as exc:
            return f"⛔ 写入失败：{type(exc).__name__}: {exc}"

        return f"✅ 已{preview.action} {preview.path}（{len(content)} 字符）"

    return write_file
