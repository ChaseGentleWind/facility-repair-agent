from __future__ import annotations

import random

from app.agent.state import Session
from app.services.ticket_counter import get_ticket_counter

# code → 规范显示名，消除 ChromaDB 历史数据中同一 code 多种写法的问题
_FAULT_TYPE_CANONICAL: dict[str, str] = {
    "100": "空调报修类",
    "200": "电气报修类",
    "300": "给排水报修类",
    "400": "土木工报修类",
    "500": "公共设备设施报修类",
    "J00": "公寓维修类",
}


async def build_ticket(session: Session) -> dict:
    """构建工单 JSON（异步获取全局唯一 repair_no）"""
    counter = get_ticket_counter()
    repair_no = await counter.next()

    draft = session.draft
    ticket_id = str(random.randint(10**16, 10**17 - 1))

    ft_code = draft.fault_type_code or "000"
    ft_name = _FAULT_TYPE_CANONICAL.get(ft_code, draft.fault_type_name or "待分类")
    priority = draft.repair_priority_rag or "MEDIUM"
    problem_description = draft.normalized_description or draft.description or ""

    return {
        "ticket_id": ticket_id,
        "repair_no": repair_no,
        "order_status": "PREVIEW",
        "repair_type": draft.repair_type or "公司报修",
        "location": {
            "estate": draft.estate,
            "building": draft.building,
            "floor": draft.floor,
            "area": draft.area,
            "room": draft.room,
        },
        "raw_problem_description": draft.description or "",
        "problem_description": problem_description,
        "image_urls": draft.image_urls,
        "reporter": {"name": None, "phone": None},
        "visit_time": draft.visit_time,
        "repair_priority": priority,
        "fault_type": {
            "code": ft_code,
            "displayName": ft_name,
        },
    }
