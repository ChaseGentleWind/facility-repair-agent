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
        elif session.state == AgentState.EDITING:
            async for event in _handle_editing(session, user_message, image_url):
                yield event
        elif session.state == AgentState.ESCALATED:
            yield {"type": "text_delta", "content": "已为您转接人工客服，如有其他问题请刷新页面重新发起。"}
        elif session.state == AgentState.COMPLETED:
            yield {"type": "text_delta", "content": "报修单已提交，如有其他问题请刷新页面重新发起。"}
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

    # 如果有图片描述，先展示给用户
    image_description = extraction.get("image_description_text")
    if image_description:
        session.image_description = image_description
        session.history.append({"role": "assistant", "content": image_description})
        yield {"type": "text_delta", "content": image_description}

        # 追加确认问句
        confirm_prompt = "\n\n这是您要报修的问题吗？"
        session.history.append({"role": "assistant", "content": confirm_prompt})
        yield {"type": "text_delta", "content": confirm_prompt}

    # COLLECTING 阶段：新图片替换旧图，避免累积
    if image_url:
        session.draft.image_urls = [image_url]

    _infer_floor_from_room(extraction, session.draft)
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
        session.retry_count += 1
        if session.retry_count >= settings.max_retry_count and not session.draft.description:
            session.state = AgentState.ESCALATED
            yield {
                "type": "human_service",
                "session_id": session.session_id,
                "partial_ticket": session.draft.to_dict(),
                "reason": "max_retries",
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

        # 如果有图片描述，先展示给用户
        image_description = extraction.get("image_description_text")
        if image_description:
            session.image_description = image_description
            session.history.append({"role": "assistant", "content": image_description})
            yield {"type": "text_delta", "content": image_description}

        # WAITING_IMAGE 阶段：新图片替换旧图，避免累积
        session.draft.image_urls = [image_url]

        _infer_floor_from_room(extraction, session.draft)
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

    # 若全程未提及上门时间，使用默认值 now+30min
    if not session.draft.visit_time:
        now = datetime.now()
        default = now + timedelta(minutes=30)
        session.draft.visit_time = f"{default.month}月{default.day}日 {default.hour}时{default.minute:02d}分"

    visit_time = session.draft.visit_time

    session.state = AgentState.CONFIRMING
    reply_text = ""
    async for chunk in llm.generate_confirmation_stream(session.draft, session.history, visit_time):
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
        session.state = AgentState.EDITING
        logger.info("ticket_ready: session=%s ticket_id=%s", session.session_id, ticket.get("ticket_id"))
        yield {"type": "ticket_ready", "ticket": ticket}
        yield {"type": "text_delta", "content": "好的！报修单预览已生成，信息已同步至预览页面。如需修改，请直接告诉我。"}
    else:
        session.state = AgentState.COLLECTING

        # 提取字段（有图片时会同时生成 image_description_text）
        extraction = await llm.extract_fields(session.draft, user_message, image_url)

        # 如果有图片描述，先展示给用户
        image_description = extraction.get("image_description_text")
        if image_description:
            session.image_description = image_description
            session.history.append({"role": "assistant", "content": image_description})
            yield {"type": "text_delta", "content": image_description}

        # CONFIRMING 阶段用户否认后重新收集：新图片替换旧图，避免累积
        if image_url:
            session.draft.image_urls = [image_url]

        _infer_floor_from_room(extraction, session.draft)
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


# ── EDITING 阶段（ticket_ready 后用户要求修改）────────────────────────────────

async def _handle_editing(
    session: Session,
    user_message: str,
    image_url: str | None,
) -> AsyncIterator[dict]:
    # 提取修改内容（有图片时会同时生成 image_description_text）
    extraction = await llm.extract_fields_editing(session.draft, user_message, image_url)
    logger.info("[EDITING] LLM extraction result: %s", extraction)

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
    # 真正检测 room 是否改变：提取到了新 room 且与原值不同
    new_room = extraction.get("room")
    room_changed = new_room is not None and new_room != session.draft.room
    new_floor = extraction.get("floor")
    floor_changed = new_floor is not None and new_floor != session.draft.floor
    logger.info("[EDITING] new_room=%s, old_room=%s, room_changed=%s, new_floor=%s, old_floor=%s, floor_changed=%s",
                new_room, session.draft.room, room_changed, new_floor, session.draft.floor, floor_changed)

    # EDITING 阶段：新图片整体替换旧图，避免 RAG 混入旧图内容
    if image_changed:
        session.draft.image_urls = [image_url]
        # 用户上传了新图片，重置"以描述为准"标记
        session.user_confirmed_description_priority = False

    # 若用户改了房间号，需要重新推断楼层（除非用户明确提供了新楼层）
    # 判断逻辑：room 改变了 且 (floor 没变 或 LLM 可能从 room 自动推断了 floor)
    if room_changed:
        # 检查 LLM 是否可能从 room 自动推断了 floor（3位或4位纯数字）
        room_clean = re.sub(r'(房间|会议室|办公室|卫生间|茶水间|储藏室|仓库|机房|配电室|停车场)$', '', new_room or '')
        llm_may_infer_floor = bool(re.match(r'^\d{3,4}$', room_clean))

        # 如果 floor 没变，或者 LLM 可能自动推断了 floor，都需要重新推断
        if not floor_changed or llm_may_infer_floor:
            logger.info("[EDITING] 清空旧楼层，准备重新推断: draft.floor=%s → None (llm_may_infer=%s)",
                       session.draft.floor, llm_may_infer_floor)
            session.draft.floor = None
            # 同时清空 extraction 中的 floor，确保推断逻辑生效
            extraction["floor"] = None

    # image_url 传 None，防止 _apply_extraction 再次 append
    logger.info("[EDITING] 调用推断函数前: extraction=%s, draft.floor=%s", extraction, session.draft.floor)
    _infer_floor_from_room(extraction, session.draft)
    logger.info("[EDITING] 调用推断函数后: extraction=%s, draft.floor=%s", extraction, session.draft.floor)
    _apply_extraction(session.draft, extraction, None)
    logger.info("[EDITING] 应用提取结果后: draft.room=%s, draft.floor=%s", session.draft.room, session.draft.floor)

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

    # description 或图片变了 → 清空 RAG 字段，重新检索
    if description_changed or image_changed:
        _clear_rag_fields(session.draft)

    async for event in _run_rag_and_confirm(session):
        yield event


# ── 工具函数 ─────────────────────────────────────────────────────────────────

def _infer_floor_from_room(extraction: dict, draft: TicketDraft) -> None:
    """
    若 LLM 提取到 room 但未提取 floor，且 draft.floor 为空，从 room 推断 floor。

    推断规则：
    - 3位纯数字（302）→ 3楼
    - 4位纯数字（1205）→ 12楼
    - 数字+字母+数字（7S1、12B5）→ 首位数字+楼
    - 连字符格式（2-L29、3-B05）→ L29/B05
    """
    room = extraction.get("room")
    floor_extracted = extraction.get("floor")

    logger.info("[推断楼层] room=%s, floor_extracted=%s, draft.floor=%s", room, floor_extracted, draft.floor)

    # 只在以下情况推断：LLM 未提取 floor，且 draft 中也没有 floor
    if not room or floor_extracted or draft.floor:
        logger.info("[推断楼层] 跳过推断: not room=%s, floor_extracted=%s, draft.floor=%s", not room, floor_extracted, draft.floor)
        return

    # 去除中文后缀（"房间"、"会议室"等）后再匹配
    room_clean = re.sub(r'(房间|会议室|办公室|卫生间|茶水间|储藏室|仓库|机房|配电室|停车场)$', '', room)

    # 连字符格式：2-L29 → L29, 3-B05 → B05（优先匹配，因为更具体）
    if m := re.match(r'^\d+-([LB]\d+)', room_clean):
        extraction["floor"] = m.group(1)
        logger.info("从房间号 '%s' 推断楼层: %s", room, extraction["floor"])

    # 3位纯数字：302 → 3楼
    elif re.match(r'^\d{3}$', room_clean):
        extraction["floor"] = f"{room_clean[0]}楼"
        logger.info("从房间号 '%s' 推断楼层: %s", room, extraction["floor"])

    # 4位纯数字：1205 → 12楼
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
    if extraction.get("unit"):
        draft.unit = extraction["unit"]
    if extraction.get("room"):
        draft.room = extraction["room"]
    if image_url and image_url not in draft.image_urls:
        draft.image_urls.append(image_url)
