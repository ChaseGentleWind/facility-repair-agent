from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from sse_starlette.sse import EventSourceResponse

from app.agent import core as agent_core
from app.agent.prompts import GREETING_TEXT
from app.agent.state import create_session, get_session, refresh_session
from app.config import settings
from app.models.api_models import InitRequest, InitResponse, MessageRequest

router = APIRouter(prefix="/chat", tags=["chat"])
logger = logging.getLogger(__name__)


@router.post("/init", response_model=InitResponse)
async def chat_init(req: InitRequest) -> InitResponse:
    source = req.metadata.get("source", "web")
    session = create_session(client_id=req.client_id, source=source)
    # 欢迎语写入历史，方便后续 LLM 了解上下文
    session.history.append({"role": "assistant", "content": GREETING_TEXT})
    return InitResponse(
        session_id=session.session_id,
        greeting=GREETING_TEXT,
        expires_in=settings.session_ttl_seconds,
    )


@router.post("/message")
async def chat_message(req: MessageRequest):
    session = get_session(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在或已过期，请重新初始化")

    refresh_session(session)
    image_url = req.message.image_url if req.message.type == "image_url" else None
    user_text = req.message.content

    async def event_generator():
        try:
            async for event in agent_core.process_message(session, user_text, image_url):
                yield {"data": json.dumps(event, ensure_ascii=False)}
        except Exception as exc:
            logger.exception("SSE generator error: %s", exc)
            yield {
                "data": json.dumps(
                    {"type": "error", "code": "STREAM_ERROR", "message": str(exc)},
                    ensure_ascii=False,
                )
            }

    return EventSourceResponse(event_generator())
