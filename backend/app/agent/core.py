from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import AsyncIterator

from app.agent.state import AgentState, Session, TicketDraft
from app.agent.ticket_builder import build_ticket
from app.config import settings
from app.services import llm
from app.services import rag

logger = logging.getLogger(__name__)

_SKIP_KEYWORDS = {"跳过", "不用", "没有", "算了", "不需要", "skip"}


async def process_message(
    session: Session,
    user_message: str,
    image_url: str | None = None,
) -> AsyncIterator[dict]:
    session.history.append({"role": "user", "content": user_message})

    if session.state == AgentState.GREETING:
        session.state = AgentState.COLLECTING

    try:
        if session.state == AgentState.COLLECTING:
            async for event in _handle_collecting(session, user_message, image_url):
                yield event
        elif session.state == AgentState.WAITING_IMAGE:
            async for event in _handle_waiting_image(session, user_message, image_url):
                yield event
        elif session.state == AgentState.CONFIRMING:
            async for event in _handle_confirming(session, user_message, image_url):
                yield event
        elif session.state == AgentState.PREVIEW_READY:
            async for event in _handle_preview_ready(session, user_message, image_url):
                yield event
        elif session.state == AgentState.ESCALATED:
            yield {"type": "text_delta", "content": "已为您转接人工客服，如有其他问题请刷新页面重新发起。"}
        elif session.state == AgentState.COMPLETED:
            yield {"type": "text_delta", "content": "报修单已提交，如有其他问题请刷新页面重新发起。"}
        elif session.state == AgentState.SUBMITTED:
            yield {"type": "text_delta", "content": "工单已提交，如有其他问题请刷新页面重新发起。"}
        else:
            yield {"type": "error", "code": "INVALID_STATE", "message": "无效的会话状态"}
    except Exception as exc:
        logger.exception("process_message error: %s", exc)
        yield {"type": "error", "code": "INTERNAL_ERROR", "message": "服务异常，请稍后重试"}

    yield {"type": "done"}


# ── COLLECTING 阶段 ──────────────────────────────────────────────────────────

async def _handle_collecting(
    session: Session,
    user_message: str,
    
    image_url: str | None,
) -> AsyncIterator[dict]:
    # 提取字段（有图片时会同时生成 image_description_text）
    extraction = await llm.extract_fields(session.draft, user_message, image_url)

    if extraction.get("_error"):
        msg = "系统繁忙，请稍后再试一次。"
        session.history.append({"role": "assistant", "content": msg})
        yield {"type": "text_delta", "content": msg}
        yield {"type": "state_update", "state": session.state.value, "collected": session.draft.to_dict()}
        return

    # 如果有图片描述，先展示给用户
    image_description = extraction.get("image_description_text")
    if image_description:
        session.image_description = image_description
        session.history.append({"role": "assistant", "content": image_description})
        yield {"type": "text_delta", "content": image_description}

    # COLLECTING 阶段：新图片替换旧图，避免累积
    if image_url:
        session.draft.image_urls = [image_url]

    _infer_location_from_area_or_room(extraction, session.draft)
    _apply_extraction(session.draft, extraction, None)

    # 若本轮提取到 visit_time_text 且 draft 中尚未解析，立即解析为绝对时间
    visit_time_text = extraction.get("visit_time_text")
    # 兜底：LLM 未提取但消息本身就是已知紧急时间词（如"越快越好"、"尽快"）
    if not visit_time_text and not session.draft.visit_time:
        from app.services.llm import _DEFAULT_KEYWORD
        if any(kw in user_message for kw in _DEFAULT_KEYWORD):
            visit_time_text = user_message.strip()
    if visit_time_text:
        session.draft.visit_time = await llm.resolve_visit_time(visit_time_text, datetime.now())

    # 兜底检查：防止 LLM 误判 needs_human
    # 只有用户消息中明确包含"人工"或"客服"才允许转人工
    needs_human = extraction.get("needs_human")
    if needs_human:
        user_msg_lower = user_message.lower()
        if "人工" not in user_msg_lower and "客服" not in user_msg_lower:
            # LLM 误判，强制覆盖为 false
            logger.warning("LLM 误判 needs_human=true，但消息中不包含'人工'或'客服': %s", user_message)
            needs_human = False

    if needs_human:
        session.state = AgentState.ESCALATED
        yield {
            "type": "human_service",
            "session_id": session.session_id,
            "partial_ticket": session.draft.to_dict(),
        }
        return

    clarification = extraction.get("clarification_question")
    if clarification:
        session.history.append({"role": "assistant", "content": clarification})
        yield {"type": "text_delta", "content": clarification}
        yield {
            "type": "state_update",
            "state": session.state.value,
            "collected": session.draft.to_dict(),
        }
        return

    missing = session.draft.missing_required()

    if not missing:
        if not session.draft.image_urls:
            # 必填项齐全但无图片 → 追问图片
            session.state = AgentState.WAITING_IMAGE
            prompt = '请问您能提供一张现场照片吗？（可跳过，直接回复"跳过"）'
            session.history.append({"role": "assistant", "content": prompt})
            yield {"type": "text_delta", "content": prompt}
            yield {
                "type": "state_update",
                "state": session.state.value,
                "collected": session.draft.to_dict(),
            }
        else:
            async for event in _run_rag_and_confirm(session):
                yield event
    else:
        # 检测是否卡住：连续多轮缺失字段集合没有减少
        if set(missing) == set(session.last_missing):
            session.stall_count += 1
        else:
            session.stall_count = 0
            session.last_missing = missing

        if session.stall_count >= settings.max_stall_count:
            session.state = AgentState.ESCALATED
            yield {
                "type": "human_service",
                "session_id": session.session_id,
                "partial_ticket": session.draft.to_dict(),
                "reason": f"stalled_on_fields:{','.join(missing)}",
            }
            return

        reply_text = ""
        async for chunk in llm.generate_reply_stream(session.draft, session.history, missing):
            reply_text += chunk
            yield {"type": "text_delta", "content": chunk}
        session.history.append({"role": "assistant", "content": reply_text})
        yield {
            "type": "state_update",
            "state": session.state.value,
            "collected": session.draft.to_dict(),
        }


# ── WAITING_IMAGE 阶段 ───────────────────────────────────────────────────────

async def _handle_waiting_image(
    session: Session,
    user_message: str,
    image_url: str | None,
) -> AsyncIterator[dict]:
    skipped = any(kw in user_message for kw in _SKIP_KEYWORDS)

    if image_url:
        # 提取字段（会同时生成 image_description_text）
        extraction = await llm.extract_fields(session.draft, user_message, image_url)

        if extraction.get("_error"):
            msg = "系统繁忙，请重新发送图片试试。"
            session.history.append({"role": "assistant", "content": msg})
            yield {"type": "text_delta", "content": msg}
            yield {"type": "state_update", "state": session.state.value, "collected": session.draft.to_dict()}
            return

        # 如果有图片描述，先展示给用户
        image_description = extraction.get("image_description_text")
        if image_description:
            session.image_description = image_description
            session.history.append({"role": "assistant", "content": image_description})
            yield {"type": "text_delta", "content": image_description}

        # WAITING_IMAGE 阶段：新图片替换旧图，避免累积
        session.draft.image_urls = [image_url]

        _infer_location_from_area_or_room(extraction, session.draft)
        _apply_extraction(session.draft, extraction, None)
    elif not skipped:
        # 用户既没上传图片也没跳过，继续等待
        prompt = '您可以拍一张现场照片发给我，或者回复"跳过"直接提交报修。'
        session.history.append({"role": "assistant", "content": prompt})
        yield {"type": "text_delta", "content": prompt}
        yield {
            "type": "state_update",
            "state": session.state.value,
            "collected": session.draft.to_dict(),
        }
        return

    # 收到图片或用户跳过 → 进入 RAG + 确认
    async for event in _run_rag_and_confirm(session):
        yield event


# ── RAG 检索 + 生成确认摘要 ──────────────────────────────────────────────────

async def _run_rag_and_confirm(session: Session) -> AsyncIterator[dict]:
    image_url = session.draft.image_urls[0] if session.draft.image_urls else None
    # 若用户已确认"以描述为准"，RAG 检索时忽略图片，避免图文矛盾导致错误匹配
    ignore_image = session.user_confirmed_description_priority
    rag_result = await rag.search_fault(session.draft.description, image_url, ignore_image=ignore_image)
    if rag_result:
        session.draft.normalized_description = rag_result.normalized_description
        session.draft.fault_type_code = rag_result.fault_type_code
        session.draft.fault_type_name = rag_result.fault_type_name
        session.draft.repair_priority_rag = rag_result.repair_priority
        session.draft.repair_type = rag_result.repair_type

    visit_time = session.draft.visit_time

    session.state = AgentState.CONFIRMING
    reply_text = ""
    async for chunk in llm.generate_confirmation_stream(session.draft, visit_time):
        reply_text += chunk
        yield {"type": "text_delta", "content": chunk}
    session.history.append({"role": "assistant", "content": reply_text})
    yield {
        "type": "state_update",
        "state": session.state.value,
        "collected": session.draft.to_dict(),
    }


# ── CONFIRMING 阶段 ──────────────────────────────────────────────────────────

async def _handle_confirming(
    session: Session,
    user_message: str,
    image_url: str | None,
) -> AsyncIterator[dict]:
    confirmed = await llm.check_user_confirmed(user_message)

    if confirmed:
        ticket = build_ticket(session)
        session.ticket = ticket
        session.state = AgentState.PREVIEW_READY
        logger.info("ticket_ready: session=%s ticket_id=%s", session.session_id, ticket.get("ticket_id"))
        yield {"type": "ticket_ready", "ticket": ticket}
        yield {"type": "text_delta", "content": "好的！报修单预览已生成。请点击「提交工单」按钮完成提交，或告诉我需要修改的内容。"}
    else:
        # 三分支否定逻辑：判断用户意图
        intent = await llm.classify_denial_intent(user_message)
        logger.info("[CONFIRMING] denial intent: %s, message: %s", intent, user_message)

        if intent == "restart":
            # 完全推翻，清空 draft，回到 COLLECTING
            session.draft = TicketDraft()
            session.state = AgentState.COLLECTING
            session.stall_count = 0
            session.last_missing = []
            msg = "好的，我们重新开始。请描述您遇到的问题。"
            session.history.append({"role": "assistant", "content": msg})
            yield {"type": "text_delta", "content": msg}
            yield {
                "type": "state_update",
                "state": session.state.value,
                "collected": session.draft.to_dict(),
            }

        elif intent == "modify":
            # 修改已有字段 → 用 editing 提取
            session.state = AgentState.COLLECTING
            extraction = await llm.extract_fields_editing(session.draft, user_message, image_url)

            if extraction.get("_error"):
                msg = "系统繁忙，请稍后再试一次。"
                session.history.append({"role": "assistant", "content": msg})
                yield {"type": "text_delta", "content": msg}
                yield {"type": "state_update", "state": session.state.value, "collected": session.draft.to_dict()}
                return

            image_description = extraction.get("image_description_text")
            if image_description:
                session.image_description = image_description
                session.history.append({"role": "assistant", "content": image_description})
                yield {"type": "text_delta", "content": image_description}

            if image_url:
                session.draft.image_urls = [image_url]

            _infer_location_from_area_or_room(extraction, session.draft)
            _apply_extraction(session.draft, extraction, None)
            visit_time_text = extraction.get("visit_time_text")
            if visit_time_text:
                session.draft.visit_time = await llm.resolve_visit_time(visit_time_text, datetime.now())

            missing = session.draft.missing_required()
            if not missing:
                async for event in _run_rag_and_confirm(session):
                    yield event
            else:
                reply_text = ""
                async for chunk in llm.generate_reply_stream(session.draft, session.history, missing):
                    reply_text += chunk
                    yield {"type": "text_delta", "content": chunk}
                session.history.append({"role": "assistant", "content": reply_text})
                yield {
                    "type": "state_update",
                    "state": session.state.value,
                    "collected": session.draft.to_dict(),
                }

        else:
            # unclear：追问用户想修改什么
            msg = "请问您需要修改哪一项？比如位置、时间或问题描述。"
            session.history.append({"role": "assistant", "content": msg})
            yield {"type": "text_delta", "content": msg}
            yield {
                "type": "state_update",
                "state": session.state.value,
                "collected": session.draft.to_dict(),
            }


# ── PREVIEW_READY 阶段（工单预览已生成，用户可修改或提交）──────────────────

async def _handle_preview_ready(
    session: Session,
    user_message: str,
    image_url: str | None,
) -> AsyncIterator[dict]:
    """
    PREVIEW_READY 阶段：用户可以修改字段或确认提交
    """
    # 提取修改内容（有图片时会同时生成 image_description_text）
    extraction = await llm.extract_fields_editing(session.draft, user_message, image_url)
    logger.info("[PREVIEW_READY] LLM extraction result: %s", extraction)

    if extraction.get("_error"):
        msg = "系统繁忙，请稍后再试一次。"
        session.history.append({"role": "assistant", "content": msg})
        yield {"type": "text_delta", "content": msg}
        yield {"type": "state_update", "state": session.state.value, "collected": session.draft.to_dict()}
        return

    # 如果有图片描述，先展示给用户
    image_description = extraction.get("image_description_text")
    if image_description:
        session.image_description = image_description
        session.history.append({"role": "assistant", "content": image_description})
        yield {"type": "text_delta", "content": image_description}

    # 检测用户是否确认"以描述为准"
    if extraction.get("user_confirmed_description_priority"):
        session.user_confirmed_description_priority = True

    description_changed = bool(extraction.get("description"))
    image_changed = image_url is not None
    # 检测 area / room 是否改变（含连字符的 area 与纯房号 room 互斥）
    new_area = extraction.get("area")
    area_changed = new_area is not None and new_area != session.draft.area
    new_room = extraction.get("room")
    room_changed = new_room is not None and new_room != session.draft.room
    new_floor = extraction.get("floor")
    floor_changed = new_floor is not None and new_floor != session.draft.floor
    new_building = extraction.get("building")
    building_changed = new_building is not None and new_building != session.draft.building
    logger.info("[PREVIEW_READY] new_area=%s new_room=%s area_changed=%s room_changed=%s new_floor=%s floor_changed=%s new_building=%s building_changed=%s",
                new_area, new_room, area_changed, room_changed, new_floor, floor_changed, new_building, building_changed)

    # PREVIEW_READY 阶段：新图片整体替换旧图，避免 RAG 混入旧图内容
    if image_changed:
        session.draft.image_urls = [image_url]
        # 用户上传了新图片，重置"以描述为准"标记
        session.user_confirmed_description_priority = False

    # 用户改了 area → 同时重推 building/floor（除非用户也明确给了新值）
    if area_changed:
        if not building_changed:
            logger.info("[PREVIEW_READY] area 变更，清空旧 building 准备重推: %s → None", session.draft.building)
            session.draft.building = None
            extraction["building"] = None
        if not floor_changed:
            logger.info("[PREVIEW_READY] area 变更，清空旧 floor 准备重推: %s → None", session.draft.floor)
            session.draft.floor = None
            extraction["floor"] = None

    # 用户改了 room → 重新推断 floor（除非用户明确提供了新楼层）
    if room_changed:
        room_clean = re.sub(r'(房间|会议室|办公室|卫生间|茶水间|储藏室|仓库|机房|配电室|停车场)$', '', new_room or '')
        llm_may_infer_floor = bool(re.match(r'^\d{3,4}$', room_clean))
        if not floor_changed or llm_may_infer_floor:
            logger.info("[PREVIEW_READY] room 变更，清空旧楼层准备重推: %s → None (llm_may_infer=%s)",
                       session.draft.floor, llm_may_infer_floor)
            session.draft.floor = None
            extraction["floor"] = None

    # image_url 传 None，防止 _apply_extraction 再次 append
    logger.info("[PREVIEW_READY] 调用推断函数前: extraction=%s, draft.floor=%s", extraction, session.draft.floor)
    _infer_location_from_area_or_room(extraction, session.draft)
    logger.info("[PREVIEW_READY] 调用推断函数后: extraction=%s, draft.floor=%s", extraction, session.draft.floor)
    _apply_extraction(session.draft, extraction, None)
    logger.info("[PREVIEW_READY] 应用提取结果后: draft.room=%s, draft.floor=%s", session.draft.room, session.draft.floor)

    # 时间：本轮提到了才更新，没提到保留原值
    visit_time_text = extraction.get("visit_time_text")
    if visit_time_text:
        session.draft.visit_time = await llm.resolve_visit_time(visit_time_text, datetime.now())

    # 图文一致性校验：用户改了描述但未换图片，且之前有图片，且用户未确认"以描述为准"
    if (
        description_changed
        and not image_changed
        and session.draft.image_urls
        and not session.user_confirmed_description_priority
    ):
        old_image = session.draft.image_urls[0]
        # 用旧图片重新做 VLM 分析，检测图文是否一致
        re_extraction = await llm.extract_fields_editing(session.draft, user_message, old_image)

        # 如果 VLM 生成了 clarification_question（检测到图文矛盾）
        clarification = re_extraction.get("clarification_question")
        if clarification:
            session.history.append({"role": "assistant", "content": clarification})
            yield {"type": "text_delta", "content": clarification}
            yield {
                "type": "state_update",
                "state": session.state.value,
                "collected": session.draft.to_dict(),
            }
            return  # 等待用户确认后再继续，不立即调用 RAG

    # description 或图片变了 → 清空 RAG 字段，重新检索 + 确认
    if description_changed or image_changed:
        _clear_rag_fields(session.draft)
        async for event in _run_rag_and_confirm(session):
            yield event
        return

    # 只改了位置/时间等非核心字段 → 直接重新生成预览
    ticket = build_ticket(session)
    session.ticket = ticket
    yield {"type": "ticket_ready", "ticket": ticket}
    yield {"type": "text_delta", "content": "已更新预览，请确认或继续修改。"}
    yield {
        "type": "state_update",
        "state": session.state.value,
        "collected": session.draft.to_dict(),
    }


# ── 工具函数 ─────────────────────────────────────────────────────────────────

def _infer_location_from_area_or_room(extraction: dict, draft: TicketDraft) -> None:
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

    # 互斥保护：area 优先
    if area and room:
        logger.warning("[互斥] LLM 同时提取了 area=%s 和 room=%s，保留 area，清空 room", area, room)
        room = None
        extraction["room"] = None

    floor_extracted = extraction.get("floor")
    building_extracted = extraction.get("building")

    # ── area 推断 ──────────────────────────────────────────────────────────
    if area:
        area_clean = re.sub(
            r'(房间|会议室|办公室|卫生间|茶水间|储藏室|仓库|机房|配电室|停车场)$', '', area
        )
        # 2-L28 / 3-B05 → building=T2/T3, floor=L28/B5
        if m := re.match(r'^(\d+)-([LB])(\d+)$', area_clean):
            b_num, prefix, num = m.group(1), m.group(2), int(m.group(3))
            if not building_extracted and not draft.building:
                extraction["building"] = f"T{b_num}"
                logger.info("从 area '%s' 推断楼栋: T%s", area, b_num)
            if not floor_extracted and not draft.floor:
                extraction["floor"] = f"{prefix}{num}楼" if prefix == "L" else f"B{num}"
                logger.info("从 area '%s' 推断楼层: %s", area, extraction["floor"])
        # 8-2401 → building=T8, floor=24楼
        elif m := re.match(r'^(\d+)-(\d{4})$', area_clean):
            b_num, room_num = m.group(1), m.group(2)
            if not building_extracted and not draft.building:
                extraction["building"] = f"T{b_num}"
                logger.info("从 area '%s' 推断楼栋: T%s", area, b_num)
            if not floor_extracted and not draft.floor:
                extraction["floor"] = f"{room_num[:2]}楼"
                logger.info("从 area '%s' 推断楼层: %s", area, extraction["floor"])
        # 2-701A / 2-301 → building=T2, floor=7楼/3楼
        elif m := re.match(r'^(\d+)-(\d{3})[A-Z]?\d*$', area_clean):
            b_num, room_num = m.group(1), m.group(2)
            if not building_extracted and not draft.building:
                extraction["building"] = f"T{b_num}"
                logger.info("从 area '%s' 推断楼栋: T%s", area, b_num)
            if not floor_extracted and not draft.floor:
                extraction["floor"] = f"{room_num[0]}楼"
                logger.info("从 area '%s' 推断楼层: %s", area, extraction["floor"])
        return

    # ── room 推断 ──────────────────────────────────────────────────────────
    if not room or floor_extracted or draft.floor:
        return

    room_clean = re.sub(
        r'(房间|会议室|办公室|卫生间|茶水间|储藏室|仓库|机房|配电室|停车场)$', '', room
    )

    # 3位纯数字：302 → 3楼
    if re.match(r'^\d{3}$', room_clean):
        extraction["floor"] = f"{room_clean[0]}楼"
        logger.info("从房间号 '%s' 推断楼层: %s", room, extraction["floor"])
    # 4位纯数字：1205 → 12楼，4505 → 45楼
    elif re.match(r'^\d{4}$', room_clean):
        extraction["floor"] = f"{room_clean[:2]}楼"
        logger.info("从房间号 '%s' 推断楼层: %s", room, extraction["floor"])
    # 数字+字母+数字：7S1 → 7楼, 12B5 → 12楼
    elif m := re.match(r'^(\d{1,2})[A-Z]\d+', room_clean):
        extraction["floor"] = f"{m.group(1)}楼"
        logger.info("从房间号 '%s' 推断楼层: %s", room, extraction["floor"])


def _clear_rag_fields(draft: TicketDraft) -> None:
    draft.normalized_description = None
    draft.fault_type_code = None
    draft.fault_type_name = None
    draft.repair_priority_rag = None
    draft.repair_type = None


def _apply_extraction(draft: TicketDraft, extraction: dict, image_url: str | None) -> None:
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
        # area 与 room 互斥：本轮提取到 area 时清空 draft.room
        draft.room = None
    if extraction.get("room"):
        draft.room = extraction["room"]
        # area 与 room 互斥：本轮提取到 room 时清空 draft.area
        draft.area = None
    if image_url and image_url not in draft.image_urls:
        draft.image_urls.append(image_url)
