from __future__ import annotations

import asyncio
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
    PREVIEW_READY = "PREVIEW_READY"  # 工单预览已生成，等待用户提交或修改
    SUBMITTED = "SUBMITTED"          # 工单已提交到后端系统
    COMPLETED = "COMPLETED"          # 维修工单已完成（由外部系统回调更新）
    ESCALATED = "ESCALATED"


@dataclass
class TicketDraft:
    description: str | None = None
    estate: str | None = None
    building: str | None = None
    floor: str | None = None
    area: str | None = None
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
        """用于展示的简化字典（给 LLM 和前端）"""
        d = {
            "description": self.description,
            "estate": self.estate,
            "building": self.building,
            "floor": self.floor,
            "area": self.area,
            "room": self.room,
            "visit_time": self.visit_time,
            "image_urls": self.image_urls,
        }
        if self.fault_type_name:
            d["fault_type"] = self.fault_type_name
        if self.repair_priority_rag:
            d["priority"] = self.repair_priority_rag
        return d

    def serialize(self) -> dict:
        """完整序列化（用于持久化存储）"""
        return {
            "description": self.description,
            "estate": self.estate,
            "building": self.building,
            "floor": self.floor,
            "area": self.area,
            "room": self.room,
            "visit_time": self.visit_time,
            "image_urls": self.image_urls,
            "normalized_description": self.normalized_description,
            "fault_type_code": self.fault_type_code,
            "fault_type_name": self.fault_type_name,
            "repair_priority_rag": self.repair_priority_rag,
            "repair_type": self.repair_type,
        }

    @classmethod
    def deserialize(cls, data: dict) -> TicketDraft:
        """从字典恢复 TicketDraft 实例"""
        return cls(
            description=data.get("description"),
            estate=data.get("estate"),
            building=data.get("building"),
            floor=data.get("floor"),
            area=data.get("area"),
            room=data.get("room"),
            visit_time=data.get("visit_time"),
            image_urls=data.get("image_urls", []),
            normalized_description=data.get("normalized_description"),
            fault_type_code=data.get("fault_type_code"),
            fault_type_name=data.get("fault_type_name"),
            repair_priority_rag=data.get("repair_priority_rag"),
            repair_type=data.get("repair_type"),
        )


@dataclass
class Session:
    session_id: str
    client_id: str
    state: AgentState
    history: list[dict]  # [{"role": "user"|"assistant", "content": "..."}]
    draft: TicketDraft
    created_at: datetime
    expires_at: datetime
    stall_count: int = 0  # 连续多轮缺失字段集合未变化的次数
    last_missing: list[str] = field(default_factory=list)  # 上一轮缺失字段列表
    image_description: str | None = None  # AI 对用户上传图片的故障描述
    user_confirmed_description_priority: bool = False  # 用户是否已确认"以描述为准"（跳过图文一致性检测）
    pending_clarification: dict | None = None  # 上一轮 LLM 返回的澄清问题上下文 {"question": str, "asked_in_state": str}
    ticket: dict | None = None  # 生成的工单快照，提交时直接使用
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)  # 并发控制锁

    def serialize(self) -> dict:
        """完整序列化（用于持久化存储）"""
        return {
            "session_id": self.session_id,
            "client_id": self.client_id,
            "state": self.state.value,
            "history": self.history,
            "draft": self.draft.serialize(),
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "stall_count": self.stall_count,
            "last_missing": self.last_missing,
            "image_description": self.image_description,
            "user_confirmed_description_priority": self.user_confirmed_description_priority,
            "pending_clarification": self.pending_clarification,
            "ticket": self.ticket,
        }

    @classmethod
    def deserialize(cls, data: dict) -> Session:
        """从字典恢复 Session 实例"""
        return cls(
            session_id=data["session_id"],
            client_id=data["client_id"],
            state=AgentState(data["state"]),
            history=data.get("history", []),
            draft=TicketDraft.deserialize(data.get("draft", {})),
            created_at=datetime.fromisoformat(data["created_at"]),
            expires_at=datetime.fromisoformat(data["expires_at"]),
            stall_count=data.get("stall_count", 0),
            last_missing=data.get("last_missing", []),
            image_description=data.get("image_description"),
            user_confirmed_description_priority=data.get("user_confirmed_description_priority", False),
            pending_clarification=data.get("pending_clarification"),
            ticket=data.get("ticket"),
        )


# ── 会话存储函数已迁移至 app.services.session_store ──────────────────────────
# create_session / get_session / refresh_session 现在是异步函数，导入自：
# from app.services.session_store import create_session, get_session, refresh_session
