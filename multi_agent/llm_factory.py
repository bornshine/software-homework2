"""三个 Agent 各自的 LLM 客户端工厂。

每个模型一个独立的 ChatOpenAI 实例（base_url / api_key / model 都不同），
用 lru_cache 保证进程内只建一次，复用底层连接池。

重试只在节点层做（resilience.retry_call），SDK 层 max_retries 默认 0，
避免两层重试相乘——否则一次卡死要等好几分钟才报错。
"""

from __future__ import annotations

from functools import lru_cache

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

import config


def _build(model: str, api_key: str, base_url: str | None) -> ChatOpenAI:
    return ChatOpenAI(
        model=model,
        api_key=api_key,  # type: ignore[arg-type]
        base_url=base_url,
        temperature=config.LLM_TEMPERATURE,
        max_retries=config.LLM_HTTP_MAX_RETRIES,
        timeout=config.LLM_REQUEST_TIMEOUT,
    )


@lru_cache(maxsize=1)
def get_coder_llm() -> BaseChatModel:
    """编码 Agent：deepseek-v4.1-flash。"""
    if not config.coder_configured():
        raise RuntimeError(
            "未配置 CODER_LLM_API_KEY：请在 .env 中填写编码 Agent 的密钥。"
        )
    return _build(config.CODER_LLM_MODEL, config.CODER_LLM_API_KEY, config.CODER_LLM_BASE_URL)


@lru_cache(maxsize=1)
def get_reviewer_llm() -> BaseChatModel:
    """审查 Agent：glm-5.3-flash。"""
    if not config.reviewer_configured():
        raise RuntimeError(
            "未配置 REVIEWER_LLM_API_KEY：请在 .env 中填写审查 Agent 的密钥。"
        )
    return _build(
        config.REVIEWER_LLM_MODEL,
        config.REVIEWER_LLM_API_KEY,
        config.REVIEWER_LLM_BASE_URL,
    )


@lru_cache(maxsize=1)
def get_fixer_llm() -> BaseChatModel:
    """修复 Agent：mimo-v2.6-pro（具体以 .env 为准）。"""
    if not config.fixer_configured():
        raise RuntimeError(
            "未配置 FIXER_LLM_API_KEY：请在 .env 中填写修复 Agent 的密钥。"
        )
    return _build(config.FIXER_LLM_MODEL, config.FIXER_LLM_API_KEY, config.FIXER_LLM_BASE_URL)


@lru_cache(maxsize=1)
def get_chat_llm() -> BaseChatModel:
    """对话 Agent（工作模式前台）：默认 mimo-v2.6-pro，支持 tool calling。"""
    if not config.chat_configured():
        raise RuntimeError(
            "未配置对话模型：请在 .env 里填 FIXER_LLM_API_KEY（对话默认复用"
            "fixer 的 mimo 端点），或单独填 CHAT_LLM_API_KEY。"
        )
    return _build(config.CHAT_LLM_MODEL, config.CHAT_LLM_API_KEY, config.CHAT_LLM_BASE_URL)
