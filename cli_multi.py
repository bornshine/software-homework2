#!/usr/bin/env python3
"""工作模式 CLI：一个能聊、能调工具、聊清楚就开工的多轮会话。

用法：
    python cli_multi.py                       # 进入工作模式
    python cli_multi.py --root D:\\code        # 追加允许读写的目录
    python cli_multi.py --max-rounds 2        # 流水线最多修 2 轮
    python cli_multi.py --graph out.mmd       # 离线导出流水线图结构后退出

会话内指令：
    /task <需求>    强制把这句话当代码任务执行（跳过模型判断）
    /resume <记录>  从某次运行记录继续跑流水线
    /clear          清空对话记忆
    /help           查看指令
    /exit           退出（quit / exit / q 同效）

分工：
    对话层 = chat（mimo-v2.6-pro，带记忆与工具）——陪你聊、查文件、跑验证、写文件
    流水线 = coder → reviewer → fixer——只在"任务提案被确认"后启动
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from typing import Any

from dotenv import load_dotenv
from langgraph.errors import GraphRecursionError
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

import config
from multi_agent import llm_factory
from multi_agent.conversation import (
    TaskProposal,
    build_conversation_agent,
    build_result_note,
    create_checkpointer,
    inject_system_note,
    run_turn,
)
from multi_agent.edges import ENTRY_NODES, NODE_CODER
from multi_agent.graph import build_workflow, describe_workflow_graph
from multi_agent.recorder import RunRecorder, plan_resume
from multi_agent.runner import (
    TERMINAL_ABORTED,
    TERMINAL_PASSED,
    RunResult,
    run_workflow,
)
from multi_agent.severity import blocking_findings, suggestions
from multi_agent.text_utils import sanitize_text
from tools import get_all_tools
from tools.file_writer import WritePreview, make_write_file_tool

load_dotenv()
console = Console()

BANNER = "🤝 多 Agent 工作模式（能聊 · 能查 · 能开工）"

EXIT_WORDS = {"exit", "quit", "q", "/exit", "/quit", "/q"}
HELP = """可用指令：
  /task <需求>     强制当代码任务执行（跳过模型判断）
  /resume <记录>   从 runs/xxx.json 继续跑流水线
  /clear           清空对话记忆（流水线运行记录不受影响）
  /help            显示这份帮助
  /exit            退出

直接输入就是聊天：可以随便问问题，也可以让我读文件、跑代码、
写文件（写之前一定会问你）。聊到需求清楚了，我会提一份任务提案给你确认。"""

SENDER_STYLE = {
    "coder": "cyan",
    "reviewer": "magenta",
    "fixer": "yellow",
    "system": "blue",
    "tools": "dim",
    "workflow": "green",
}
KIND_STYLE = {
    "handoff": "cyan",
    "request_fix": "yellow",
    "verdict": "green",
    "report": "dim",
    "error": "red",
}


# ---------------------------------------------------------------------------
# 参数与启动
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="cli_multi.py", description="多 Agent 工作模式")
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        metavar="DIR",
        help="允许读写 / 让 Agent 访问的目录，可重复指定",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=None,
        help=f"流水线最大修复轮次（默认 {config.MAX_FIX_ROUNDS}）",
    )
    parser.add_argument("--run-dir", type=str, default=None, metavar="DIR", help="运行记录目录")
    parser.add_argument("--no-record", action="store_true", help="不写运行记录 JSON")
    parser.add_argument(
        "--graph",
        nargs="?",
        const="workflow_graph.mmd",
        metavar="PATH",
        help="导出流水线图结构（Mermaid）后退出",
    )
    return parser.parse_args(argv)


def configure_stdio() -> None:
    """统一 stdio 编码。

    - stdout/stderr：Windows 传统控制台/管道可能是 GBK，直接打印 emoji 会崩
    - stdin：**只在非交互（管道 / 重定向）时**切 UTF-8。真实终端下
      input() 走控制台 API、本身就能正确解码中文，不能动；但管道输入是
      按字节解码的，编码不匹配会产生孤立代理字符，发往模型时 UTF-8
      编码会直接抛 UnicodeEncodeError（实测踩过一次）。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

    stdin = sys.stdin
    try:
        is_tty = stdin.isatty()
    except (ValueError, OSError):
        is_tty = True
    if not is_tty:
        reconfigure = getattr(stdin, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def check_credentials() -> bool:
    """检查三组流水线模型 + 对话模型是否都配好。"""
    missing: list[str] = []
    if not config.coder_configured():
        missing.append("CODER_LLM_API_KEY")
    if not config.reviewer_configured():
        missing.append("REVIEWER_LLM_API_KEY")
    if not config.fixer_configured():
        missing.append("FIXER_LLM_API_KEY")
    if not config.chat_configured():
        missing.append("CHAT_LLM_API_KEY（或 FIXER_LLM_API_KEY）")

    if not missing:
        return True
    console.print(
        Panel.fit(
            f"[red]⚠️  未配置以下密钥：{', '.join(missing)}[/red]\n\n"
            "请复制 .env.example 为 .env 并填写模型配置。",
            border_style="red",
        )
    )
    return False


def export_graph(destination: str) -> None:
    mermaid = describe_workflow_graph()
    with open(destination, "w", encoding="utf-8") as handle:
        handle.write(mermaid + "\n")
    console.print(f"[green]✅ 已导出图结构：{destination}[/green]\n")
    console.print(Panel(mermaid, title="流水线结构（Mermaid）", border_style="blue"))


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def render_chat_event(kind: str, payload: Any) -> None:
    """对话层的工具轨迹（工具调用的实时回显）。"""
    if kind == "tool_call":
        args = payload.get("args")
        console.print(f"[dim]🔧 调用 {payload.get('name')}({_brief(args)})[/dim]")
    elif kind == "tool_result":
        console.print(
            f"[dim]↩︎  {payload.get('name')} 返回 {payload.get('chars')} 字符[/dim]"
        )


def render_pipeline_event(kind: str, payload: Any) -> None:
    """流水线的事件渲染（沿用原来的日志 / 消息时间线）。"""
    if kind == "node":
        console.print(f"[bold]▶ {payload}[/bold]")
    elif kind == "log":
        console.print(f"  [dim]{payload}[/dim]")
    elif kind == "message":
        console.print(_message_line(payload))


def _brief(args: Any, limit: int = 90) -> str:
    try:
        text = json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(args)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def _message_line(message: dict[str, Any]) -> str:
    sender = message.get("sender", "?")
    recipient = message.get("recipient", "?")
    kind = message.get("kind", "?")
    summary = message.get("summary", "")
    sender_style = SENDER_STYLE.get(sender, "white")
    kind_style = KIND_STYLE.get(kind, "white")
    iteration = f" · 轮次 {message['iteration']}" if message.get("iteration") is not None else ""
    return (
        f"  ✉ [{sender_style}]{sender}[/] → [{sender_style}]{recipient}[/] "
        f"[{kind_style}]{kind}[/] {summary}[dim]{iteration}[/dim]"
    )


def _finding_lines(items: list[dict[str, Any]]) -> list[str]:
    return [
        f"  - [{item.get('severity', '?')}] {item.get('location', '?')}: {item.get('issue', '')}"
        for item in items
    ]


def _print_metrics(result: RunResult, state: dict[str, Any]) -> None:
    calls = ", ".join(f"{name}×{count}" for name, count in result.node_calls.items()) or "-"
    message_count = len(state.get("messages") or [])
    console.print(
        f"[dim]耗时 {result.elapsed:.1f}s | 图步数 {result.steps} | 节点调用 {calls} | "
        f"消息 {message_count} 条 | 修复轮次 {state.get('iteration', 0)}[/dim]"
    )
    if result.record_path:
        console.print(f"[dim]运行记录：{result.record_path}[/dim]")


def print_summary(result: RunResult) -> None:
    """流水线结果面板。"""
    state = result.state
    findings = state.get("findings") or []
    blocking = blocking_findings(findings)
    advice = suggestions(findings)
    errors = state.get("errors") or []
    fatal = [item for item in errors if item.get("fatal")]
    degraded = [item for item in errors if not item.get("fatal")]
    iteration = state.get("iteration", 0)

    console.print()
    if result.terminal == TERMINAL_PASSED:
        lines = ["✅ 审查通过，无阻断问题。"]
        if advice:
            lines.append(f"\n另有 {len(advice)} 条非阻断建议（未触发修复，仅记录）：")
            lines.extend(_finding_lines(advice))
        console.print(Panel("\n".join(lines), title="[bold green]流水线结果[/bold green]", border_style="green"))
    elif result.terminal == TERMINAL_ABORTED:
        console.print(
            Panel(
                "⚠️  流程中止（达到最大图步数）。"
                + ("\n" + "\n".join(f"  - {item.get('message', '')}" for item in fatal) if fatal else ""),
                title="[bold red]流水线结果[/bold red]",
                border_style="red",
            )
        )
    elif fatal:
        console.print(
            Panel(
                "❌ 流程失败：\n"
                + "\n".join(f"  - [{item.get('scope', '?')}] {item.get('message', '')}" for item in fatal),
                title="[bold red]流水线结果[/bold red]",
                border_style="red",
            )
        )
    else:
        console.print(
            Panel(
                f"⚠️  经过 {iteration} 轮修复，仍有 {len(blocking)} 个阻断问题未解决。"
                + ("\n" + "\n".join(_finding_lines(blocking)) if blocking else ""),
                title="[bold yellow]流水线结果[/bold yellow]",
                border_style="yellow",
            )
        )
        if advice:
            console.print(f"[dim]另有 {len(advice)} 条非阻断建议已记录[/dim]")

    if degraded:
        console.print(f"[dim]降级记录 {len(degraded)} 条（流程未中断）[/dim]")

    _print_metrics(result, state)
    console.print()

    code = state.get("current_code", "")
    if code:
        console.print(Panel(code, title="[bold blue]最终代码[/bold blue]", border_style="blue"))


# ---------------------------------------------------------------------------
# 写文件确认
# ---------------------------------------------------------------------------
def confirm_write(preview: WritePreview) -> bool:
    """写文件前的确认：展示理由 + 预览/diff，由用户拍板。"""
    console.print()
    console.print(
        Panel(
            f"[bold]{preview.action}[/bold] {preview.path}\n"
            f"[dim]{preview.reason or '（模型没有说明理由）'}[/dim]",
            title="[bold yellow]⚠️ 写入确认[/bold yellow]",
            border_style="yellow",
        )
    )
    if preview.exists:
        diff = preview.diff or "（内容没有变化）"
        if len(diff) > 4000:
            diff = diff[:4000] + "\n…（diff 过长，已截断）"
        console.print(Panel(diff, title="改动 diff", border_style="dim"))
    else:
        body = preview.new_text
        if len(body) > 2000:
            body = body[:2000] + "\n…（内容过长，已截断）"
        console.print(Panel(body, title="将新建的文件内容", border_style="dim"))

    try:
        choice = console.input("[bold]写入吗？[/bold] [green]y[/green]=写入 / [red]n[/red]=拒绝 › ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print("\n[yellow]已按拒绝处理[/yellow]")
        return False
    approved = choice in {"y", "yes", "是", "好", "ok"}
    console.print("[green]✅ 已确认写入[/green]" if approved else "[yellow]⏹ 已拒绝，文件未改动[/yellow]")
    return approved


# ---------------------------------------------------------------------------
# 对话与流水线
# ---------------------------------------------------------------------------
def build_chat_app(args: argparse.Namespace):
    """对话层：mimo + 只读工具 + 写文件工具（带确认）。"""
    tools = get_all_tools()
    tools.append(make_write_file_tool(confirm=confirm_write))
    return build_conversation_agent(
        llm=llm_factory.get_chat_llm(),
        tools=tools,
        checkpointer=create_checkpointer(),
    )


def chat_once(app: Any, text: str, thread_id: str) -> Any:
    console.print("[dim]🤔 思考中…[/dim]")
    return run_turn(app, text, thread_id, on_event=render_chat_event)


def run_pipeline(
    requirement: str,
    app: Any,
    thread_id: str,
    args: argparse.Namespace,
) -> RunResult | None:
    """用户确认后执行流水线，并把结果回灌进对话历史。"""
    max_rounds = args.max_rounds or config.MAX_FIX_ROUNDS
    run_id = uuid.uuid4().hex[:8]
    state = {
        "requirement": requirement,
        "iteration": 0,
        "max_iterations": max_rounds,
        "run_id": run_id,
        "findings": [],
        "messages": [],
        "errors": [],
        "log": [],
        "code_history": [],
        "review_history": [],
    }

    recorder: RunRecorder | None = None
    if not args.no_record:
        recorder = RunRecorder(
            run_id,
            requirement,
            directory=args.run_dir,
            max_iterations=max_rounds,
            entry=NODE_CODER,
        )
        if not recorder.enabled:
            console.print(f"[yellow]⚠️  {recorder.warning}[/yellow]")

    console.print(f"\n[cyan]🚀 开始执行流水线（run_id={run_id}，最多修 {max_rounds} 轮）…[/cyan]")
    try:
        # 用户已经确认过是代码任务，直接进 coder，不再让 router 二次判断
        workflow = build_workflow(entry=NODE_CODER)
        result = run_workflow(
            workflow, state, recorder=recorder, on_event=render_pipeline_event
        )
    except GraphRecursionError:
        console.print("[yellow]⚠️  已达到最大图步数，流程中止。[/yellow]")
        return None
    except KeyboardInterrupt:
        console.print("\n[yellow]⏹  已中断。可用 /resume 从运行记录续跑。[/yellow]")
        if recorder is not None and recorder.enabled:
            console.print(f"[dim]运行记录：{recorder}[/dim]")
        return None
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]❌ 流水线出错：{type(exc).__name__}: {exc}[/red]")
        return None

    print_summary(result)
    note = build_result_note(
        requirement=requirement,
        terminal=result.terminal,
        iteration=result.state.get("iteration", 0),
        findings=result.state.get("findings"),
        code=result.state.get("current_code", ""),
        record_path=result.record_path,
    )
    inject_system_note(app, thread_id, note)
    console.print("[dim]（结果已加入对话上下文，可以接着追问或要求修改）[/dim]")
    return result


def resume_pipeline(path: str, app: Any, thread_id: str, args: argparse.Namespace) -> None:
    """从运行记录续跑流水线。"""
    try:
        state, entry, document = plan_resume(path)
    except (OSError, ValueError) as exc:
        console.print(f"[red]❌ 读不了运行记录 {path}：{type(exc).__name__}: {exc}[/red]")
        return

    if not state:
        console.print(f"[red]❌ 记录里没有状态快照：{path}[/red]")
        return
    if entry is None:
        result = document.get("result") or {}
        console.print(f"[green]该运行已经结束（结论：{result.get('terminal', 'unknown')}），不用续跑。[/green]")
        return
    if entry not in ENTRY_NODES:
        console.print(f"[red]❌ 记录给出的起始节点不合法：{entry}[/red]")
        return

    requirement = state.get("requirement", "")
    run_id = uuid.uuid4().hex[:8]
    recorder: RunRecorder | None = None
    if not args.no_record:
        recorder = RunRecorder(
            run_id,
            requirement,
            directory=args.run_dir,
            max_iterations=state.get("max_iterations", args.max_rounds or config.MAX_FIX_ROUNDS),
            entry=entry,
            resumed_from=path,
        )

    console.print(f"[cyan]🚀 从 {path} 续跑（上一个完成节点={document.get('last_node') or '-'}，从 {entry} 开始）…[/cyan]")
    try:
        result = run_workflow(
            build_workflow(entry=entry),
            state,
            recorder=recorder,
            on_event=render_pipeline_event,
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]❌ 续跑失败：{type(exc).__name__}: {exc}[/red]")
        return

    print_summary(result)
    inject_system_note(
        app,
        thread_id,
        build_result_note(
            requirement=requirement,
            terminal=result.terminal,
            iteration=result.state.get("iteration", 0),
            findings=result.state.get("findings"),
            code=result.state.get("current_code", ""),
            record_path=result.record_path,
        ),
    )


def handle_proposal(
    proposal: TaskProposal,
    app: Any,
    thread_id: str,
    args: argparse.Namespace,
) -> None:
    """展示任务提案 → 等用户确认 → 执行流水线。"""
    body = [f"[bold]{proposal.requirement}[/bold]"]
    if proposal.summary:
        body.append(f"\n怎么做的：{proposal.summary}")
    body.append(
        f"\n执行方式：coder → reviewer → fixer（最多 {args.max_rounds or config.MAX_FIX_ROUNDS} 轮修复）"
    )
    console.print()
    console.print(
        Panel("\n".join(body), title="📋 任务提案", border_style="cyan")
    )

    while True:
        try:
            choice = console.input(
                "[bold]回车[/bold]=开始执行 / [bold]n[/bold]=先不做 / 直接说点什么=补充需求 › "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]已跳过这次任务[/yellow]")
            return

        if not choice:
            run_pipeline(proposal.requirement, app, thread_id, args)
            return
        if choice.lower() in {"n", "no", "不", "跳过"}:
            console.print("[yellow]⏹ 已跳过，没有执行流水线[/yellow]")
            inject_system_note(
                app, thread_id, "用户选择暂不执行上一个任务提案。可以问问他打算怎么调整。"
            )
            return

        # 用户补充了说明：当成下一轮对话继续聊
        turn = chat_once(app, choice, thread_id)
        if turn.answer:
            console.print(Panel(Markdown(turn.answer), title="[bold green]回答[/bold green]", border_style="green"))
        if turn.proposal:
            proposal = turn.proposal
            console.print(Panel(f"[bold]{proposal.requirement}[/bold]", title="📋 更新后的任务提案", border_style="cyan"))
            continue
        return


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    args = parse_args(argv)

    for root in args.root:
        try:
            config.add_allowed_root(root)
        except OSError as exc:
            console.print(f"[red]❌ 无效的 --root 目录：{root}（{exc}）[/red]")
            return 2

    if args.graph:
        export_graph(args.graph)
        return 0

    console.print(f"\n[bold blue]{BANNER}[/bold blue]")
    console.print(
        f"[dim]对话：{config.CHAT_LLM_MODEL}（能自由问答 · 读文件 · 跑验证 · 写文件）[/dim]"
    )
    console.print(
        f"[dim]流水线：coder → reviewer → fixer（确认后启动）；"
        f"可访问目录：{config.describe_allowed_roots()}[/dim]\n"
    )
    console.print(f"[dim]{HELP}[/dim]\n")

    if not check_credentials():
        return 1

    try:
        app = build_chat_app(args)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]❌ 初始化失败：{type(exc).__name__}: {exc}[/red]")
        return 1

    thread_id = uuid.uuid4().hex[:8]

    while True:
        try:
            raw = console.input("\n[bold cyan]你 ›[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]👋 再见！[/yellow]")
            return 0

        raw = sanitize_text(raw)
        if not raw:
            continue

        lowered = raw.lower()
        if lowered in EXIT_WORDS:
            console.print("\n[yellow]👋 再见！[/yellow]")
            return 0
        if lowered in {"/clear", "clear"}:
            thread_id = uuid.uuid4().hex[:8]
            console.print("[green]🧹 对话记忆已清空[/green]")
            continue
        if lowered in {"/help", "help", "?"}:
            console.print(f"[dim]{HELP}[/dim]")
            continue
        if lowered.startswith("/task"):
            requirement = raw[len("/task") :].strip()
            if not requirement:
                console.print("[yellow]用法：/task 写一个 median 函数，处理空列表[/yellow]")
                continue
            handle_proposal(
                TaskProposal(requirement=requirement, summary="（你手动指定的任务）", source="manual"),
                app,
                thread_id,
                args,
            )
            continue
        if lowered.startswith("/resume"):
            path = raw[len("/resume") :].strip()
            if not path:
                console.print("[yellow]用法：/resume runs/xxxx.json[/yellow]")
                continue
            resume_pipeline(path, app, thread_id, args)
            continue
        if raw.startswith("/"):
            console.print(f"[yellow]未知指令：{raw.split()[0]}（/help 看用法）[/yellow]")
            continue

        # 普通对话一轮
        try:
            turn = chat_once(app, raw, thread_id)
        except GraphRecursionError:
            console.print("[yellow]⚠️  这一步工具调用太久了，本轮中止。[/yellow]")
            continue
        except KeyboardInterrupt:
            console.print("\n[yellow]⏹  已中断本轮[/yellow]")
            continue
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]❌ 出错了：{type(exc).__name__}: {exc}[/red]")
            continue

        if turn.answer:
            console.print(
                Panel(Markdown(turn.answer), title="[bold green]回答[/bold green]", border_style="green")
            )
        elif not turn.proposal:
            console.print("[yellow]⚠️  这一轮没有生成回答，请再补充一句。[/yellow]")

        if turn.proposal:
            handle_proposal(turn.proposal, app, thread_id, args)


if __name__ == "__main__":
    sys.exit(main())
