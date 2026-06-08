from __future__ import annotations

import logging
import re

from app.agent.schemas import ImageAnalysis, TextExtraction
from app.agent.state import TicketDraft

logger = logging.getLogger(__name__)


def infer_location_from_area_or_room(
    extraction: dict,
    draft: TicketDraft,
) -> None:
    """
    从 extraction["area"] 或 extraction["room"] 反向推断 building / floor。

    - area：含 `-` 的复合编号（2-L28、8-2401、2-701A、3-B05停车场）
      · 同时推断 building（T<n>）和 floor，仅在 draft 对应字段为空时填入
    - room：不含 `-` 的纯房号（302、4505、7S1）
      · 仅推断 floor，仅在 draft.floor 为空时填入

    互斥处理：area 与 room 同时被填时，记 warning 并清空 room（area 优先）。
    若 LLM 已自行提取 building/floor，规则不再覆盖。
    """
    area = extraction.get("area")
    room = extraction.get("room")

    if area and room:
        logger.warning("[互斥] LLM 同时提取了 area=%s 和 room=%s，保留 area，清空 room", area, room)
        room = None
        extraction["room"] = None

    floor_extracted = extraction.get("floor")
    building_extracted = extraction.get("building")

    if area:
        area_clean = re.sub(
            r'(房间|会议室|办公室|卫生间|茶水间|储藏室|仓库|机房|配电室|停车场)$', '', area
        )
        if m := re.match(r'^(\d+)-([LB])(\d+)$', area_clean):
            b_num, prefix, num = m.group(1), m.group(2), int(m.group(3))
            if not building_extracted and not draft.building:
                extraction["building"] = f"T{b_num}"
                logger.info("从 area '%s' 推断楼栋: T%s", area, b_num)
            if not floor_extracted and not draft.floor:
                extraction["floor"] = f"{prefix}{num}楼" if prefix == "L" else f"B{num}"
                logger.info("从 area '%s' 推断楼层: %s", area, extraction["floor"])
        elif m := re.match(r'^(\d+)-(\d{4})$', area_clean):
            b_num, room_num = m.group(1), m.group(2)
            if not building_extracted and not draft.building:
                extraction["building"] = f"T{b_num}"
                logger.info("从 area '%s' 推断楼栋: T%s", area, b_num)
            if not floor_extracted and not draft.floor:
                extraction["floor"] = f"{room_num[:2]}楼"
                logger.info("从 area '%s' 推断楼层: %s", area, extraction["floor"])
        elif m := re.match(r'^(\d+)-(\d{3})[A-Z]?\d*$', area_clean):
            b_num, room_num = m.group(1), m.group(2)
            if not building_extracted and not draft.building:
                extraction["building"] = f"T{b_num}"
                logger.info("从 area '%s' 推断楼栋: T%s", area, b_num)
            if not floor_extracted and not draft.floor:
                extraction["floor"] = f"{room_num[0]}楼"
                logger.info("从 area '%s' 推断楼层: %s", area, extraction["floor"])
        return

    if not room or floor_extracted or draft.floor:
        return

    room_clean = re.sub(
        r'(房间|会议室|办公室|卫生间|茶水间|储藏室|仓库|机房|配电室|停车场)$', '', room
    )

    if re.match(r'^\d{3}$', room_clean):
        extraction["floor"] = f"{room_clean[0]}楼"
        logger.info("从房间号 '%s' 推断楼层: %s", room, extraction["floor"])
    elif re.match(r'^\d{4}$', room_clean):
        extraction["floor"] = f"{room_clean[:2]}楼"
        logger.info("从房间号 '%s' 推断楼层: %s", room, extraction["floor"])
    elif m := re.match(r'^(\d{1,2})[A-Z]\d+', room_clean):
        extraction["floor"] = f"{m.group(1)}楼"
        logger.info("从房间号 '%s' 推断楼层: %s", room, extraction["floor"])


def clear_rag_fields(draft: TicketDraft) -> None:
    draft.normalized_description = None
    draft.fault_type_code = None
    draft.fault_type_name = None
    draft.repair_priority_rag = None
    draft.repair_type = None


def apply_extraction(draft: TicketDraft, extraction: dict, image_url: str | None) -> None:
    if extraction.get("description"):
        draft.description = extraction["description"]
    if extraction.get("estate"):
        draft.estate = extraction["estate"]
    if extraction.get("building"):
        draft.building = extraction["building"]
    if extraction.get("floor"):
        draft.floor = extraction["floor"]
    if extraction.get("area"):
        draft.area = extraction["area"]
        draft.room = None
    if extraction.get("room"):
        draft.room = extraction["room"]
        draft.area = None
    if image_url and image_url not in draft.image_urls:
        draft.image_urls.append(image_url)


def _make_conflict(field: str, text_value: str, image_value: str) -> dict:
    return {
        "field": field,
        "text_value": text_value,
        "image_value": image_value,
        "text_source": "text",
        "image_source": "image",
    }


def merge_extraction(
    text: TextExtraction | None,
    image: ImageAnalysis | None,
    *,
    user_priority: bool,
) -> dict:
    """图文冲突的唯一裁决点。返回一个 dict（兼容 apply_extraction / infer_location_from_area_or_room）。

    策略：
    - 文本字段优先：text 中非空字段直接采用（用户最权威）
    - 图片补全：text 字段为空且 image 观察到值时，用图片字段补位
    - user_priority=True：忽略图片观察到的所有字段（用户已表态以描述为准）
    - description 例外：text 没说而图片清晰可见时用图片描述顶上，避免 description 为空
    - needs_human / clarification_question / ambiguous_fields 直接透传 text 不与 image 混合
    """
    merged: dict = {}

    text_data = text.model_dump() if text else {}
    image_fields = (
        image.visual_fields.model_dump()
        if (image and not user_priority and not image.is_unclear)
        else {}
    )
    conflicts: list[dict] = []

    for key in ("description", "estate", "building", "floor", "area", "room"):
        text_val = text_data.get(key)
        image_val = image_fields.get(key)
        if text_val and image_val:
            if key == "description":
                # 用户文字是故障事实主来源。图片描述只作为 RAG 辅助和附件证据，
                # 不因故障现象差异打断报修流程。
                merged[key] = text_val
                continue
            if text_val != image_val:
                merged[key] = None
                conflicts.append(_make_conflict(key, text_val, image_val))
                continue
            merged[key] = text_val
        elif text_val:
            merged[key] = text_val
        elif image_val:
            merged[key] = image_val
        else:
            merged[key] = None

    merged["visit_time_text"] = text_data.get("visit_time_text")
    merged["needs_human"] = text_data.get("needs_human", False)
    merged["user_confirmed_description_priority"] = text_data.get(
        "user_confirmed_description_priority", False
    )
    merged["clarification_question"] = text_data.get("clarification_question")
    merged["ambiguous_fields"] = text_data.get("ambiguous_fields", []) or []
    merged["_conflicts"] = conflicts
    return merged
