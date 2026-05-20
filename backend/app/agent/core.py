from __future__ import annotations

import json
import logging
from typing import AsyncIterator

from app.agent.state import AgentState, Session, TicketDraft
from app.agent.ticket_builder import build_ticket
from app.config import settings
from app.services import llm

logger = logging.getLogger(__name__)


async def process_message(
    session: Session,
    user_message: str,
    image_url: str | None = None,
) -> AsyncIterator[dict]:
    """
    主入口：接受用户消息，yield SSE 事件 dict。
    调用方负责将 dict 序列化为 SSE 格式写入响应流。
    """
    session.history.append({"role": "user", "content": user_message})

    if session.state == AgentState.GREETING:
        session.state = AgentState.COLLECTING

    try:
        if session.state == AgentState.COLLECTING:
            async for event in _handle_collecting(session, user_message, image_url):
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
    # Step 1: 提取字段（非流式，JSON）
    extraction = await llm.extract_fields(session.draft, user_message, image_url)
    _apply_extraction(session.draft, extraction, image_url)

    # 用户要求转人工
    if extraction.get("needs_human"):
        session.state = AgentState.ESCALATED
        yield {
            "type": "human_service",
            "session_id": session.session_id,
            "partial_ticket": session.draft.to_dict(),
        }
        return

    missing = session.draft.missing_required()

    # Step 2: 生成回复（流式）
    if not missing:
        # 必填项齐全 → 生成确认摘要，切换到 CONFIRMING
        session.state = AgentState.CONFIRMING
        reply_text = ""
        async for chunk in llm.generate_confirmation_stream(session.draft, session.history):
            reply_text += chunk
            yield {"type": "text_delta", "content": chunk}
    else:
        # 还有缺失项 → 生成追问
        session.retry_count += 1
        if session.retry_count >= settings.max_retry_count and not session.draft.description:
            # 多次未能获取任何有效信息，主动降级转人工
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


# ── CONFIRMING 阶段 ──────────────────────────────────────────────────────────

async def _handle_confirming(
    session: Session,
    user_message: str,
) -> AsyncIterator[dict]:
    confirmed = await llm.check_user_confirmed(user_message)

    if confirmed:
        ticket = build_ticket(session)
        session.state = AgentState.COMPLETED
        yield {"type": "ticket_ready", "ticket": ticket}
    else:
        # 用户要修改，回到 COLLECTING
        session.state = AgentState.COLLECTING
        extraction = await llm.extract_fields(session.draft, user_message)
        _apply_extraction(session.draft, extraction, None)

        missing = session.draft.missing_required()
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
    if extraction.get("building"):
        draft.building = extraction["building"]
    if extraction.get("floor"):
        draft.floor = extraction["floor"]
    if extraction.get("room"):
        draft.room = extraction["room"]
    if image_url and image_url not in draft.image_urls:
        draft.image_urls.append(image_url)
