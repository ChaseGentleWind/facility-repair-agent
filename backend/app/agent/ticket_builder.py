from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.agent.state import Session


def build_ticket(session: Session) -> dict:
    draft = session.draft
    ticket_id = f"TKT-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"

    location: dict[str, str | None] = {
        "building": draft.building,
        "floor": draft.floor,
        "room": draft.room,
    }

    ft_code = draft.fault_type_code or "000"
    ft_name = draft.fault_type_name or "待分类"
    priority = draft.repair_priority_rag or "MEDIUM"
    confidence = draft.confidence or "low"
    match_score = draft.rag_match_score

    return {
        "ticket_id": ticket_id,
        "session_id": session.session_id,
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "reporter": {
            "name": None,
            "phone": None,
        },
        "location": location,
        "problem": {
            "description": draft.description,
            "normalized_description": draft.normalized_description,
        },
        "fault_type": {
            "code": ft_code,
            "displayName": ft_name,
        },
        "repair_priority": priority,
        "repair_type": draft.repair_type or "公司报修",
        "image_urls": draft.image_urls,
        "confidence": confidence,
        "rag_match_score": match_score,
    }
