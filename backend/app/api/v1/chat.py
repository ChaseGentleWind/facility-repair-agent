from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from app.agent import graph as agent_graph
from app.agent.prompts import GREETING_TEXT
from app.services.session_store import create_session, get_session, refresh_session, get_session_store
from app.config import settings
from app.models.api_models import InitRequest, InitResponse, MessageRequest

router = APIRouter(prefix="/chat", tags=["chat"])
logger = logging.getLogger(__name__)


@router.post("/init", response_model=InitResponse)
async def chat_init(req: InitRequest) -> InitResponse:
    session = await create_session(client_id=req.client_id)
    session.history.append({"role": "assistant", "content": GREETING_TEXT})
    store = get_session_store()
    await store.save(session)
    return InitResponse(
        session_id=session.session_id,
        greeting=GREETING_TEXT,
        expires_in=settings.session_ttl_seconds,
    )


@router.post("/message")
async def chat_message(req: MessageRequest):
    store = get_session_store()
    session = await get_session(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在或已过期，请重新初始化")

    image_url = req.message.image_url if req.message.type == "image_url" else None
    user_text = req.message.content

    async def event_generator():
        # 尝试获取分布式锁（非阻塞），如果已被占用则拒绝请求
        async with store.acquire_lock(req.session_id, ttl_s=60) as lock:
            if lock is None:
                logger.warning("Session %s is busy, rejecting concurrent request", req.session_id)
                yield {
                    "data": json.dumps(
                        {"type": "error", "code": "BUSY", "message": "正在处理中，请稍后再试"},
                        ensure_ascii=False,
                    )
                }
                return

            try:
                await store.refresh_ttl(req.session_id)
                # 持锁期间独占 session，串行处理消息
                async for event in agent_graph.process_message(session, user_text, image_url):
                    yield {"data": json.dumps(event, ensure_ascii=False)}
                # 写回 session 状态
                await store.save(session)
            except Exception as exc:
                logger.exception("SSE generator error: %s", exc)
                yield {
                    "data": json.dumps(
                        {"type": "error", "code": "STREAM_ERROR", "message": str(exc)},
                        ensure_ascii=False,
                    )
                }

    return EventSourceResponse(event_generator())
