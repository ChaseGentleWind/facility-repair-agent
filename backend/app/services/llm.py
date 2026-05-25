from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
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
        temperature=0.4,
        **_EXTRA,
    )
    async for chunk in stream:
        content = chunk.choices[0].delta.content or ""
        if content:
            yield content


async def generate_confirmation_stream(
    draft: TicketDraft,
    history: list[dict],
    visit_time: str,
) -> AsyncIterator[str]:
    """流式生成工单确认摘要，yield 文字片段。"""
    system = confirmation_system_prompt(
        json.dumps(draft.to_dict(), ensure_ascii=False),
        visit_time,
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
# 包含这些前缀时，正向关键词命中视为无效（用户在提问而非确认）
_NEGATION_PREFIX = {"不", "没", "无需", "不需要", "需要吗", "吗", "？", "?"}


async def check_user_confirmed(text: str) -> bool:
    """判断用户是否确认了工单摘要；关键词无法判断时才调用 LLM。"""
    lower = text.lower().strip()
    for kw in _NEGATIVE:
        if kw in lower:
            return False
    # 正向关键词命中前，先排除"否定词+关键词"或问句场景
    for kw in _POSITIVE:
        if kw in lower:
            idx = lower.index(kw)
            prefix = lower[:idx]
            # 关键词前有否定词，或整句是问句，交给 LLM 判断
            if any(neg in prefix for neg in _NEGATION_PREFIX) or lower.endswith(("吗", "？", "?")):
                break
            return True
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

_DEFAULT_KEYWORD = {"随便", "都行", "无所谓", "任意", "不限", "你定", "尽快", "快点", "越快越好", "马上"}
_RESOLVE_SYSTEM = """\
你是时间解析助手。将用户描述的时间转化为绝对时间。
已知当前时刻：{now_str}
规则：
- 输出格式严格为：M月D日 H时mm分（不带年份，如 5月25日 16时30分）
- "今天"指当天，"明天"指次日，"后天"指两天后
- "上午"默认9时，"下午"默认14时，"傍晚"默认17时，"晚上"默认19时
- "X分钟后"/"X小时后"基于当前时刻加算
- 如果描述模糊无法解析，输出：DEFAULT
只输出结果，不输出任何解释。"""


async def resolve_visit_time(raw_text: str, now: datetime) -> str:
    """将用户自然语言时间描述解析为 'M月D日 H时mm分' 格式的字符串。
    无法解析或用户随意/尽快时，返回 now + 30分钟。
    """
    default_time = now + timedelta(minutes=30)
    default_str = f"{default_time.month}月{default_time.day}日 {default_time.hour}时{default_time.minute:02d}分"

    if not raw_text:
        return default_str

    lower = raw_text.strip()
    if any(kw in lower for kw in _DEFAULT_KEYWORD):
        return default_str

    now_str = f"{now.year}年{now.month}月{now.day}日 {now.hour}时{now.minute:02d}分"
    system = _RESOLVE_SYSTEM.format(now_str=now_str)
    try:
        resp = await _client.chat.completions.create(
            model=settings.qwen_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": raw_text},
            ],
            max_tokens=30,
            temperature=0,
            **_EXTRA,
        )
        result = resp.choices[0].message.content.strip()
        if result == "DEFAULT" or not result:
            return default_str
        return result
    except Exception as exc:
        logger.warning("resolve_visit_time failed: %s", exc)
        return default_str

