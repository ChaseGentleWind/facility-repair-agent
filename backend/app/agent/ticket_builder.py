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
            # normalized_description 由 P1 RAG 阶段填充
            "normalized_description": None,
        },
        # fault_type / repair_priority 由 P1 RAG 阶段填充
        "fault_type": {
            "code": "000",
            "displayName": "待分类",
        },
        "repair_priority": "MEDIUM",
        "repair_type": "公司报修",
        "image_urls": draft.image_urls,
        "confidence": "low",   # P1 RAG 接入后将根据匹配分数更新
        "rag_match_score": None,
    }
