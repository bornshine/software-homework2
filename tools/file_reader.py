"""文件读取工具（从原项目复用）。

安全边界：
1. 只允许读取 ALLOWED_ROOTS 之内（解析软链接后）的文件；
2. .env / *.pem / id_rsa 等敏感文件一律拒绝；
3. 超过 MAX_FILE_BYTES 的文件直接拒绝，内容超过 MAX_FILE_CHARS 时截断。
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.tools import tool

import config


def _denied_by_name(path: Path) -> bool:
    name = path.name.lower()
    return name in config.DENIED_FILE_NAMES or path.suffix.lower() in config.DENIED_FILE_SUFFIXES


@tool
def file_reader(file_path: str) -> str:
    """读取指定路径的代码文件内容。

    当需要审查某段代码、或读取项目中的文件时使用。
    只能读取允许目录内的文件，且会拒绝 .env、证书、私钥等敏感文件。
    """
    if not file_path or not file_path.strip():
        return "错误：未提供文件路径。"

    try:
        path = Path(file_path).expanduser().resolve()
    except OSError as exc:
        return f"错误：路径无法解析 - {file_path}（{exc}）"

    if _denied_by_name(path):
        return f"错误：出于安全考虑，拒绝读取敏感文件 - {path.name}"

    if not config.is_path_allowed(path):
        return (
            f"错误：路径不在允许的目录内 - {path}\n"
            f"当前允许的目录：{config.describe_allowed_roots()}\n"
            f"如确需审查该文件，请用 `python cli_multi.py --root <目录>` 重新启动。"
        )

    if not path.exists():
        return f"错误：文件不存在 - {file_path}"
    if not path.is_file():
        return f"错误：不是文件 - {file_path}"

    try:
        file_size = path.stat().st_size
    except OSError as exc:
        return f"错误：无法获取文件信息 - {file_path}（{exc}）"

    if file_size > config.MAX_FILE_BYTES:
        return (
            f"错误：文件过大（{file_size / 1024:.1f}KB），"
            f"超过读取限制（{config.MAX_FILE_BYTES / 1024:.1f}KB）"
        )

    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"错误：文件不是 UTF-8 文本格式 - {file_path}"
    except PermissionError:
        return f"错误：没有权限读取文件 - {file_path}"
    except OSError as exc:
        return f"错误：读取文件失败 - {file_path}（{exc}）"

    total_lines = content.count("\n") + 1
    total_chars = len(content)
    truncated = total_chars > config.MAX_FILE_CHARS
    if truncated:
        content = content[: config.MAX_FILE_CHARS]

    result = f"📄 文件: {path.name}\n"
    result += f"📊 总行数: {total_lines}，总字符数: {total_chars}\n"
    if truncated:
        result += f"⚠️ 内容过长，已截断为前 {config.MAX_FILE_CHARS} 个字符\n"
    result += "=" * 60 + "\n" + content
    return result
