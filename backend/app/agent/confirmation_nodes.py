from __future__ import annotations

import logging
import re
from datetime import datetime

from app.agent import templates
from app.agent.conflict_nodes import resolve_pending_conflict, set_pending_conflict
from app.agent.draft_ops import (
    apply_extraction,
    clear_rag_fields,
    infer_location_from_area_or_room,
    merge_extraction,
)
from app.agent.graph_state import GraphState
from app.agent.node_utils import (
    busy_response,
    draft_confirm_event,
    emit_template,
    image_affects_fault,
    image_failed_response,
    maybe_analyze_image,
    state_event,
)
from app.agent.schemas import ConfirmationIntent, ErrorResult, ImageAnalysis, TextExtraction
from app.agent.state import AgentState, TicketDraft
from app.agent.ticket_builder import build_ticket
from app.services import llm, rag

logger = logging.getLogger(__name__)


async def rag_and_confirm(state: GraphState) -> dict:
    """RAG 检索 + 模板化确认摘要。"""
    session = state["session"]
    events: list[dict] = []

    visual_summary = (
        session.image_analysis.visual_fault_summary
        if session.image_analysis and not session.image_analysis.is_unclear
        else None
    )
    ignore_image = session.user_confirmed_description_priority
    rag_result = await rag.search_fault(
        session.draft.description,
        visual_fault_summary=visual_summary,
        ignore_image=ignore_image,
    )
    if rag_result:
        session.draft.normalized_description = rag_result.normalized_description
        session.draft.fault_type_code = rag_result.fault_type_code
        session.draft.fault_type_name = rag_result.fault_type_name
        session.draft.repair_priority_rag = rag_result.repair_priority
        session.draft.repair_type = rag_result.repair_type

    session.state = AgentState.CONFIRMING
    session.history.append({"role": "assistant", "content": "已展示报修信息确认卡片。"})
    events.append(draft_confirm_event(session))
    events.append(state_event(session))
    return {"events": events}


async def confirming(state: GraphState) -> dict:
    """确认摘要阶段处理确认、重开、修改和图文冲突。"""
    session = state["session"]
    user_message = state["user_message"]
    image_url = state["image_url"]
    events: list[dict] = []

    resolved = resolve_pending_conflict(session, user_message, events)
    if resolved is not None:
        return resolved | {
            "_intent": "modify" if resolved.get("_conflict_resolved") else "unclear",
            "_need_rerag": bool(resolved.get("_need_rerag")),
        }

    intent_result = await llm.classify_confirmation_intent(session.draft, user_message)
    if isinstance(intent_result, ErrorResult):
        return busy_response(session, events) | {"_intent": "unclear", "_need_rerag": False}

    intent = intent_result.intent
    if image_url and intent == "unclear":
        intent = "modify"
        intent_result = ConfirmationIntent(intent="modify", modified_fields=TextExtraction())
    logger.info("[CONFIRMING] intent=%s, message=%s", intent, user_message)

    if intent == "confirm":
        ticket = await build_ticket(session)
        session.ticket = ticket
        session.state = AgentState.PREVIEW_READY
        logger.info("ticket_ready: session=%s ticket_id=%s", session.session_id, ticket.get("ticket_id"))
        events.append({"type": "ticket_ready", "ticket": ticket})
        events.append(
            {"type": "text_delta", "content": "好的！报修单预览已生成。请点击「提交工单」按钮完成提交，或告诉我需要修改的内容。"}
        )
        events.append(state_event(session))
        return {"events": events, "_intent": "confirmed", "_need_rerag": False}

    if intent == "restart":
        _reset_to_collecting(session)
        msg = "好的，我们重新开始。请描述您遇到的问题。"
        session.history.append({"role": "assistant", "content": msg})
        events.append({"type": "text_delta", "content": msg})
        events.append(state_event(session))
        return {"events": events, "_intent": "restart", "_need_rerag": False}

    if intent == "modify":
        return await _apply_confirmation_modify(session, intent_result, image_url, events)

    msg = "请问您需要修改哪一项？比如位置、时间或问题描述。"
    session.history.append({"role": "assistant", "content": msg})
    events.append({"type": "text_delta", "content": msg})
    events.append(state_event(session))
    return {"events": events, "_intent": "unclear", "_need_rerag": False}


async def preview_edit(state: GraphState) -> dict:
    """PREVIEW_READY 阶段处理用户修改：用 classify_confirmation_intent 一次拿意图+字段。"""
    session = state["session"]
    user_message = state["user_message"]
    image_url = state["image_url"]
    events: list[dict] = []

    resolved = resolve_pending_conflict(session, user_message, events)
    if resolved is not None:
        return resolved | {"_need_rerag": bool(resolved.get("_need_rerag"))}

    intent_result = await llm.classify_confirmation_intent(session.draft, user_message)
    if isinstance(intent_result, ErrorResult):
        return busy_response(session, events) | {"_need_rerag": False}

    intent = intent_result.intent
    if image_url and intent == "unclear":
        intent = "modify"
        intent_result = ConfirmationIntent(intent="modify", modified_fields=TextExtraction())
    logger.info("[PREVIEW_READY] intent=%s, message=%s", intent, user_message)

    if intent == "confirm":
        msg = "请点击「提交工单」按钮完成提交，或继续告诉我需要修改的内容。"
        session.history.append({"role": "assistant", "content": msg})
        events.append({"type": "text_delta", "content": msg})
        events.append(state_event(session))
        return {"events": events, "_need_rerag": False}

    if intent == "restart":
        _reset_to_collecting(session)
        msg = "好的，我们重新开始。请描述您遇到的问题。"
        session.history.append({"role": "assistant", "content": msg})
        events.append({"type": "text_delta", "content": msg})
        events.append(state_event(session))
        return {"events": events, "_need_rerag": False}

    if intent != "modify":
        msg = "请问您需要修改哪一项？比如位置、时间或问题描述。"
        session.history.append({"role": "assistant", "content": msg})
        events.append({"type": "text_delta", "content": msg})
        events.append(state_event(session))
        return {"events": events, "_need_rerag": False}

    modified = intent_result.modified_fields or TextExtraction()
    image_result, image_for_merge, early_response = await _prepare_edit_image(
        session, image_url, events, with_intent=False
    )
    if early_response is not None:
        return early_response

    if modified.user_confirmed_description_priority:
        session.user_confirmed_description_priority = True

    if modified.ambiguous_fields or modified.clarification_question:
        question = modified.clarification_question or "请问您指的是哪一项？"
        session.pending_clarification = {
            "question": question,
            "asked_in_state": session.state.value,
        }
        session.history.append({"role": "assistant", "content": question})
        events.append({"type": "text_delta", "content": question})
        events.append(state_event(session))
        return {"events": events, "_need_rerag": False}

    description_changed = bool(modified.description)
    image_changed = image_url is not None and image_affects_fault(image_result)

    _clear_dependent_location_fields(session, modified)

    merged = merge_extraction(
        modified, image_for_merge, user_priority=session.user_confirmed_description_priority
    )
    conflicts = merged.get("_conflicts", [])
    infer_location_from_area_or_room(merged, session.draft)
    if conflicts:
        apply_extraction(session.draft, merged, None)
        set_pending_conflict(session, conflicts, merged)
        question = session.pending_conflict["question"]
        session.history.append({"role": "assistant", "content": question})
        events.append({"type": "text_delta", "content": question})
        events.append(state_event(session))
        return {"events": events, "_need_rerag": False}
    apply_extraction(session.draft, merged, None)

    visit_time_text = merged.get("visit_time_text")
    if visit_time_text:
        session.draft.visit_time = await llm.resolve_visit_time(visit_time_text, datetime.now())

    if description_changed or image_changed:
        clear_rag_fields(session.draft)
        return {"events": events, "_need_rerag": True}

    ticket = await build_ticket(session)
    session.ticket = ticket
    text = templates.render_preview_updated(session.draft)
    emit_template(session, text, events)
    events.append({"type": "ticket_ready", "ticket": ticket})
    events.append(state_event(session))
    return {"events": events, "_need_rerag": False}


async def re_confirm(state: GraphState) -> dict:
    """非 description/image 字段变更后，重新生成确认摘要（不走 RAG）。"""
    session = state["session"]
    events: list[dict] = []
    session.state = AgentState.CONFIRMING
    session.history.append({"role": "assistant", "content": "已更新报修信息确认卡片。"})
    events.append(draft_confirm_event(session))
    events.append(state_event(session))
    return {"events": events}


async def _apply_confirmation_modify(session, intent_result, image_url: str | None, events: list[dict]) -> dict:
    modified = intent_result.modified_fields or TextExtraction()

    image_result, image_for_merge, early_response = await _prepare_edit_image(
        session, image_url, events, with_intent=True
    )
    if early_response is not None:
        return early_response

    if modified.user_confirmed_description_priority:
        session.user_confirmed_description_priority = True

    if modified.ambiguous_fields or modified.clarification_question:
        question = modified.clarification_question or "请问您指的是哪一项？"
        session.pending_clarification = {
            "question": question,
            "asked_in_state": session.state.value,
        }
        session.history.append({"role": "assistant", "content": question})
        events.append({"type": "text_delta", "content": question})
        events.append(state_event(session))
        return {"events": events, "_intent": "modify", "_need_rerag": False}

    description_changed = bool(modified.description)
    image_changed = image_url is not None and image_affects_fault(image_result)

    merged = merge_extraction(
        modified, image_for_merge, user_priority=session.user_confirmed_description_priority
    )
    conflicts = merged.get("_conflicts", [])
    infer_location_from_area_or_room(merged, session.draft)
    if conflicts:
        apply_extraction(session.draft, merged, None)
        set_pending_conflict(session, conflicts, merged)
        question = session.pending_conflict["question"]
        session.history.append({"role": "assistant", "content": question})
        events.append({"type": "text_delta", "content": question})
        events.append(state_event(session))
        return {"events": events, "_intent": "modify", "_need_rerag": False}
    apply_extraction(session.draft, merged, None)

    visit_time_text = merged.get("visit_time_text")
    if visit_time_text:
        session.draft.visit_time = await llm.resolve_visit_time(visit_time_text, datetime.now())

    if description_changed or image_changed:
        clear_rag_fields(session.draft)
        return {"events": events, "_intent": "modify", "_need_rerag": True}
    return {"events": events, "_intent": "modify", "_need_rerag": False}


async def _prepare_edit_image(
    session,
    image_url: str | None,
    events: list[dict],
    *,
    with_intent: bool,
) -> tuple[ImageAnalysis | None, ImageAnalysis | None, dict | None]:
    if not image_url:
        return None, None, None

    image_result_raw = await maybe_analyze_image(session, image_url)
    if isinstance(image_result_raw, ErrorResult):
        response = image_failed_response(session, events)
        if with_intent:
            response |= {"_intent": "modify", "_need_rerag": False}
        else:
            response |= {"_need_rerag": False}
        return None, None, response

    image_result = image_result_raw
    session.user_confirmed_description_priority = False
    if image_url not in session.draft.image_urls:
        session.draft.image_urls = [image_url]
    if image_result is not None and not image_result.is_unclear:
        msg = image_result.visual_description
        if msg:
            session.history.append({"role": "assistant", "content": msg})
            events.append({"type": "text_delta", "content": msg})
    return image_result, image_result, None


def _clear_dependent_location_fields(session, modified: TextExtraction) -> None:
    new_area = modified.area
    new_room = modified.room
    new_floor = modified.floor
    new_building = modified.building
    area_changed = new_area is not None and new_area != session.draft.area
    room_changed = new_room is not None and new_room != session.draft.room
    floor_changed = new_floor is not None and new_floor != session.draft.floor
    building_changed = new_building is not None and new_building != session.draft.building

    if area_changed:
        if not building_changed:
            session.draft.building = None
        if not floor_changed:
            session.draft.floor = None
    if room_changed:
        room_clean = re.sub(
            r"(房间|会议室|办公室|卫生间|茶水间|储藏室|仓库|机房|配电室|停车场)$",
            "",
            new_room or "",
        )
        if not floor_changed or re.match(r"^\d{3,4}$", room_clean):
            session.draft.floor = None


def _reset_to_collecting(session) -> None:
    session.draft = TicketDraft()
    session.state = AgentState.COLLECTING
    session.stall_count = 0
    session.last_missing = []
    session.pending_clarification = None
    session.pending_conflict = None
    session.image_analysis = None
