from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from app.config import settings


class AgentState(str, Enum):
    GREETING = "GREETING"
    COLLECTING = "COLLECTING"
    WAITING_IMAGE = "WAITING_IMAGE"
    CONFIRMING = "CONFIRMING"
    COMPLETED = "COMPLETED"
    ESCALATED = "ESCALATED"


@dataclass
class TicketDraft:
    description: str | None = None
    estate: str | None = None
    building: str | None = None
    floor: str | None = None
    unit: str | None = None
    room: str | None = None
    visit_time: str | None = None        # 用户期望上门时间，格式：M月D日 H时mm分
    image_urls: list[str] = field(default_factory=list)
    # RAG 填充字段
    normalized_description: str | None = None
    fault_type_code: str | None = None
    fault_type_name: str | None = None
    repair_priority_rag: str | None = None
    repair_type: str | None = None

    def missing_required(self) -> list[str]:
        missing = []
        if not self.description:
            missing.append("description")
        if not self.estate:
            missing.append("estate")
        if not self.building:
            missing.append("building")
        if not self.floor:
            missing.append("floor")
        if not self.visit_time:
            missing.append("visit_time")
        return missing

    def to_dict(self) -> dict:
        d = {
            "description": self.description,
            "estate": self.estate,
            "building": self.building,
            "floor": self.floor,
            "unit": self.unit,
            "room": self.room,
            "visit_time": self.visit_time,
            "image_urls": self.image_urls,
        }
        if self.fault_type_name:
            d["fault_type"] = self.fault_type_name
        if self.repair_priority_rag:
            d["priority"] = self.repair_priority_rag
        return d


@dataclass
class Session:
    session_id: str
    client_id: str
    source: str
    state: AgentState
    history: list[dict]  # [{"role": "user"|"assistant", "content": "..."}]
    draft: TicketDraft
    created_at: datetime
    expires_at: datetime
    retry_count: int = 0


# ── 内存会话存储 ──────────────────────────────────────────────────────────────

_store: dict[str, Session] = {}


def create_session(client_id: str, source: str) -> Session:
    now = datetime.now()
    session = Session(
        session_id=f"sess_{uuid.uuid4().hex[:12]}",
        client_id=client_id,
        source=source,
        state=AgentState.GREETING,
        history=[],
        draft=TicketDraft(),
        created_at=now,
        expires_at=now + timedelta(seconds=settings.session_ttl_seconds),
    )
    _store[session.session_id] = session
    return session


def get_session(session_id: str) -> Session | None:
    session = _store.get(session_id)
    if session is None:
        return None
    if session.expires_at < datetime.now():
        del _store[session_id]
        return None
    return session


def refresh_session(session: Session) -> None:
    session.expires_at = datetime.now() + timedelta(seconds=settings.session_ttl_seconds)
