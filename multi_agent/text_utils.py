"""文本处理小工具：模型输出取文本、剥离代码块、提取 JSON 数组。"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"^\s*```[^\n]*\n(?P<body>.*?)\n?\s*```\s*$", re.DOTALL)
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\[.*?\])\s*```", re.DOTALL)
# 孤立代理字符：多字节编码不匹配读入的脏数据、或模型返回的畸形字节
# 它们会让 JSON / UTF-8 编码直接抛 UnicodeEncodeError
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
# 模型有时会把控制标记漏进正文（实测：fixer 输出末尾多了一行 </parameter>）
# 这种整行就是标签的内容在 Python 里一定是语法错误，且几乎不会是有效代码
_TAG_LINE_RE = re.compile(r"</?[A-Za-z_][A-Za-z0-9_:.-]*(\s[^>]*)?/?>")


def sanitize_text(text: str) -> str:
    """去掉孤立的代理字符，避免后续编码崩溃。"""
    return _SURROGATE_RE.sub("", text or "")


def message_text(response: Any) -> str:
    """把 LLM 返回内容统一取成字符串。

    content 可能是 str，也可能是内容块列表（如 [{"type": "text", "text": "..."}]），
    这里统一拍平成纯文本，避免节点里到处写 isinstance 判断。
    """
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return "" if content is None else str(content)


def strip_code_fence(text: str) -> str:
    """去掉 ```python ... ``` 包裹（如果模型加了的话）。"""
    text = (text or "").strip()
    match = _FENCE_RE.match(text)
    if match:
        return match.group("body").strip("\n")

    # 模型输出被截断（有开头 ``` 但没有收尾）时，至少去掉首行标记
    if text.startswith("```"):
        lines = text.split("\n")[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines)
    return text


def strip_model_artifacts(text: str) -> str:
    """去掉模型漏在首尾的控制标记行（如 </parameter>、<response>）。

    只处理首尾：中间出现的同名标签可能是字符串字面量里的真实内容，不动它。
    """
    lines = (text or "").split("\n")

    def _is_tag(line: str) -> bool:
        return bool(_TAG_LINE_RE.fullmatch(line.strip()))

    while lines and (not lines[0].strip() or _is_tag(lines[0])):
        lines.pop(0)
    while lines and (not lines[-1].strip() or _is_tag(lines[-1])):
        lines.pop()
    return "\n".join(lines)


def clean_code_output(text: str) -> str:
    """把模型返回的代码清洗成可直接使用的形式。

    顺序很重要：尾部的 </parameter> 会让收尾的 ``` 不在文本末尾，
    先剥代码块就匹配不上，所以必须先去控制标记再剥代码块。
    """
    return strip_code_fence(strip_model_artifacts(text))


def extract_json_array(text: str) -> list[Any] | None:
    """从模型输出中提取 JSON 数组，失败返回 None。

    依次尝试：整体解析 → ```json 代码块 → 第一个 [ 到最后一个 ]。
    """
    for candidate in _json_candidates(text):
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, list):
            return data
    return None


def _json_candidates(text: str) -> list[str]:
    text = text or ""
    candidates: list[str] = []
    stripped = text.strip()
    if stripped:
        candidates.append(stripped)

    block = _JSON_BLOCK_RE.search(text)
    if block:
        candidates.append(block.group(1))

    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start : end + 1])
    return candidates
