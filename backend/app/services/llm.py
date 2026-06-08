"""LLM 业务函数：拆分后只剩"任务"层，调用治理由 llm_client 统一处理。

公开函数：
- analyze_image：单次 VLM 调用 → ImageAnalysis（之前 extract_fields + describe + conflict 三连发的合体）
- extract_text_fields：纯文本字段抽取 → TextExtraction
- classify_confirmation_intent：确认意图 + 修改字段一次给出 → ConfirmationIntent
- generate_reply_stream：唯一保留的 LLM 流式生成（追问缺失字段）
- resolve_visit_time：自然语言时间 → "M月D日 H时mm分"
- _encode_image_to_data_uri：供其他模块（如 rag.py）复用
"""
from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timedelta
from typing import AsyncIterator

from app.agent.prompts import (
    CONFIRMATION_INTENT_SYSTEM,
    IMAGE_ANALYSIS_SYSTEM,
    TEXT_EXTRACTION_SYSTEM,
    confirmation_intent_user_prompt,
    image_analysis_user_prompt,
    reply_system_prompt,
    resolve_visit_time_system,
    text_extraction_editing_user_prompt,
    text_extraction_user_prompt,
)
from app.agent.schemas import (
    ConfirmationIntent,
    ErrorResult,
    ImageAnalysis,
    TextExtraction,
)
from app.agent.state import TicketDraft
from app.config import settings
from app.services import llm_client

logger = logging.getLogger(__name__)


# ── 图片编码（供 rag.py 等其他模块复用）─────────────────────────────────────
def _encode_image_to_data_uri(image_url: str) -> str | None:
    """将图片 URL 解析为 base64 data URI，供多模态消息使用。失败返回 None。"""
    from app.services.storage import read_image_bytes
    data = read_image_bytes(image_url)
    if data is None:
        return None
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


def _multimodal_user_content(text: str, image_url: str) -> list[dict]:
    data_uri = _encode_image_to_data_uri(image_url)
    if data_uri is None:
        return [{"type": "text", "text": text}]
    return [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": data_uri}},
    ]


# ── 图片分析（VLM，一次调用产出 ImageAnalysis，按 image_url 在 Session 缓存）

async def analyze_image(image_url: str) -> ImageAnalysis | ErrorResult:
    """单次 VLM 调用，产出 ImageAnalysis。失败返回 ErrorResult。

    替代旧的 extract_fields(图片描述) + _describe_image_fault + _check_semantic_conflict 三连发。
    """
    user_text = image_analysis_user_prompt(image_url)
    content = _multimodal_user_content(user_text, image_url)
    if not content or len(content) == 1:
        # 编码失败：图片不可读
        logger.warning("analyze_image: 图片编码失败 image_url=%s", image_url)
        return ErrorResult(
            code="llm_call_failed",
            purpose="image_analysis",
            message="image encode failed",
        )
    result = await llm_client.structured_call(
        ImageAnalysis,
        purpose="image_analysis",
        system=IMAGE_ANALYSIS_SYSTEM,
        user=content,
        temperature=0.1,
        max_tokens=400,
    )
    # 兜底：LLM 没回填 image_url 时手动写回
    if isinstance(result, ImageAnalysis) and not result.image_url:
        result = result.model_copy(update={"image_url": image_url})
    return result


# ── 文本字段抽取（不含图片观察） ─────────────────────────────────────────────

async def extract_text_fields(
    draft: TicketDraft,
    user_message: str,
    pending_clarification: dict | None = None,
    *,
    editing: bool = False,
) -> TextExtraction | ErrorResult:
    """纯文本字段抽取。editing=True 时使用修改场景的 user prompt。"""
    draft_json = json.dumps(draft.to_dict(), ensure_ascii=False)
    if editing:
        user_text = text_extraction_editing_user_prompt(
            draft_json, user_message, pending_clarification
        )
    else:
        user_text = text_extraction_user_prompt(
            draft_json, user_message, pending_clarification
        )
    return await llm_client.structured_call(
        TextExtraction,
        purpose="text_extract",
        system=TEXT_EXTRACTION_SYSTEM,
        user=user_text,
        temperature=0.1,
    )


# ── 确认意图分类（含修改字段，一次调用） ─────────────────────────────────────

# 关键词快速路径：命中即返，节约一次 LLM 调用
_POSITIVE_PHRASES = {
    "是的",
    "对的",
    "好的",
    "没错",
    "正确",
    "确认",
    "确定",
    "可以的",
    "没问题",
    "对对对",
    "就这样",
    "提交",
    "生成预览",
    "生成工单预览",
}
_POSITIVE_SINGLE = {"是", "对", "好", "行", "嗯", "ok", "yes"}
_NEGATIVE = {"不", "错", "改", "修改", "重新", "不对", "不是", "取消", "no", "换"}
_NEGATION_PREFIX = {"不", "没", "无需", "不需要", "需要吗", "吗", "？", "?"}
_RESTART_PHRASES = {"重新来", "全部不对", "都错了", "重来", "重新填", "重新开始"}


def _keyword_intent(user_message: str) -> str | None:
    """关键词快速判断意图。命中返回 'confirm'/'restart'，否则返回 None 交给 LLM。"""
    lower = user_message.lower().strip()
    if not lower:
        return None
    for phrase in _RESTART_PHRASES:
        if phrase in lower:
            return "restart"
    has_negative = any(kw in lower for kw in _NEGATIVE)
    if has_negative:
        return None
    for phrase in _POSITIVE_PHRASES:
        if phrase in lower:
            return "confirm"
    for kw in _POSITIVE_SINGLE:
        if kw in lower:
            idx = lower.index(kw)
            prefix = lower[:idx]
            if any(neg in prefix for neg in _NEGATION_PREFIX) or lower.endswith(("吗", "？", "?")):
                return None
            return "confirm"
    return None


async def classify_confirmation_intent(
    draft: TicketDraft,
    user_message: str,
) -> ConfirmationIntent | ErrorResult:
    """一次调用返回意图（confirm/modify/restart/unclear）+ 修改字段。

    关键词命中（"是的"/"重新来"等）会先于 LLM 拦截，返回 confirm/restart。
    """
    fast = _keyword_intent(user_message)
    if fast is not None:
        return ConfirmationIntent(intent=fast)

    draft_json = json.dumps(draft.to_dict(), ensure_ascii=False)
    user_text = confirmation_intent_user_prompt(draft_json, user_message)
    return await llm_client.structured_call(
        ConfirmationIntent,
        purpose="confirmation_intent",
        system=CONFIRMATION_INTENT_SYSTEM,
        user=user_text,
        temperature=0,
    )


# ── 流式追问（唯一保留的 LLM 流式生成） ─────────────────────────────────────

_MAX_REPLY_HISTORY = 10


async def generate_reply_stream(
    draft: TicketDraft,
    history: list[dict],
    missing: list[str],
) -> AsyncIterator[str]:
    """流式生成追问回复，yield 文字片段。只传最近 10 条 history 避免 token 浪费。"""
    system = reply_system_prompt(
        json.dumps(draft.to_dict(), ensure_ascii=False),
        missing,
    )
    recent = history[-_MAX_REPLY_HISTORY:]
    messages = [{"role": "system", "content": system}, *recent]
    async for chunk in llm_client.stream_call(
        purpose="reply_stream",
        messages=messages,
        temperature=0.4,
    ):
        yield chunk


# ── 时间解析 ──────────────────────────────────────────────────────────────────

_DEFAULT_KEYWORD = {"随便", "都行", "无所谓", "任意", "不限", "你定", "尽快", "快点", "越快越好", "马上", "越早越好", "时间越快愈好", "快点来"}


async def resolve_visit_time(raw_text: str, now: datetime) -> str:
    """自然语言时间 → 'M月D日 H时mm分'。无法解析或用户随意/尽快时返回 now+30 分钟。"""
    default_time = now + timedelta(minutes=30)
    default_str = f"{default_time.month}月{default_time.day}日 {default_time.hour}时{default_time.minute:02d}分"

    if not raw_text:
        return default_str

    lower = raw_text.strip()
    if any(kw in lower for kw in _DEFAULT_KEYWORD):
        return default_str

    now_str = f"{now.year}年{now.month}月{now.day}日 {now.hour}时{now.minute:02d}分"
    system = resolve_visit_time_system(now_str)
    raw = await llm_client.text_call(
        purpose="resolve_visit_time",
        system=system,
        user=raw_text,
        temperature=0,
        max_tokens=30,
    )
    if not raw:
        return default_str
    result = raw.strip()
    if result == "DEFAULT":
        return default_str
    return result
