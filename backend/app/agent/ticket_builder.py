from __future__ import annotations

import random
import threading

from app.agent.state import Session

# 模块级维修单号计数器，重启后重置
_repair_no_counter = 1726198
_counter_lock = threading.Lock()  # 保护计数器的线程锁


def build_ticket(session: Session) -> dict:
    global _repair_no_counter

    # 使用线程锁保护计数器，避免并发时重复
    with _counter_lock:
        repair_no = _repair_no_counter
        _repair_no_counter += 1

    draft = session.draft
    ticket_id = str(random.randint(10**16, 10**17 - 1))

    ft_code = draft.fault_type_code or "000"
    ft_name = draft.fault_type_name or "待分类"
    priority = draft.repair_priority_rag or "MEDIUM"
    problem_description = draft.normalized_description or draft.description or ""

    return {
        "ticket_id": ticket_id,
        "repair_no": repair_no,
        "order_status": "COMPLETED",
        "repair_type": draft.repair_type or "公司报修",
        "location": {
            "estate": draft.estate,
            "building": draft.building,
            "floor": draft.floor,
            "unit": draft.unit,
        },
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
