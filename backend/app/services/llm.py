from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timedelta
from typing import AsyncIterator

from openai import AsyncOpenAI

from app.agent.prompts import (
    CONFIRM_CHECK_SYSTEM,
    EXTRACTION_SYSTEM,
    confirmation_system_prompt,
    editing_extract_prompt,
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


def _encode_image_to_data_uri(image_url: str) -> str | None:
    """将图片 URL 解析为 base64 data URI，供多模态消息使用。失败返回 None。"""
    from app.services.storage import read_image_bytes
    data = read_image_bytes(image_url)
    if data is None:
        return None
    # 根据文件头判断 MIME 类型
    if data[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        mime = "image/jpeg"
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _build_user_message(text: str, image_url: str | None) -> dict:
    """构造用户消息，有图片时使用多模态内容列表格式。"""
    if not image_url:
        return {"role": "user", "content": text}

    data_uri = _encode_image_to_data_uri(image_url)
    if data_uri is None:
        logger.warning("_build_user_message: 图片编码失败，降级为纯文本消息")
        return {"role": "user", "content": text}

    return {
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": data_uri}},
        ],
    }


async def extract_fields(
    draft: TicketDraft,
    user_message: str,
    image_url: str | None = None,
) -> dict:
    """非流式调用，返回提取到的字段 dict（含 image_description_text）。有图片时使用多模态消息格式。

    Args:
        draft: 当前工单草稿
        user_message: 用户消息
        image_url: 图片 URL（可选）

    Returns:
        dict: 包含 image_description_text（有图片时）和其他字段
    """
    user_text = extraction_user_prompt(
        json.dumps(draft.to_dict(), ensure_ascii=False),
        user_message,
        image_url,
    )
    user_msg = _build_user_message(user_text, image_url)
    try:
        resp = await _client.chat.completions.create(
            model=settings.qwen_model,
            messages=[
                {"role": "system", "content": EXTRACTION_SYSTEM},
                user_msg,
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            **_EXTRA,
        )
        return _clean_json(resp.choices[0].message.content)
    except Exception as exc:
        logger.warning("extract_fields failed: %s", exc)
        return {"_error": "llm_call_failed"}


async def extract_fields_editing(
    draft: TicketDraft,
    user_message: str,
    image_url: str | None = None,
) -> dict:
    """EDITING 阶段专用提取：明确告知 LLM 当前是修改场景，未提及字段返回 null。"""
    user_text = editing_extract_prompt(
        json.dumps(draft.to_dict(), ensure_ascii=False),
        user_message,
        image_url,
    )
    user_msg = _build_user_message(user_text, image_url)
    try:
        resp = await _client.chat.completions.create(
            model=settings.qwen_model,
            messages=[
                {"role": "system", "content": EXTRACTION_SYSTEM},
                user_msg,
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            **_EXTRA,
        )
        return _clean_json(resp.choices[0].message.content)
    except Exception as exc:
        logger.warning("extract_fields_editing failed: %s", exc)
        return {"_error": "llm_call_failed"}


_MAX_REPLY_HISTORY = 10


async def generate_reply_stream(
    draft: TicketDraft,
    history: list[dict],
    missing: list[str],
) -> AsyncIterator[str]:
    """流式生成追问回复，yield 文字片段。只传最近几轮 history 避免 token 浪费和幻觉。"""
    system = reply_system_prompt(
        json.dumps(draft.to_dict(), ensure_ascii=False),
        missing,
    )
    recent = history[-_MAX_REPLY_HISTORY:]
    messages = [{"role": "system", "content": system}, *recent]
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
    visit_time: str,
) -> AsyncIterator[str]:
    """流式生成工单确认摘要，yield 文字片段。不需要 history，纯基于 draft 生成。"""
    system = confirmation_system_prompt(
        json.dumps(draft.to_dict(), ensure_ascii=False),
        visit_time,
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": "请生成确认摘要。"}]
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
# 优先级1：完整词组（精确匹配，避免误触发）
_POSITIVE_PHRASES = {"是的", "对的", "好的", "没错", "正确", "确认", "确定", "可以的", "没问题", "对对对"}
# 优先级2：单字关键词（需要排除否定前缀）
_POSITIVE_SINGLE = {"是", "对", "好", "行", "嗯", "ok", "yes", "提交"}
_NEGATIVE = {"不", "错", "改", "修改", "重新", "不对", "不是", "取消", "no"}
# 包含这些前缀时，正向关键词命中视为无效（用户在提问而非确认）
_NEGATION_PREFIX = {"不", "没", "无需", "不需要", "需要吗", "吗", "？", "?"}


async def check_user_confirmed(text: str) -> bool:
    """判断用户是否确认了工单摘要；关键词无法判断时才调用 LLM。"""
    lower = text.lower().strip()

    # 第一步：检查否定词，直接拒绝
    for kw in _NEGATIVE:
        if kw in lower:
            logger.info("[确认判断] 检测到否定词 '%s'，返回 False", kw)
            return False

    # 第二步：优先匹配完整词组（"是的"、"对的"等），这些是明确的肯定回复
    for phrase in _POSITIVE_PHRASES:
        if phrase in lower:
            logger.info("[确认判断] 匹配到完整肯定词组 '%s'，返回 True", phrase)
            return True

    # 第三步：匹配单字关键词，但需要排除否定前缀和问句
    for kw in _POSITIVE_SINGLE:
        if kw in lower:
            idx = lower.index(kw)
            prefix = lower[:idx]
            # 关键词前有否定词，或整句是问句，交给 LLM 判断
            if any(neg in prefix for neg in _NEGATION_PREFIX) or lower.endswith(("吗", "？", "?")):
                logger.info("[确认判断] 关键词 '%s' 前有否定词或为问句，交给 LLM 判断", kw)
                break
            logger.info("[确认判断] 匹配到单字肯定词 '%s'，返回 True", kw)
            return True

    # 第四步：关键词无法判断，用 LLM 做一次轻量判断
    logger.info("[确认判断] 关键词无法判断，调用 LLM fallback: text='%s'", text)
    try:
        resp = await _client.chat.completions.create(
            model=settings.qwen_model,
            messages=[
                {"role": "system", "content": CONFIRM_CHECK_SYSTEM},
                {"role": "user", "content": text},
            ],
            max_tokens=10,  # 从 5 提升到 10，避免截断
            temperature=0,
            **_EXTRA,
        )
        result = resp.choices[0].message.content.strip().lower()
        is_confirmed = "true" in result
        logger.info("[确认判断] LLM 返回: '%s'，判断结果: %s", result, is_confirmed)
        return is_confirmed
    except Exception as exc:
        logger.warning("check_user_confirmed LLM fallback failed: %s", exc)
        return False

_DENIAL_INTENT_SYSTEM = """\
用户刚刚否认了一份报修工单摘要。判断用户意图属于以下哪种：
- modify：用户想修改某个具体字段（如时间、位置、描述），例如"时间改成下午三点"、"不是A栋是B栋"、"描述不对，是漏水不是灯坏"
- restart：用户完全否认所有内容，想重新开始，例如"全部不对"、"重新来"、"都错了"
- unclear：无法判断用户想修改什么，例如"不对"、"不是"、"有问题"

只输出 modify / restart / unclear 之一，不要输出任何解释。"""


async def classify_denial_intent(user_message: str) -> str:
    """判断用户否认确认摘要时的意图：modify / restart / unclear"""
    try:
        resp = await _client.chat.completions.create(
            model=settings.qwen_model,
            messages=[
                {"role": "system", "content": _DENIAL_INTENT_SYSTEM},
                {"role": "user", "content": user_message},
            ],
            max_tokens=10,
            temperature=0,
            **_EXTRA,
        )
        result = resp.choices[0].message.content.strip().lower()
        if result in ("modify", "restart", "unclear"):
            return result
        return "unclear"
    except Exception as exc:
        logger.warning("classify_denial_intent failed: %s", exc)
        return "unclear"


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

