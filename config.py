"""集中配置（多 Agent 版）。

在原版单 Agent 配置基础上，扩展为三组独立的 LLM 配置：
- CODER_*   编码 Agent（deepseek-v4.1-flash）
- REVIEWER_* 审查 Agent（glm-5.3-flash）
- FIXER_*   修复 Agent（xiaomi-v2.6-pro）

所有参数均可通过环境变量或 .env 覆盖。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# 项目位置
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# 通用 LLM 参数（三个 Agent 共用的默认值）
# ---------------------------------------------------------------------------
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.3"))
# SDK 层不重试：重试统一由节点层（resilience.retry_call）负责。
# 两层都重试会相乘：3 次节点重试 × 3 次 SDK 重试 × 60s 超时 = 9 分钟才失败。
LLM_HTTP_MAX_RETRIES = int(os.getenv("LLM_HTTP_MAX_RETRIES", "0"))
LLM_REQUEST_TIMEOUT = float(os.getenv("LLM_REQUEST_TIMEOUT", "60"))

PLACEHOLDER_API_KEYS = frozenset({"", "your-api-key-here", "your-api-key", "sk-xxx"})


def _is_configured(key: str) -> bool:
    return key.strip() not in PLACEHOLDER_API_KEYS


# ---------------------------------------------------------------------------
# 编码 Agent（deepseek-v4.1-flash）
# ---------------------------------------------------------------------------
CODER_LLM_MODEL = os.getenv("CODER_LLM_MODEL", "deepseek-v4.1-flash")
CODER_LLM_API_KEY = os.getenv("CODER_LLM_API_KEY", "")
CODER_LLM_BASE_URL = os.getenv("CODER_LLM_BASE_URL") or None


def coder_configured() -> bool:
    return _is_configured(CODER_LLM_API_KEY)


# ---------------------------------------------------------------------------
# 审查 Agent（glm-5.3-flash）
# ---------------------------------------------------------------------------
REVIEWER_LLM_MODEL = os.getenv("REVIEWER_LLM_MODEL", "glm-5.3-flash")
REVIEWER_LLM_API_KEY = os.getenv("REVIEWER_LLM_API_KEY", "")
REVIEWER_LLM_BASE_URL = os.getenv("REVIEWER_LLM_BASE_URL") or None


def reviewer_configured() -> bool:
    return _is_configured(REVIEWER_LLM_API_KEY)


# ---------------------------------------------------------------------------
# 修复 Agent（xiaomi-v2.6-pro）
# ---------------------------------------------------------------------------
FIXER_LLM_MODEL = os.getenv("FIXER_LLM_MODEL", "xiaomi-v2.6-pro")
FIXER_LLM_API_KEY = os.getenv("FIXER_LLM_API_KEY", "")
FIXER_LLM_BASE_URL = os.getenv("FIXER_LLM_BASE_URL") or None


def fixer_configured() -> bool:
    return _is_configured(FIXER_LLM_API_KEY)


# ---------------------------------------------------------------------------
# 对话 Agent（工作模式的前台，默认复用 fixer 的 mimo 端点）
# ---------------------------------------------------------------------------
# 单独用 CHAT_LLM_* 可以指向别的模型；不填就跟着 FIXER_* 走
CHAT_LLM_MODEL = os.getenv("CHAT_LLM_MODEL", "mimo-v2.6-pro")
CHAT_LLM_API_KEY = os.getenv("CHAT_LLM_API_KEY") or FIXER_LLM_API_KEY
CHAT_LLM_BASE_URL = os.getenv("CHAT_LLM_BASE_URL") or FIXER_LLM_BASE_URL


def chat_configured() -> bool:
    return _is_configured(CHAT_LLM_API_KEY)


def all_agents_configured() -> bool:
    """三个 Agent 的 API Key 是否都已配置。"""
    return coder_configured() and reviewer_configured() and fixer_configured()


# ---------------------------------------------------------------------------
# Workflow 运行时
# ---------------------------------------------------------------------------
# 单次 invoke 允许的最大图步数（防止工具调用死循环）
RECURSION_LIMIT = int(os.getenv("AGENT_RECURSION_LIMIT", "40"))
# 修复-复审最大循环次数（业务层兜底，与 RECURSION_LIMIT 配合）
MAX_FIX_ROUNDS = int(os.getenv("WORKFLOW_MAX_FIX_ROUNDS", "3"))
# 送入模型的历史消息上限
MAX_HISTORY_MESSAGES = int(os.getenv("AGENT_MAX_HISTORY_MESSAGES", "40"))

# ---------------------------------------------------------------------------
# 问题严重级别 / 阻断阈值
# ---------------------------------------------------------------------------
# 只有 BLOCKING_SEVERITIES 里的级别才算"阻断问题"，会触发修复循环；
# 其余级别（默认 info）仍然完整保留在 findings 与运行记录里，但不阻止判定通过。
# 没有这个阈值时，模型审查会持续产出 style nit，流程永远收敛不到"通过"。
# 语义：通过 = 无阻断问题，不等于"零建议"。
KNOWN_SEVERITIES = ("critical", "warning", "info")
BLOCKING_SEVERITIES = frozenset(
    item.strip().lower()
    for item in os.getenv("WORKFLOW_BLOCKING_SEVERITIES", "critical,warning").split(",")
    if item.strip()
)

# 节点级重试（模型调用抖动不应该把整条 workflow 打崩）
NODE_MAX_ATTEMPTS = int(os.getenv("NODE_MAX_ATTEMPTS", "3"))
NODE_RETRY_BASE_DELAY = float(os.getenv("NODE_RETRY_BASE_DELAY", "0.6"))
NODE_RETRY_MAX_DELAY = float(os.getenv("NODE_RETRY_MAX_DELAY", "5.0"))
# 工具调用重试次数（比模型调用更保守）
TOOL_MAX_ATTEMPTS = int(os.getenv("TOOL_MAX_ATTEMPTS", "2"))
# fixer 自验失败后，带错误反馈再修一次的最大次数
FIXER_SELF_REPAIR_ATTEMPTS = int(os.getenv("FIXER_SELF_REPAIR_ATTEMPTS", "1"))
# 运行记录（JSON）输出目录，支持中断后 --resume
RUN_RECORD_DIR = os.getenv("RUN_RECORD_DIR", "runs")

# ---------------------------------------------------------------------------
# file_reader 的读取边界
# ---------------------------------------------------------------------------
MAX_FILE_BYTES = int(os.getenv("MAX_FILE_BYTES", str(1024 * 1024)))
MAX_FILE_CHARS = int(os.getenv("MAX_FILE_CHARS", "20000"))

DENIED_FILE_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".htpasswd",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "credentials",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
    }
)
DENIED_FILE_SUFFIXES = frozenset(
    {".pem", ".key", ".pfx", ".p12", ".keystore", ".jks", ".ppk", ".kdbx"}
)


def _to_path(value: os.PathLike[str] | str) -> Path:
    return Path(value).expanduser().resolve()


def _default_allowed_roots() -> list[Path]:
    raw = os.getenv("ALLOWED_ROOTS", "").strip()
    if raw:
        return [_to_path(p) for p in raw.split(os.pathsep) if p.strip()]
    return [Path.cwd().resolve()]


ALLOWED_ROOTS: list[Path] = _default_allowed_roots()


def add_allowed_root(path: os.PathLike[str] | str) -> Path:
    root = _to_path(path)
    if root not in ALLOWED_ROOTS:
        ALLOWED_ROOTS.append(root)
    return root


def _normalized(path: os.PathLike[str] | str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def is_path_allowed(path: os.PathLike[str] | str) -> bool:
    target = _normalized(path)
    for root in ALLOWED_ROOTS:
        prefix = _normalized(root).rstrip("\\/")
        if target == prefix or target.startswith(prefix + os.sep):
            return True
    return False


def describe_allowed_roots(roots: Iterable[Path] | None = None) -> str:
    items = list(roots) if roots is not None else list(ALLOWED_ROOTS)
    return "、".join(str(r) for r in items) if items else "（未配置）"


# ---------------------------------------------------------------------------
# code_executor 的执行限制
# ---------------------------------------------------------------------------
EXEC_TIMEOUT_SECONDS = int(os.getenv("EXEC_TIMEOUT_SECONDS", "10"))
EXEC_MAX_OUTPUT_CHARS = int(os.getenv("EXEC_MAX_OUTPUT_CHARS", "5000"))
EXEC_MAX_CODE_CHARS = int(os.getenv("EXEC_MAX_CODE_CHARS", "20000"))
# 沙箱执行的工作目录（留空用系统临时目录；受限环境下可指向工作区内目录）
EXEC_WORKDIR = os.getenv("EXEC_WORKDIR") or None

EXEC_ALLOWED_IMPORTS = frozenset(
    {
        "math",
        "cmath",
        "statistics",
        "random",
        "decimal",
        "fractions",
        "json",
        "re",
        "string",
        "textwrap",
        "datetime",
        "collections",
        "itertools",
        "functools",
        "heapq",
        "bisect",
        "copy",
        "enum",
        "dataclasses",
        "typing",
        "uuid",
        "hashlib",
        "base64",
        "unicodedata",
        "pprint",
        "operator",
    }
)

EXEC_DENIED_CALLS = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "open",
        "input",
        "__import__",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
        "breakpoint",
        "exit",
        "quit",
        "memoryview",
    }
)

EXEC_DENIED_ATTRS = frozenset(
    {
        "system",
        "popen",
        "spawn",
        "spawnl",
        "spawnv",
        "spawnve",
        "execv",
        "execve",
        "execvp",
        "fork",
        "forkpty",
        "kill",
        "killpg",
        "rmtree",
        "putenv",
        "getenv",
        "environ",
        "import_module",
    }
)
