"""AST 静态分析工具（从原项目复用）：代码结构、圈复杂度、常见问题。"""

from __future__ import annotations

import ast
from typing import Any

from langchain_core.tools import tool

_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
_COMPLEXITY_NODES = (
    ast.If,
    ast.While,
    ast.For,
    ast.AsyncFor,
    ast.IfExp,
    ast.ExceptHandler,
    ast.comprehension,
)
_HIGH_COMPLEXITY_THRESHOLD = 5


def _complexity_of(node: ast.AST) -> int:
    complexity = 1
    stack: list[ast.AST] = [node]
    while stack:
        current = stack.pop()
        for child in ast.iter_child_nodes(current):
            if isinstance(child, _FUNCTION_NODES):
                continue
            if isinstance(child, _COMPLEXITY_NODES):
                complexity += 1
            stack.append(child)
    return complexity


def _type_name(node: ast.AST | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ast.dump(node) if node is not None else ""


def analyze_code(code: str) -> str:
    """对 Python 代码做静态分析：提取函数与类、计算圈复杂度、发现常见问题。"""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"❌ 语法错误: 第 {exc.lineno} 行 - {exc.msg}"

    report: list[str] = []
    functions: list[dict[str, Any]] = []
    classes: list[dict[str, Any]] = []

    def collect(node: ast.AST, in_class: bool = False) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _FUNCTION_NODES):
                functions.append(
                    {
                        "name": child.name,
                        "lineno": child.lineno,
                        "args": len(child.args.args),
                        "is_method": in_class,
                    }
                )
                collect(child, in_class=False)
            elif isinstance(child, ast.ClassDef):
                method_count = sum(1 for item in child.body if isinstance(item, _FUNCTION_NODES))
                classes.append(
                    {"name": child.name, "lineno": child.lineno, "methods": method_count}
                )
                collect(child, in_class=True)
            else:
                collect(child, in_class=in_class)

    collect(tree)

    method_total = sum(1 for func in functions if func["is_method"])
    report.append(
        f"📦 代码结构: 函数 {len(functions)} 个"
        f"（模块级 {len(functions) - method_total} 个 / 类方法 {method_total} 个）, "
        f"类 {len(classes)} 个"
    )
    for func in functions:
        tag = "[方法] " if func["is_method"] else ""
        report.append(f"  · {tag}{func['name']}()  [行 {func['lineno']}] 参数 {func['args']} 个")
    for cls in classes:
        report.append(f"  · class {cls['name']}  [行 {cls['lineno']}] 方法 {cls['methods']} 个")

    complexities = [
        {"name": node.name, "lineno": node.lineno, "complexity": _complexity_of(node)}
        for node in ast.walk(tree)
        if isinstance(node, _FUNCTION_NODES)
    ]

    high = [item for item in complexities if item["complexity"] > _HIGH_COMPLEXITY_THRESHOLD]
    if high:
        report.append(f"\n⚠️  高复杂度函数（>{_HIGH_COMPLEXITY_THRESHOLD}）:")
        for item in sorted(high, key=lambda entry: -entry["complexity"]):
            report.append(f"  - {item['name']}(): 复杂度 {item['complexity']}")
    else:
        report.append("\n✅ 所有函数复杂度合理")

    issues: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            if node.type is None:
                issues.append({"type": "规范", "line": node.lineno, "msg": "裸 except，会吞掉所有异常"})
            elif all(isinstance(stmt, ast.Pass) for stmt in node.body):
                issues.append(
                    {
                        "type": "规范",
                        "line": node.lineno,
                        "msg": f"`except {_type_name(node.type)}: pass` 静默吞掉异常",
                    }
                )
        elif isinstance(node, _FUNCTION_NODES):
            defaults = list(node.args.defaults) + [d for d in node.args.kw_defaults if d]
            for default in defaults:
                if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                    issues.append(
                        {
                            "type": "Bug",
                            "line": node.lineno,
                            "msg": f"{node.name}() 使用了可变默认参数",
                        }
                    )

    if issues:
        report.append("\n🔍 发现问题:")
        for issue in issues:
            report.append(f"  - [{issue['type']}] 行 {issue['line']}: {issue['msg']}")
    else:
        report.append("\n✅ 未发现明显静态问题")

    return "\n".join(report)


@tool
def ast_analyzer(code: str) -> str:
    """对 Python 代码做静态分析：提取函数与类、计算圈复杂度、发现常见问题。

    当需要了解代码结构、判断函数是否过于复杂时使用。
    """
    return analyze_code(code)
