"""输入意图识别：这条输入是要“产出代码”，还是在“问问题”。

三层判定，从便宜到贵：
1. 规则层：常见的“要代码”/“要问答”信号直接拍板，零延迟、离线可测
2. 模型层：规则拿不准时再问一次快模型
3. 兜底层：模型也失败就退回 code，保持旧行为不变

同时 CLI 提供 --mode 显式覆盖，演示时不想依赖分类就手动指定。
"""

from __future__ import annotations

import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from multi_agent.prompts import ROUTER_SYSTEM_PROMPT
from multi_agent.resilience import invoke_llm

MODE_CODE = "code"
MODE_CHAT = "chat"
MODES = (MODE_CODE, MODE_CHAT)

# 要“一份代码产物”的信号
_CODE_VERBS = (
    "写", "实现", "编写", "生成", "补全", "重构", "优化", "修复", "调试",
    "改一下", "改写", "加个", "加上", "帮我做", "帮我写", "做一个", "写个",
)
_CODE_NOUNS = (
    "函数", "方法", "类", "脚本", "程序", "代码", "算法", "模块", "接口",
    "爬虫", "正则", "测试", "demo", "def ", "class ", ".py",
)

# 问问题 / 自我介绍 / 问能力
_CHAT_PATTERNS = re.compile(
    r"(你能做什么|你能干什么|你会什么|你有什么能力|你是什么|你是谁|自我介绍|介绍一下你|"
    r"介绍一下自己|如何使用|怎么用|怎么使用|帮助我了解|你的功能|你的能力)"
)
_QUESTION_PREFIX = re.compile(
    r"^\s*(什么是|为什么|如何|怎么|怎样|请问|解释|说明|介绍|能不能|可以吗|是否|有没有)"
)


def rule_classify(text: str) -> str | None:
    """规则层：能确定就返回 code / chat，拿不准返回 None。"""
    text = (text or "").strip()
    if not text:
        return MODE_CHAT

    has_verb = any(verb in text for verb in _CODE_VERBS)
    has_noun = any(noun in text.lower() for noun in _CODE_NOUNS)

    # 明确要代码：动词 + 代码对象（先判，避免“怎么写快排”被当成纯提问）
    if has_verb and has_noun:
        return MODE_CODE
    # 长描述 + 代码对象，通常也是实现任务
    if has_noun and len(text) > 60:
        return MODE_CODE

    if _CHAT_PATTERNS.search(text):
        return MODE_CHAT
    if _QUESTION_PREFIX.match(text) and not has_verb:
        return MODE_CHAT
    return None


def llm_classify(text: str, llm: Any) -> str | None:
    """模型层：只接受严格的 code / chat，其他输出当没答。"""
    attempt = invoke_llm(
        llm,
        [
            SystemMessage(content=ROUTER_SYSTEM_PROMPT),
            HumanMessage(content=f"用户输入：{text}"),
        ],
        attempts=2,
    )
    if not attempt.ok:
        return None
    answer = (attempt.value or "").strip().lower()
    for mode in MODES:
        if answer.startswith(mode):
            return mode
    return None


def classify_intent(text: str, llm: Any | None = None) -> str:
    """完整判定链：规则 → 模型 → 兜底 code。"""
    rule = rule_classify(text)
    if rule:
        return rule
    if llm is not None:
        decided = llm_classify(text, llm)
        if decided:
            return decided
    return MODE_CODE
