from __future__ import annotations

import logging

from app.agent import templates
from app.agent.schemas import ErrorResult, ImageAnalysis
from app.services import llm

logger = logging.getLogger(__name__)

BUSY_MSG = "系统繁忙，请稍后再试一次。"


def state_event(session) -> dict:
    return {
        "type": "state_update",
        "state": session.state.value,
        "collected": session.draft.to_dict(),
    }


def draft_confirm_event(session) -> dict:
    draft = session.draft
    return {
        "type": "draft_confirm",
        "draft": {
            "location": {
                "estate": draft.estate,
                "building": draft.building,
                "floor": draft.floor,
                "area": draft.area,
                "room": draft.room,
            },
            "description": draft.description,
            "visit_time": draft.visit_time,
            "image_urls": draft.image_urls,
            "fault_type": {
                "code": draft.fault_type_code,
                "displayName": draft.fault_type_name,
            },
            "repair_priority": draft.repair_priority_rag,
            "repair_type": draft.repair_type,
        },
    }


def busy_response(session, events: list[dict]) -> dict:
    session.history.append({"role": "assistant", "content": BUSY_MSG})
    events.append({"type": "text_delta", "content": BUSY_MSG})
    events.append(state_event(session))
    return {"events": events}


def emit_template(session, text: str, events: list[dict]) -> None:
    """模板文本按句切片发出，保留前端 SSE 体感。"""
    for chunk in templates.sentence_chunks(text):
        events.append({"type": "text_delta", "content": chunk})
    session.history.append({"role": "assistant", "content": text})


async def maybe_analyze_image(session, image_url: str | None) -> ImageAnalysis | ErrorResult | None:
    """有图片时调 VLM；同 image_url 命中缓存时直接复用。"""
    if not image_url:
        return None
    cached = session.image_analysis
    if cached and cached.image_url == image_url:
        return cached
    result = await llm.analyze_image(image_url)
    if isinstance(result, ImageAnalysis):
        session.image_analysis = result
        return result
    logger.warning("[image_analysis] 失败: %s", result)
    return result


def image_failed_response(session, events: list[dict]) -> dict:
    msg = '图片识别失败，请重新上传一张清晰的现场照片，或回复"跳过"继续提交。'
    session.history.append({"role": "assistant", "content": msg})
    events.append({"type": "text_delta", "content": msg})
    events.append(state_event(session))
    return {"events": events}


def image_affects_fault(image: ImageAnalysis | None) -> bool:
    if image is None or image.is_unclear:
        return False
    fields = image.visual_fields
    return bool(fields.description or image.visual_fault_summary)
