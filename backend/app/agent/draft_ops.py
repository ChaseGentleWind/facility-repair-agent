from __future__ import annotations

import logging
import re

from app.agent.state import TicketDraft

logger = logging.getLogger(__name__)


def infer_location_from_area_or_room(extraction: dict, draft: TicketDraft) -> None:
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
