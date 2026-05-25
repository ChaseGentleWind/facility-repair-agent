from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import AsyncIterator

from app.agent.state import AgentState, Session, TicketDraft
from app.agent.ticket_builder import build_ticket
from app.config import settings
from app.services import llm
from app.services import rag

logger = logging.getLogger(__name__)

_SKIP_KEYWORDS = {"跳过", "不用", "没有", "算了", "不需要", "skip"}


async def process_message(
    session: Session,
    user_message: str,
    image_url: str | None = None,
) -> AsyncIterator[dict]:
    session.history.append({"role": "user", "content": user_message})

    if session.state == AgentState.GREETING:
        session.state = AgentState.COLLECTING

    try:
        if session.state == AgentState.COLLECTING:
            async for event in _handle_collecting(session, user_message, image_url):
                yield event
        elif session.state == AgentState.WAITING_IMAGE:
            async for event in _handle_waiting_image(session, user_message, image_url):
                yield event
        elif session.state == AgentState.CONFIRMING:
            async for event in _handle_confirming(session, user_message):
                yield event
        elif session.state in (AgentState.COMPLETED, AgentState.ESCALATED):
            yield {"type": "text_delta", "content": "您的报修单已提交，如有其他问题请刷新页面重新发起。"}
        else:
            yield {"type": "error", "code": "INVALID_STATE", "message": "无效的会话状态"}
    except Exception as exc:
        logger.exception("process_message error: %s", exc)
        yield {"type": "error", "code": "INTERNAL_ERROR", "message": "服务异常，请稍后重试"}

    yield {"type": "done"}


# ── COLLECTING 阶段 ──────────────────────────────────────────────────────────

async def _handle_collecting(
    session: Session,
    user_message: str,
    image_url: str | None,
) -> AsyncIterator[dict]:
    extraction = await llm.extract_fields(session.draft, user_message, image_url)
    _apply_extraction(session.draft, extraction, image_url)

    # 若本轮提取到 visit_time_text 且 draft 中尚未解析，立即解析为绝对时间
    visit_time_text = extraction.get("visit_time_text")
    # 兜底：LLM 未提取但消息本身就是已知紧急时间词（如"越快越好"、"尽快"）
    if not visit_time_text and not session.draft.visit_time:
        from app.services.llm import _DEFAULT_KEYWORD
        if any(kw in user_message for kw in _DEFAULT_KEYWORD):
            visit_time_text = user_message.strip()
    if visit_time_text:
        session.draft.visit_time = await llm.resolve_visit_time(visit_time_text, datetime.now())

    if extraction.get("needs_human"):
        session.state = AgentState.ESCALATED
        yield {
            "type": "human_service",
            "session_id": session.session_id,
            "partial_ticket": session.draft.to_dict(),
        }
        return

    clarification = extraction.get("clarification_question")
    if clarification:
        session.history.append({"role": "assistant", "content": clarification})
        yield {"type": "text_delta", "content": clarification}
        yield {
            "type": "state_update",
            "state": session.state.value,
            "collected": session.draft.to_dict(),
        }
        return

    missing = session.draft.missing_required()

    if not missing:
        if not session.draft.image_urls:
            # 必填项齐全但无图片 → 追问图片
            session.state = AgentState.WAITING_IMAGE
            prompt = '请问您能提供一张现场照片吗？（可跳过，直接回复"跳过"）'
            session.history.append({"role": "assistant", "content": prompt})
            yield {"type": "text_delta", "content": prompt}
            yield {
                "type": "state_update",
                "state": session.state.value,
                "collected": session.draft.to_dict(),
            }
        else:
            async for event in _run_rag_and_confirm(session):
                yield event
    else:
        session.retry_count += 1
        if session.retry_count >= settings.max_retry_count and not session.draft.description:
            session.state = AgentState.ESCALATED
            yield {
                "type": "human_service",
                "session_id": session.session_id,
                "partial_ticket": session.draft.to_dict(),
                "reason": "max_retries",
            }
            return

        reply_text = ""
        async for chunk in llm.generate_reply_stream(session.draft, session.history, missing):
            reply_text += chunk
            yield {"type": "text_delta", "content": chunk}
        session.history.append({"role": "assistant", "content": reply_text})
        yield {
            "type": "state_update",
            "state": session.state.value,
            "collected": session.draft.to_dict(),
        }


# ── WAITING_IMAGE 阶段 ───────────────────────────────────────────────────────

async def _handle_waiting_image(
    session: Session,
    user_message: str,
    image_url: str | None,
) -> AsyncIterator[dict]:
    skipped = any(kw in user_message for kw in _SKIP_KEYWORDS)

    if image_url:
        _apply_extraction(session.draft, {}, image_url)
    elif not skipped:
        # 用户既没上传图片也没跳过，继续等待
        prompt = '您可以拍一张现场照片发给我，或者回复"跳过"直接提交报修。'
        session.history.append({"role": "assistant", "content": prompt})
        yield {"type": "text_delta", "content": prompt}
        yield {
            "type": "state_update",
            "state": session.state.value,
            "collected": session.draft.to_dict(),
        }
        return

    # 收到图片或用户跳过 → 进入 RAG + 确认
    async for event in _run_rag_and_confirm(session):
        yield event


# ── RAG 检索 + 生成确认摘要 ──────────────────────────────────────────────────

async def _run_rag_and_confirm(session: Session) -> AsyncIterator[dict]:
    rag_result = await rag.search_fault(session.draft.description)
    if rag_result:
        session.draft.normalized_description = rag_result.normalized_description
        session.draft.fault_type_code = rag_result.fault_type_code
        session.draft.fault_type_name = rag_result.fault_type_name
        session.draft.repair_priority_rag = rag_result.repair_priority
        session.draft.repair_type = rag_result.repair_type

    # 若全程未提及上门时间，使用默认值 now+30min
    if not session.draft.visit_time:
        now = datetime.now()
        default = now + timedelta(minutes=30)
        session.draft.visit_time = f"{default.month}月{default.day}日 {default.hour}时{default.minute:02d}分"

    visit_time = session.draft.visit_time

    session.state = AgentState.CONFIRMING
    reply_text = ""
    async for chunk in llm.generate_confirmation_stream(session.draft, session.history, visit_time):
        reply_text += chunk
        yield {"type": "text_delta", "content": chunk}
    session.history.append({"role": "assistant", "content": reply_text})
    yield {
        "type": "state_update",
        "state": session.state.value,
        "collected": session.draft.to_dict(),
    }


# ── CONFIRMING 阶段 ──────────────────────────────────────────────────────────

async def _handle_confirming(
    session: Session,
    user_message: str,
) -> AsyncIterator[dict]:
    confirmed = await llm.check_user_confirmed(user_message)

    if confirmed:
        ticket = build_ticket(session)
        session.state = AgentState.COMPLETED
        logger.info("ticket_ready: session=%s ticket_id=%s", session.session_id, ticket.get("ticket_id"))
        yield {"type": "ticket_ready", "ticket": ticket}
        yield {"type": "text_delta", "content": "好的，您的报修单已提交！我们会尽快安排人员上门处理。"}
    else:
        session.state = AgentState.COLLECTING
        extraction = await llm.extract_fields(session.draft, user_message)
        _apply_extraction(session.draft, extraction, None)
        visit_time_text = extraction.get("visit_time_text")
        if visit_time_text:
            session.draft.visit_time = await llm.resolve_visit_time(visit_time_text, datetime.now())

        missing = session.draft.missing_required()
        if not missing:
            async for event in _run_rag_and_confirm(session):
                yield event
        else:
            reply_text = ""
            async for chunk in llm.generate_reply_stream(session.draft, session.history, missing):
                reply_text += chunk
                yield {"type": "text_delta", "content": chunk}
            session.history.append({"role": "assistant", "content": reply_text})
            yield {
                "type": "state_update",
                "state": session.state.value,
                "collected": session.draft.to_dict(),
            }


# ── 工具函数 ─────────────────────────────────────────────────────────────────

def _apply_extraction(draft: TicketDraft, extraction: dict, image_url: str | None) -> None:
    if extraction.get("description"):
        draft.description = extraction["description"]
    if extraction.get("estate"):
        draft.estate = extraction["estate"]
    if extraction.get("building"):
        draft.building = extraction["building"]
    if extraction.get("floor"):
        draft.floor = extraction["floor"]
    if extraction.get("unit"):
        draft.unit = extraction["unit"]
    if extraction.get("room"):
        draft.room = extraction["room"]
    if image_url and image_url not in draft.image_urls:
        draft.image_urls.append(image_url)
