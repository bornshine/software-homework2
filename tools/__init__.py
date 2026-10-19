"""工具集合：统一的注册入口。"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from tools.ast_analyzer import ast_analyzer
from tools.code_executor import code_executor
from tools.file_reader import file_reader
from tools.file_writer import WritePreview, is_sensitive, make_write_file_tool, prepare_write
from tools.sandbox_policy import check_code_policy

__all__ = [
    "WritePreview",
    "ast_analyzer",
    "check_code_policy",
    "code_executor",
    "file_reader",
    "get_all_tools",
    "is_sensitive",
    "make_write_file_tool",
    "prepare_write",
]

# 只读类工具：流水线的 reviewer / fixer 以及对话 Agent 都可复用
_TOOLS: tuple[BaseTool, ...] = (file_reader, code_executor, ast_analyzer)


def get_all_tools() -> list[BaseTool]:
    """返回所有可用的只读工具。"""
    return list(_TOOLS)
