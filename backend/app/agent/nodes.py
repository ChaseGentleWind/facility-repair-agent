from __future__ import annotations

import logging
import re
from datetime import datetime

from app.agent.draft_ops import apply_extraction, clear_rag_fields, infer_location_from_area_or_room
from app.agent.graph_state import GraphState
from app.agent.state import AgentState, TicketDraft
from app.agent.ticket_builder import build_ticket
from app.config import settings
from app.services import llm, rag

logger = logging.getLogger(__name__)

_SKIP_KEYWORDS = {"跳过", "不用", "没有", "算了", "不需要", "skip"}


# ── entry_router ──────────────────────────────────────────────────────────────
def entry_router(state: GraphState) -> dict:
    session = state["session"]
    user_message = state["user_message"]
    session.history.append({"role": "user", "content": user_message})
    if session.state == AgentState.GREETING:
        session.state = AgentState.COLLECTING
    return {}


# ── collect_extract ───────────────────────────────────────────────────────────
async def collect_extract(state: GraphState) -> dict:
    session = state["session"]
    user_message = state["user_message"]
    image_url = state["image_url"]
    events = []

    extraction = await llm.extract_fields(
        session.draft, user_message, image_url, session.pending_clarification
    )

    if extraction.get("_error"):
        msg = "系统繁忙，请稍后再试一次。"
        session.history.append({"role": "assistant", "content": msg})
        events.append({"type": "text_delta", "content": msg})
        events.append({"type": "state_update", "state": session.state.value, "collected": session.draft.to_dict()})
        return {"events": events, "_extraction": extraction}

    # 清空已使用的澄清上下文
    session.pending_clarification = None

    image_description = extraction.get("image_description_text")
    if image_description:
        session.image_description = image_description
        session.history.append({"role": "assistant", "content": image_description})
        events.append({"type": "text_delta", "content": image_description})

    if image_url:
        session.draft.image_urls = [image_url]

    if extraction.get("user_confirmed_description_priority"):
        session.user_confirmed_description_priority = True

    infer_location_from_area_or_room(extraction, session.draft)
    apply_extraction(session.draft, extraction, None)

    visit_time_text = extraction.get("visit_time_text")
    if not visit_time_text and not session.draft.visit_time:
        from app.services.llm import _DEFAULT_KEYWORD
        if any(kw in user_message for kw in _DEFAULT_KEYWORD):
            visit_time_text = user_message.strip()
    if visit_time_text:
        session.draft.visit_time = await llm.resolve_visit_time(visit_time_text, datetime.now())

    needs_human = extraction.get("needs_human")
    if needs_human:
        user_msg_lower = user_message.lower()
        if "人工" not in user_msg_lower and "客服" not in user_msg_lower:
            logger.warning("LLM 误判 needs_human=true，但消息中不包含'人工'或'客服': %s", user_message)
            needs_human = False

    if needs_human:
        session.state = AgentState.ESCALATED
        events.append({
            "type": "human_service",
            "session_id": session.session_id,
            "partial_ticket": session.draft.to_dict(),
        })
        extraction["needs_human"] = True
        return {"events": events, "_extraction": extraction}

    clarification = extraction.get("clarification_question")
    if clarification:
        session.pending_clarification = {
            "question": clarification,
            "asked_in_state": session.state.value,
        }
        session.history.append({"role": "assistant", "content": clarification})
        events.append({"type": "text_delta", "content": clarification})
        events.append({"type": "state_update", "state": session.state.value, "collected": session.draft.to_dict()})
        return {"events": events, "_extraction": extraction}

    return {"events": events, "_extraction": extraction}


# ── collect_decide ────────────────────────────────────────────────────────────
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
            "events": [{
                "type": "human_service",
                "session_id": session.session_id,
                "partial_ticket": session.draft.to_dict(),
                "reason": f"stalled_on_fields:{','.join(missing)}",
            }]
        }

    return {}


# ── stream_reply ──────────────────────────────────────────────────────────────
async def stream_reply(state: GraphState) -> dict:
    session = state["session"]
    missing = session.draft.missing_required()
    events = []

    reply_text = ""
    async for chunk in llm.generate_reply_stream(session.draft, session.history, missing):
        reply_text += chunk
        events.append({"type": "text_delta", "content": chunk})

    session.history.append({"role": "assistant", "content": reply_text})
    events.append({"type": "state_update", "state": session.state.value, "collected": session.draft.to_dict()})
    return {"events": events}


# ── ask_image ─────────────────────────────────────────────────────────────────
def ask_image(state: GraphState) -> dict:
    session = state["session"]
    session.state = AgentState.WAITING_IMAGE
    prompt = '请问您能提供一张现场照片吗？（可跳过，直接回复"跳过"）'
    session.history.append({"role": "assistant", "content": prompt})
    return {
        "events": [
            {"type": "text_delta", "content": prompt},
            {"type": "state_update", "state": session.state.value, "collected": session.draft.to_dict()},
        ]
    }


# ── wait_image ────────────────────────────────────────────────────────────────
async def wait_image(state: GraphState) -> dict:
    session = state["session"]
    user_message = state["user_message"]
    image_url = state["image_url"]
    events = []
    skipped = any(kw in user_message for kw in _SKIP_KEYWORDS)

    if image_url:
        extraction = await llm.extract_fields(
            session.draft, user_message, image_url, session.pending_clarification
        )

        if extraction.get("_error"):
            msg = "系统繁忙，请重新发送图片试试。"
            session.history.append({"role": "assistant", "content": msg})
            events.append({"type": "text_delta", "content": msg})
            events.append({"type": "state_update", "state": session.state.value, "collected": session.draft.to_dict()})
            return {"events": events, "_proceed_to_rag": False}

        session.pending_clarification = None

        image_description = extraction.get("image_description_text")
        if image_description:
            session.image_description = image_description
            session.history.append({"role": "assistant", "content": image_description})
            events.append({"type": "text_delta", "content": image_description})

        if extraction.get("user_confirmed_description_priority"):
            session.user_confirmed_description_priority = True

        session.draft.image_urls = [image_url]
        infer_location_from_area_or_room(extraction, session.draft)
        apply_extraction(session.draft, extraction, None)

        clarification = extraction.get("clarification_question")
        if clarification:
            session.pending_clarification = {
                "question": clarification,
                "asked_in_state": session.state.value,
            }
            session.history.append({"role": "assistant", "content": clarification})
            events.append({"type": "text_delta", "content": clarification})
            events.append({"type": "state_update", "state": session.state.value, "collected": session.draft.to_dict()})
            return {"events": events, "_proceed_to_rag": False}

        return {"events": events, "_proceed_to_rag": True}

    elif not skipped:
        prompt = '您可以拍一张现场照片发给我，或者回复"跳过"直接提交报修。'
        session.history.append({"role": "assistant", "content": prompt})
        events.append({"type": "text_delta", "content": prompt})
        events.append({"type": "state_update", "state": session.state.value, "collected": session.draft.to_dict()})
        return {"events": events, "_proceed_to_rag": False}

    # 用户跳过
    return {"events": events, "_proceed_to_rag": True}


async def rag_and_confirm(state: GraphState) -> dict:
    session = state["session"]
    events = []
    image_url = session.draft.image_urls[0] if session.draft.image_urls else None
    ignore_image = session.user_confirmed_description_priority
    rag_result = await rag.search_fault(session.draft.description, image_url, ignore_image=ignore_image)
    if rag_result:
        session.draft.normalized_description = rag_result.normalized_description
        session.draft.fault_type_code = rag_result.fault_type_code
        session.draft.fault_type_name = rag_result.fault_type_name
        session.draft.repair_priority_rag = rag_result.repair_priority
        session.draft.repair_type = rag_result.repair_type
    visit_time = session.draft.visit_time
    session.state = AgentState.CONFIRMING
    reply_text = ""
    async for chunk in llm.generate_confirmation_stream(session.draft, visit_time):
        reply_text += chunk
        events.append({"type": "text_delta", "content": chunk})
    session.history.append({"role": "assistant", "content": reply_text})
    events.append({"type": "state_update", "state": session.state.value, "collected": session.draft.to_dict()})
    return {"events": events}


async def confirming(state: GraphState) -> dict:
    session = state["session"]
    user_message = state["user_message"]
    image_url = state["image_url"]
    events = []
    confirmed = await llm.check_user_confirmed(user_message)
    if confirmed:
        ticket = build_ticket(session)
        session.ticket = ticket
        session.state = AgentState.PREVIEW_READY
        logger.info("ticket_ready: session=%s ticket_id=%s", session.session_id, ticket.get("ticket_id"))
        events.append({"type": "ticket_ready", "ticket": ticket})
        events.append({"type": "text_delta", "content": "好的！报修单预览已生成。请点击「提交工单」按钮完成提交，或告诉我需要修改的内容。"})
        return {"events": events, "_intent": "confirmed", "_need_rerag": False}
    intent = await llm.classify_denial_intent(user_message)
    logger.info("[CONFIRMING] denial intent: %s, message: %s", intent, user_message)
    if intent == "restart":
        session.draft = TicketDraft()
        session.state = AgentState.COLLECTING
        session.stall_count = 0
        session.last_missing = []
        session.pending_clarification = None
        msg = "好的，我们重新开始。请描述您遇到的问题。"
        session.history.append({"role": "assistant", "content": msg})
        events.append({"type": "text_delta", "content": msg})
        events.append({"type": "state_update", "state": session.state.value, "collected": session.draft.to_dict()})
        return {"events": events, "_intent": "restart", "_need_rerag": False}
    elif intent == "modify":
        extraction = await llm.extract_fields_editing(
            session.draft, user_message, image_url, session.pending_clarification
        )
        if extraction.get("_error"):
            msg = "系统繁忙，请稍后再试一次。"
            session.history.append({"role": "assistant", "content": msg})
            events.append({"type": "text_delta", "content": msg})
            events.append({"type": "state_update", "state": session.state.value, "collected": session.draft.to_dict()})
            return {"events": events, "_intent": "modify", "_need_rerag": False}

        session.pending_clarification = None

        image_description = extraction.get("image_description_text")
        if image_description:
            session.image_description = image_description
            session.history.append({"role": "assistant", "content": image_description})
            events.append({"type": "text_delta", "content": image_description})
        if extraction.get("user_confirmed_description_priority"):
            session.user_confirmed_description_priority = True
        if image_url:
            session.draft.image_urls = [image_url]
            session.user_confirmed_description_priority = False

        description_changed = bool(extraction.get("description"))
        image_changed = image_url is not None

        # 图文冲突时先询问，不立即修改
        if (description_changed and not image_changed and session.draft.image_urls
                and not session.user_confirmed_description_priority):
            old_image = session.draft.image_urls[0]
            re_extraction = await llm.extract_fields_editing(session.draft, user_message, old_image)
            clarification = re_extraction.get("clarification_question")
            if clarification:
                session.pending_clarification = {
                    "question": clarification,
                    "asked_in_state": session.state.value,
                }
                session.history.append({"role": "assistant", "content": clarification})
                events.append({"type": "text_delta", "content": clarification})
                events.append({"type": "state_update", "state": session.state.value, "collected": session.draft.to_dict()})
                return {"events": events, "_intent": "modify", "_need_rerag": False}

        infer_location_from_area_or_room(extraction, session.draft)
        apply_extraction(session.draft, extraction, None)
        visit_time_text = extraction.get("visit_time_text")
        if visit_time_text:
            session.draft.visit_time = await llm.resolve_visit_time(visit_time_text, datetime.now())

        if description_changed or image_changed:
            clear_rag_fields(session.draft)
            return {"events": events, "_intent": "modify", "_need_rerag": True}

        # 非 description/image 字段变更（如时间/楼层），跳过 RAG 直接重生成确认摘要
        return {"events": events, "_intent": "modify", "_need_rerag": False}
    else:
        msg = "请问您需要修改哪一项？比如位置、时间或问题描述。"
        session.history.append({"role": "assistant", "content": msg})
        events.append({"type": "text_delta", "content": msg})
        events.append({"type": "state_update", "state": session.state.value, "collected": session.draft.to_dict()})
        return {"events": events, "_intent": "unclear", "_need_rerag": False}


async def preview_edit(state: GraphState) -> dict:
    session = state["session"]
    user_message = state["user_message"]
    image_url = state["image_url"]
    events = []
    extraction = await llm.extract_fields_editing(
        session.draft, user_message, image_url, session.pending_clarification
    )
    logger.info("[PREVIEW_READY] LLM extraction result: %s", extraction)
    if extraction.get("_error"):
        msg = "系统繁忙，请稍后再试一次。"
        session.history.append({"role": "assistant", "content": msg})
        events.append({"type": "text_delta", "content": msg})
        events.append({"type": "state_update", "state": session.state.value, "collected": session.draft.to_dict()})
        return {"events": events, "_need_rerag": False}
    image_description = extraction.get("image_description_text")
    if image_description:
        session.image_description = image_description
        session.history.append({"role": "assistant", "content": image_description})
        events.append({"type": "text_delta", "content": image_description})
    if extraction.get("user_confirmed_description_priority"):
        session.user_confirmed_description_priority = True

    session.pending_clarification = None

    description_changed = bool(extraction.get("description"))
    image_changed = image_url is not None
    new_area = extraction.get("area")
    area_changed = new_area is not None and new_area != session.draft.area
    new_room = extraction.get("room")
    room_changed = new_room is not None and new_room != session.draft.room
    new_floor = extraction.get("floor")
    floor_changed = new_floor is not None and new_floor != session.draft.floor
    new_building = extraction.get("building")
    building_changed = new_building is not None and new_building != session.draft.building
    logger.info("[PREVIEW_READY] area_changed=%s room_changed=%s floor_changed=%s building_changed=%s",
                area_changed, room_changed, floor_changed, building_changed)
    if image_changed:
        session.draft.image_urls = [image_url]
        session.user_confirmed_description_priority = False
    if area_changed:
        if not building_changed:
            session.draft.building = None
            extraction["building"] = None
        if not floor_changed:
            session.draft.floor = None
            extraction["floor"] = None
    if room_changed:
        room_clean = re.sub(r"(房间|会议室|办公室|卫生间|茶水间|储藏室|仓库|机房|配电室|停车场)$", "", new_room or "")
        llm_may_infer_floor = bool(re.match(r"^\d{3,4}$", room_clean))
        if not floor_changed or llm_may_infer_floor:
            session.draft.floor = None
            extraction["floor"] = None
    infer_location_from_area_or_room(extraction, session.draft)
    apply_extraction(session.draft, extraction, None)
    visit_time_text = extraction.get("visit_time_text")
    if visit_time_text:
        session.draft.visit_time = await llm.resolve_visit_time(visit_time_text, datetime.now())
    if (description_changed and not image_changed and session.draft.image_urls
            and not session.user_confirmed_description_priority):
        old_image = session.draft.image_urls[0]
        re_extraction = await llm.extract_fields_editing(session.draft, user_message, old_image)
        clarification = re_extraction.get("clarification_question")
        if clarification:
            session.pending_clarification = {
                "question": clarification,
                "asked_in_state": session.state.value,
            }
            session.history.append({"role": "assistant", "content": clarification})
            events.append({"type": "text_delta", "content": clarification})
            events.append({"type": "state_update", "state": session.state.value, "collected": session.draft.to_dict()})
            return {"events": events, "_need_rerag": False}
    if description_changed or image_changed:
        clear_rag_fields(session.draft)
        return {"events": events, "_need_rerag": True}
    ticket = build_ticket(session)
    session.ticket = ticket
    events.append({"type": "ticket_ready", "ticket": ticket})
    events.append({"type": "text_delta", "content": "已更新预览，请确认或继续修改。"})
    events.append({"type": "state_update", "state": session.state.value, "collected": session.draft.to_dict()})
    return {"events": events, "_need_rerag": False}


def escalated(state: GraphState) -> dict:
    session = state["session"]
    if session.state != AgentState.ESCALATED:
        session.state = AgentState.ESCALATED
    return {"events": [{"type": "text_delta", "content": "已为您转接人工客服，如有其他问题请刷新页面重新发起。"}]}


def submitted(state: GraphState) -> dict:
    return {"events": [{"type": "text_delta", "content": "工单已提交，如有其他问题请刷新页面重新发起。"}]}


def completed(state: GraphState) -> dict:
    return {"events": [{"type": "text_delta", "content": "报修单已提交，如有其他问题请刷新页面重新发起。"}]}


def finalize(state: GraphState) -> dict:
    return {}


async def re_confirm(state: GraphState) -> dict:
    """modify 分支中非 description/image 字段变更后，跳过 RAG 直接重生成确认摘要。"""
    session = state["session"]
    events = []
    session.state = AgentState.CONFIRMING
    visit_time = session.draft.visit_time
    reply_text = ""
    async for chunk in llm.generate_confirmation_stream(session.draft, visit_time):
        reply_text += chunk
        events.append({"type": "text_delta", "content": chunk})
    session.history.append({"role": "assistant", "content": reply_text})
    events.append({"type": "state_update", "state": session.state.value, "collected": session.draft.to_dict()})
    return {"events": events}
