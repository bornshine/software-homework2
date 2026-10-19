"""代码执行前的静态策略校验（AST 层，从原项目复用）。

这不是安全沙箱边界，而是一道「策略检查」：
拦下常见的越权写法，并把明确的中文原因反馈给 LLM。
"""

from __future__ import annotations

import ast

from config import (
    EXEC_ALLOWED_IMPORTS,
    EXEC_DENIED_ATTRS,
    EXEC_DENIED_CALLS,
    EXEC_MAX_CODE_CHARS,
)


# 无害的双下划线名字：__name__ 是标准写法（if __name__ == "__main__"），
# 拦掉它会让大量正常代码的"运行验证"直接降级。真正的逃逸向量是
# __class__ / __globals__ / __subclasses__ / __builtins__ 这类属性链，继续拦。
_ALLOWED_DUNDERS = frozenset({"__name__", "__doc__"})


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def check_code_policy(code: str) -> str | None:
    """校验代码是否允许执行。返回 None 表示通过；否则返回拒绝原因。"""
    if not code or not code.strip():
        return "代码为空。"

    if len(code) > EXEC_MAX_CODE_CHARS:
        return f"代码过长（{len(code)} 字符），上限 {EXEC_MAX_CODE_CHARS} 字符。"

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"语法错误：第 {exc.lineno} 行 - {exc.msg}"

    for node in ast.walk(tree):
        reason = _check_node(node)
        if reason:
            return reason
    return None


def _check_node(node: ast.AST) -> str | None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            top = alias.name.split(".")[0]
            if top not in EXEC_ALLOWED_IMPORTS:
                return f"不允许 import 模块 '{top}'；允许的模块：{_allowed_imports_hint()}"
    elif isinstance(node, ast.ImportFrom):
        if node.level:
            return "不允许相对导入。"
        top = (node.module or "").split(".")[0]
        if top not in EXEC_ALLOWED_IMPORTS:
            return f"不允许 from {node.module} import ...；允许的模块：{_allowed_imports_hint()}"
    elif isinstance(node, ast.Call):
        name = _call_name(node)
        if name in EXEC_DENIED_CALLS:
            return f"不允许调用 '{name}()'。"
        if name in EXEC_DENIED_ATTRS:
            return f"不允许调用属性方法 '{name}()'。"
    elif isinstance(node, ast.Attribute):
        if node.attr.startswith("__") and node.attr not in _ALLOWED_DUNDERS:
            return f"不允许访问双下划线属性 '{node.attr}'。"
    elif isinstance(node, ast.Name):
        if node.id.startswith("__") and node.id not in _ALLOWED_DUNDERS:
            return f"不允许使用双下划线名字 '{node.id}'。"
    return None


def _allowed_imports_hint() -> str:
    return ", ".join(sorted(EXEC_ALLOWED_IMPORTS))
