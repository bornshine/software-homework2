"""在受限环境中执行 Python 代码片段（从原项目复用，补结构化结果）。

对外提供两个层次：
- execute_code(code) -> dict    结构化结果，节点内部自验时直接消费，不再解析字符串
- code_executor(code) -> str    LangChain 工具封装，给模型看的文本结果
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
from typing import Any

from langchain_core.tools import tool

import config
from tools.sandbox_policy import check_code_policy

_ENV_KEEP = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "LANG",
    "LC_ALL",
)


def build_child_env() -> dict[str, str]:
    env = {key: os.environ[key] for key in _ENV_KEEP if key in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def kill_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _truncate(text: str) -> str:
    limit = config.EXEC_MAX_OUTPUT_CHARS
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（输出过长，仅保留前 {limit} 个字符）"


def _blank_result(**overrides: Any) -> dict[str, Any]:
    """构造一份结构化的执行结果。"""
    result: dict[str, Any] = {
        "ok": False,
        "skipped": False,      # True 表示因安全策略未执行
        "timed_out": False,
        "policy_reason": "",    # 策略拒绝原因（skipped=True 时有值）
        "error": "",            # 环境类错误（临时目录不可写、子进程起不来等）
        "returncode": None,
        "stdout": "",
        "stderr": "",
    }
    result.update(overrides)
    return result


def execute_code(code: str) -> dict[str, Any]:
    """在受限环境中执行代码，返回结构化结果（不抛异常）。

    字段：ok / skipped / timed_out / policy_reason / error / returncode / stdout / stderr
    """
    reason = check_code_policy(code)
    if reason:
        return _blank_result(skipped=True, policy_reason=reason)

    try:
        workdir = tempfile.mkdtemp(prefix="ma_exec_", dir=config.EXEC_WORKDIR or None)
    except OSError as exc:
        return _blank_result(error=f"无法创建临时目录：{type(exc).__name__}: {exc}")

    script_path = os.path.join(workdir, "snippet.py")

    try:
        try:
            with open(script_path, "w", encoding="utf-8") as handle:
                handle.write(code)
        except OSError as exc:
            return _blank_result(error=f"无法写入临时脚本：{type(exc).__name__}: {exc}")

        popen_kwargs: dict[str, object] = {
            "cwd": workdir,
            "env": build_child_env(),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(  # type: ignore[call-overload]
                [sys.executable, "-I", script_path], **popen_kwargs
            )
        except OSError as exc:
            return _blank_result(error=f"无法启动子进程：{type(exc).__name__}: {exc}")

        try:
            stdout, stderr = proc.communicate(timeout=config.EXEC_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            kill_process_tree(proc)
            return _blank_result(
                timed_out=True,
                error=f"执行超时（{config.EXEC_TIMEOUT_SECONDS} 秒）",
            )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    return _blank_result(
        ok=proc.returncode == 0,
        returncode=proc.returncode,
        stdout=stdout or "",
        stderr=stderr or "",
    )


def format_exec_result(result: dict[str, Any]) -> str:
    """把结构化结果渲染成给模型看的文本。"""
    if result.get("skipped"):
        return f"⛔ 已拒绝执行：{result.get('policy_reason', '')}"
    if result.get("error"):
        prefix = "⏰" if result.get("timed_out") else "🛠️"
        return f"{prefix} 执行环境异常：{result['error']}"

    parts: list[str] = []
    if result.get("stdout"):
        parts.append(f"📤 标准输出:\n{_truncate(result['stdout'])}")
    if result.get("stderr"):
        parts.append(f"❌ 错误输出:\n{_truncate(result['stderr'])}")
    if not parts:
        parts.append("（没有任何输出）")

    ok = bool(result.get("ok"))
    status = "正常完成" if ok else f"异常退出(码:{result.get('returncode')})"
    parts.append(f"\n{'✅' if ok else '⚠️'} {status}")
    return "\n".join(parts)


@tool
def code_executor(code: str) -> str:
    """在受限的临时目录中执行一小段 Python 代码并返回输出。

    当需要验证代码是否有 Bug、确认运行结果时使用。
    只接受受限的 Python 子集：不允许 import os/subprocess/sys 等模块，
    不允许 open/eval/exec/getattr 等调用。
    """
    return format_exec_result(execute_code(code))
