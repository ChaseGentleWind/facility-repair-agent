from __future__ import annotations

from app.agent.draft_ops import (
    apply_extraction,
    infer_location_from_area_or_room,
)
from app.agent.node_utils import state_event


def describe_field(field: str) -> str:
    return {
        "description": "问题描述",
        "estate": "楼盘/项目",
        "building": "楼栋",
        "floor": "楼层",
        "area": "区域",
        "room": "房间",
    }.get(field, field)


def conflict_question(conflicts: list[dict]) -> str:
    first = conflicts[0]
    label = describe_field(first["field"])
    return (
        f"我发现{label}有不一致：您描述的是“{first['text_value']}”，"
        f"图片看起来像“{first['image_value']}”。本次报修以哪个为准？"
    )


def set_pending_conflict(session, conflicts: list[dict], merged: dict) -> None:
    session.pending_conflict = {
        "question": conflict_question(conflicts),
        "conflicts": conflicts,
        "base_extraction": {
            key: value
            for key, value in merged.items()
            if key in {"description", "estate", "building", "floor", "area", "room"} and value
        },
    }


def resolve_pending_conflict(session, user_message: str, events: list[dict]) -> dict | None:
    pending = session.pending_conflict
    if not pending:
        return None

    msg = user_message.lower()
    choose_text = any(kw in msg for kw in ("文字", "描述", "我说", "我的", "按我", "text"))
    choose_image = any(kw in msg for kw in ("图片", "照片", "图里", "image", "photo"))
    if not choose_text and not choose_image:
        question = pending["question"]
        session.history.append({"role": "assistant", "content": question})
        events.append({"type": "text_delta", "content": question})
        events.append(state_event(session))
        return {"events": events, "_conflict_unresolved": True}

    extraction = dict(pending.get("base_extraction") or {})
    description_changed = False

    for conflict in pending.get("conflicts", []):
        field = conflict["field"]
        old_value = getattr(session.draft, field, None)
        if choose_text:
            extraction[field] = conflict["text_value"]
            if field == "description":
                session.user_confirmed_description_priority = True
        else:
            extraction[field] = conflict["image_value"]
            if field == "description":
                session.user_confirmed_description_priority = False
        if field == "description" and (extraction[field] != old_value or choose_text or choose_image):
            description_changed = True

    infer_location_from_area_or_room(extraction, session.draft)
    apply_extraction(session.draft, extraction, None)
    session.pending_conflict = None
    session.pending_clarification = None

    text = "好的，已按您的选择更新报修信息。"
    session.history.append({"role": "assistant", "content": text})
    events.append({"type": "text_delta", "content": text})
    events.append(state_event(session))
    return {
        "events": events,
        "_conflict_resolved": True,
        "_need_rerag": description_changed,
        "_extraction": {"conflict_resolved": True},
    }
