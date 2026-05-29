from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from app.agent.state import AgentState, get_session
from app.models.api_models import SubmitTicketRequest, SubmitTicketResponse

router = APIRouter(prefix="/ticket", tags=["ticket"])
logger = logging.getLogger(__name__)


@router.post("/submit", response_model=SubmitTicketResponse)
async def submit_ticket(req: SubmitTicketRequest) -> SubmitTicketResponse:
    session = get_session(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")

    if session.state != AgentState.PREVIEW_READY:
        raise HTTPException(status_code=400, detail="工单未就绪，无法提交")

    if session.ticket is None:
        raise HTTPException(status_code=400, detail="工单数据缺失，请重新确认")

    if req.ticket:
        for key in ("location", "problem_description", "visit_time",
                    "repair_type", "fault_type", "image_urls"):
            if key in req.ticket:
                session.ticket[key] = req.ticket[key]

    session.ticket["order_status"] = "PENDING"

    # TODO: 调用实际的工单系统 API
    logger.info("工单已提交: session=%s ticket_id=%s", session.session_id, session.ticket["ticket_id"])

    session.state = AgentState.SUBMITTED

    return SubmitTicketResponse(
        success=True,
        ticket_id=session.ticket["ticket_id"],
        message="工单已成功提交"
    )
