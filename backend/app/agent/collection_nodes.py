from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from app.agent import templates
from app.agent.conflict_nodes import resolve_pending_conflict, set_pending_conflict
from app.agent.draft_ops import (
    apply_extraction,
    infer_location_from_area_or_room,
    merge_extraction,
)
from app.agent.graph_state import GraphState
from app.agent.node_utils import (
    busy_response,
    image_failed_response,
    maybe_analyze_image,
    state_event,
)
from app.agent.schemas import ErrorResult
from app.agent.state import AgentState
from app.config import settings
from app.services import llm

logger = logging.getLogger(__name__)

SKIP_KEYWORDS = {"跳过", "不用", "没有", "算了", "不需要", "skip"}


def entry_router(state: GraphState) -> dict:
    session = state["session"]
    user_message = state["user_message"]
    session.history.append({"role": "user", "content": user_message})
    if session.state == AgentState.GREETING:
        session.state = AgentState.COLLECTING
    return {}


async def collect_extract(state: GraphState) -> dict:
    """并行调 text_extract + analyze_image → merge → 检查澄清 → apply。"""
    session = state["session"]
    user_message = state["user_message"]
    image_url = state["image_url"]
    events: list[dict] = []

    resolved = resolve_pending_conflict(session, user_message, events)
    if resolved is not None:
        return resolved

    text_task = asyncio.create_task(
        llm.extract_text_fields(
            session.draft, user_message, session.pending_clarification, editing=False
        )
    )
    image_task = (
        asyncio.create_task(maybe_analyze_image(session, image_url)) if image_url else None
    )

    text_result = await text_task
    image_result = await image_task if image_task else None

    if isinstance(text_result, ErrorResult):
        return busy_response(session, events) | {"_extraction": {"_error": "llm_call_failed"}}
    if isinstance(image_result, ErrorResult):
        return image_failed_response(session, events) | {"_extraction": {"_error": "image_analysis_failed"}}

    session.pending_clarification = None

    if image_result is not None and not image_result.is_unclear:
        msg = image_result.visual_description
        if msg:
            session.history.append({"role": "assistant", "content": msg})
            events.append({"type": "text_delta", "content": msg})

    if image_url and image_url not in session.draft.image_urls:
        session.draft.image_urls = [image_url]

    if text_result.user_confirmed_description_priority:
        session.user_confirmed_description_priority = True

    needs_human = text_result.needs_human
    if needs_human:
        lower = user_message.lower()
        if "人工" not in lower and "客服" not in lower:
            logger.warning("LLM 误判 needs_human=true: %s", user_message)
            needs_human = False
    if needs_human:
        session.state = AgentState.ESCALATED
        events.append(
            {
                "type": "human_service",
                "session_id": session.session_id,
                "partial_ticket": session.draft.to_dict(),
            }
        )
        return {"events": events, "_extraction": {"needs_human": True}}

    if text_result.ambiguous_fields or text_result.clarification_question:
        question = text_result.clarification_question or "请问您指的是哪个位置？"
        session.pending_clarification = {
            "question": question,
            "asked_in_state": session.state.value,
        }
        session.history.append({"role": "assistant", "content": question})
        events.append({"type": "text_delta", "content": question})
        events.append(state_event(session))
        return {"events": events, "_extraction": {"clarification_question": question}}

    merged = merge_extraction(
        text_result, image_result, user_priority=session.user_confirmed_description_priority
    )
    conflicts = merged.get("_conflicts", [])
    infer_location_from_area_or_room(merged, session.draft)

    if conflicts:
        apply_extraction(session.draft, merged, None)
        visit_time_text = merged.get("visit_time_text")
        if visit_time_text:
            session.draft.visit_time = await llm.resolve_visit_time(visit_time_text, datetime.now())
        set_pending_conflict(session, conflicts, merged)
        question = session.pending_conflict["question"]
        session.history.append({"role": "assistant", "content": question})
        events.append({"type": "text_delta", "content": question})
        events.append(state_event(session))
        return {"events": events, "_extraction": {"clarification_question": question}}

    apply_extraction(session.draft, merged, None)

    visit_time_text = merged.get("visit_time_text")
    if not visit_time_text and not session.draft.visit_time:
        if any(kw in user_message for kw in llm._DEFAULT_KEYWORD):
            visit_time_text = user_message.strip()
    if visit_time_text:
        session.draft.visit_time = await llm.resolve_visit_time(visit_time_text, datetime.now())

    return {"events": events, "_extraction": merged}


def collect_decide(state: GraphState) -> dict:
    session = state["session"]
    missing = session.draft.missing_required()

    if set(missing) == set(session.last_missing):
        session.stall_count += 1
    else:
        session.stall_count = 0
        session.last_missing = missing

    if session.stall_count >= settings.max_stall_count:
        session.state = AgentState.ESCALATED
        return {
            "events": [
                {
                    "type": "human_service",
                    "session_id": session.session_id,
                    "partial_ticket": session.draft.to_dict(),
                    "reason": f"stalled_on_fields:{','.join(missing)}",
                }
            ]
        }
    return {}


async def stream_reply(state: GraphState) -> dict:
    session = state["session"]
    missing = session.draft.missing_required()
    events: list[dict] = []

    reply_text = ""
    async for chunk in llm.generate_reply_stream(session.draft, session.history, missing):
        reply_text += chunk
        events.append({"type": "text_delta", "content": chunk})

    if not reply_text.strip():
        reply_text = templates.render_missing_fields_prompt(missing)
        for chunk in templates.sentence_chunks(reply_text):
            events.append({"type": "text_delta", "content": chunk})

    session.history.append({"role": "assistant", "content": reply_text})
    events.append(state_event(session))
    return {"events": events}


def ask_image(state: GraphState) -> dict:
    session = state["session"]
    session.state = AgentState.WAITING_IMAGE
    prompt = '报修信息已经够用了。如有现场照片可以继续上传，能帮助维修人员判断；也可以回复"跳过"继续。'
    session.history.append({"role": "assistant", "content": prompt})
    return {
        "events": [
            {"type": "text_delta", "content": prompt},
            state_event(session),
        ]
    }


async def wait_image(state: GraphState) -> dict:
    session = state["session"]
    user_message = state["user_message"]
    image_url = state["image_url"]
    events: list[dict] = []
    skipped = any(kw in user_message for kw in SKIP_KEYWORDS)

    resolved = resolve_pending_conflict(session, user_message, events)
    if resolved is not None:
        resolved["_proceed_to_rag"] = bool(resolved.get("_conflict_resolved"))
        return resolved

    if image_url:
        text_task = asyncio.create_task(
            llm.extract_text_fields(
                session.draft, user_message, session.pending_clarification, editing=False
            )
        )
        image_task = asyncio.create_task(maybe_analyze_image(session, image_url))

        text_result = await text_task
        image_result = await image_task

        if isinstance(text_result, ErrorResult):
            msg = "系统繁忙，请重新发送图片试试。"
            session.history.append({"role": "assistant", "content": msg})
            events.append({"type": "text_delta", "content": msg})
            events.append(state_event(session))
            return {"events": events, "_proceed_to_rag": False}
        if isinstance(image_result, ErrorResult):
            return image_failed_response(session, events) | {"_proceed_to_rag": False}

        session.pending_clarification = None

        if image_result is not None and not image_result.is_unclear:
            msg = image_result.visual_description
            if msg:
                session.history.append({"role": "assistant", "content": msg})
                events.append({"type": "text_delta", "content": msg})

        if text_result.user_confirmed_description_priority:
            session.user_confirmed_description_priority = True

        if image_url not in session.draft.image_urls:
            session.draft.image_urls = [image_url]

        if text_result.ambiguous_fields or text_result.clarification_question:
            question = text_result.clarification_question or "请问您指的是哪个位置？"
            session.pending_clarification = {
                "question": question,
                "asked_in_state": session.state.value,
            }
            session.history.append({"role": "assistant", "content": question})
            events.append({"type": "text_delta", "content": question})
            events.append(state_event(session))
            return {"events": events, "_proceed_to_rag": False}

        merged = merge_extraction(
            text_result, image_result, user_priority=session.user_confirmed_description_priority
        )
        conflicts = merged.get("_conflicts", [])
        infer_location_from_area_or_room(merged, session.draft)
        if conflicts:
            apply_extraction(session.draft, merged, None)
            visit_time_text = merged.get("visit_time_text")
            if visit_time_text:
                session.draft.visit_time = await llm.resolve_visit_time(visit_time_text, datetime.now())
            set_pending_conflict(session, conflicts, merged)
            question = session.pending_conflict["question"]
            session.history.append({"role": "assistant", "content": question})
            events.append({"type": "text_delta", "content": question})
            events.append(state_event(session))
            return {"events": events, "_proceed_to_rag": False}
        apply_extraction(session.draft, merged, None)
        return {"events": events, "_proceed_to_rag": True}

    if not skipped:
        prompt = '现场照片是可选的。您可以继续上传照片帮助判断，也可以回复"跳过"继续。'
        session.history.append({"role": "assistant", "content": prompt})
        events.append({"type": "text_delta", "content": prompt})
        events.append(state_event(session))
        return {"events": events, "_proceed_to_rag": False}

    return {"events": events, "_proceed_to_rag": True}
