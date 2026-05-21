from __future__ import annotations

import json
import logging
from typing import AsyncIterator

from openai import AsyncOpenAI

from app.agent.prompts import (
    CONFIRM_CHECK_SYSTEM,
    EXTRACTION_SYSTEM,
    confirmation_system_prompt,
    extraction_user_prompt,
    reply_system_prompt,
)
from app.agent.state import TicketDraft
from app.config import settings

logger = logging.getLogger(__name__)


def _clean_json(raw: str) -> dict:
    """尝试从 LLM 返回的原始文本中提取第一个有效 JSON 对象。"""
    text = raw.strip()
    if text.startswith("```"):
        first_nl = text.index("\n") if "\n" in text else 3
        text = text[first_nl + 1 :]
        if text.endswith("```"):
            text = text[: -3]
        text = text.strip()
    decoder = json.JSONDecoder()
    obj, _ = decoder.raw_decode(text)
    return obj

_client = AsyncOpenAI(
    api_key=settings.qwen_api_key,
    base_url=settings.qwen_base_url,
)

# Qwen3 系列模型默认开启思考链，flash 变体关闭，若接口报错可删除 extra_body
_EXTRA = {"extra_body": {"enable_thinking": False}}


async def extract_fields(
    draft: TicketDraft,
    user_message: str,
    image_url: str | None = None,
) -> dict:
    """非流式调用，返回提取到的字段 dict。调用失败时返回空 dict。"""
    user_content = extraction_user_prompt(
        json.dumps(draft.to_dict(), ensure_ascii=False),
        user_message,
        image_url,
    )
    try:
        resp = await _client.chat.completions.create(
            model=settings.qwen_model,
            messages=[
                {"role": "system", "content": EXTRACTION_SYSTEM},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            **_EXTRA,
        )
        return _clean_json(resp.choices[0].message.content)
    except Exception as exc:
        logger.warning("extract_fields failed: %s", exc)
        return {}


async def generate_reply_stream(
    draft: TicketDraft,
    history: list[dict],
    missing: list[str],
) -> AsyncIterator[str]:
    """流式生成追问回复，yield 文字片段。"""
    system = reply_system_prompt(
        json.dumps(draft.to_dict(), ensure_ascii=False),
        missing,
    )
    messages = [{"role": "system", "content": system}, *history]
    stream = await _client.chat.completions.create(
        model=settings.qwen_model,
        messages=messages,
        stream=True,
        temperature=0.7,
        **_EXTRA,
    )
    async for chunk in stream:
        content = chunk.choices[0].delta.content or ""
        if content:
            yield content


async def generate_confirmation_stream(
    draft: TicketDraft,
    history: list[dict],
) -> AsyncIterator[str]:
    """流式生成工单确认摘要，yield 文字片段。"""
    system = confirmation_system_prompt(
        json.dumps(draft.to_dict(), ensure_ascii=False)
    )
    messages = [{"role": "system", "content": system}, *history]
    stream = await _client.chat.completions.create(
        model=settings.qwen_model,
        messages=messages,
        stream=True,
        temperature=0.3,
        **_EXTRA,
    )
    async for chunk in stream:
        content = chunk.choices[0].delta.content or ""
        if content:
            yield content


# 快速关键词判断，避免额外 LLM 调用
_POSITIVE = {"确认", "是", "对", "好", "好的", "没错", "正确", "确定", "可以", "ok", "yes", "行", "提交"}
_NEGATIVE = {"不", "错", "改", "修改", "重新", "不对", "不是", "取消", "no"}


async def check_user_confirmed(text: str) -> bool:
    """判断用户是否确认了工单摘要；关键词无法判断时才调用 LLM。"""
    lower = text.lower().strip()
    for kw in _POSITIVE:
        if kw in lower:
            return True
    for kw in _NEGATIVE:
        if kw in lower:
            return False
    # 关键词无法判断，用 LLM 做一次轻量判断
    try:
        resp = await _client.chat.completions.create(
            model=settings.qwen_model,
            messages=[
                {"role": "system", "content": CONFIRM_CHECK_SYSTEM},
                {"role": "user", "content": text},
            ],
            max_tokens=5,
            temperature=0,
            **_EXTRA,
        )
        return "true" in resp.choices[0].message.content.lower()
    except Exception as exc:
        logger.warning("check_user_confirmed LLM fallback failed: %s", exc)
        return False
