"""每路径 LLM 调用次数预算回归测试。

锁死的预算（依据计划文档）：
- 纯文本首轮采集：text_extract=1 + reply_stream=1 = 2
- 关键词命中确认：confirmation_intent=0
- 关键词未命中走 LLM 确认：confirmation_intent=1
- 修改时间：confirmation_intent=1 + resolve_visit_time=1 = 2（不再走 RAG，模板化）
- 修改描述（无新图，缓存命中）：confirmation_intent=1 + rag_normalize=1 = 2
- 修改描述（带新图）：confirmation_intent=1 + image_analysis=1 + rag_normalize=1 = 3
- LLM 返回 ErrorResult：节点降级为"系统繁忙"，draft 不被污染
"""
from __future__ import annotations

import pytest

from app.agent.graph import process_message
from app.agent.schemas import (
    ConfirmationIntent,
    ErrorResult,
    ImageAnalysis,
    TextExtraction,
    VisualFields,
)
from app.agent.state import AgentState
from app.services.session_store import create_session


# ── 1. 纯文本首轮采集：2 次 ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_pure_text_first_round_budget(llm_call_counter):
    session = await create_session(client_id="test")

    llm_call_counter.set(
        "text_extract",
        TextExtraction(description="空调不制冷"),
    )
    llm_call_counter.set("reply_stream", ["请问您在哪个园区？"])

    events = []
    async for ev in process_message(session, "空调不制冷", None):
        events.append(ev)

    assert llm_call_counter.hits("text_extract") == 1
    assert llm_call_counter.hits("reply_stream") == 1
    assert llm_call_counter.hits("image_analysis") == 0
    assert llm_call_counter.hits("rag_normalize") == 0
    assert llm_call_counter.total() == 2
    assert session.draft.description == "空调不制冷"


# ── 2. 关键词命中确认：0 次 ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_keyword_confirm_budget(llm_call_counter):
    session = await create_session(client_id="test")
    session.state = AgentState.CONFIRMING
    session.draft.description = "空调不制冷"
    session.draft.estate = "园区A"
    session.draft.building = "T1"
    session.draft.floor = "3楼"
    session.draft.visit_time = "6月3日 14时00分"

    events = []
    async for ev in process_message(session, "好的", None):
        events.append(ev)

    assert llm_call_counter.hits("confirmation_intent") == 0
    assert llm_call_counter.total() == 0
    assert session.state == AgentState.PREVIEW_READY


@pytest.mark.asyncio
async def test_generate_preview_keyword_budget(llm_call_counter):
    session = await create_session(client_id="test")
    session.state = AgentState.CONFIRMING
    session.draft.description = "空调不制冷"
    session.draft.estate = "园区A"
    session.draft.building = "T1"
    session.draft.floor = "3楼"
    session.draft.visit_time = "6月3日 14时00分"

    events = []
    async for ev in process_message(session, "生成预览", None):
        events.append(ev)

    assert llm_call_counter.hits("confirmation_intent") == 0
    assert llm_call_counter.total() == 0
    assert session.state == AgentState.PREVIEW_READY
    assert any(ev.get("type") == "ticket_ready" for ev in events)


# ── 3. 关键词未命中走 LLM 确认：1 次 ───────────────────────────────────────
@pytest.mark.asyncio
async def test_llm_confirm_budget(llm_call_counter):
    session = await create_session(client_id="test")
    session.state = AgentState.CONFIRMING
    session.draft.description = "空调不制冷"
    session.draft.estate = "园区A"
    session.draft.building = "T1"
    session.draft.floor = "3楼"
    session.draft.visit_time = "6月3日 14时00分"

    llm_call_counter.set("confirmation_intent", ConfirmationIntent(intent="confirm"))

    events = []
    async for ev in process_message(session, "感觉差不多", None):
        events.append(ev)

    assert llm_call_counter.hits("confirmation_intent") == 1
    assert llm_call_counter.total() == 1


# ── 4. 修改时间：2 次（confirmation_intent + resolve_visit_time） ──────────
@pytest.mark.asyncio
async def test_modify_time_budget(llm_call_counter):
    session = await create_session(client_id="test")
    session.state = AgentState.CONFIRMING
    session.draft.description = "空调不制冷"
    session.draft.estate = "园区A"
    session.draft.building = "T1"
    session.draft.floor = "3楼"
    session.draft.visit_time = "6月3日 14时00分"

    llm_call_counter.set(
        "confirmation_intent",
        ConfirmationIntent(
            intent="modify",
            modified_fields=TextExtraction(visit_time_text="明天上午十点"),
        ),
    )
    llm_call_counter.set("resolve_visit_time", "6月4日 10时00分")

    events = []
    async for ev in process_message(session, "时间改成明天上午十点", None):
        events.append(ev)

    assert llm_call_counter.hits("confirmation_intent") == 1
    assert llm_call_counter.hits("resolve_visit_time") == 1
    # 时间字段变更走 re_confirm（模板化），不应该触发 RAG 标准化
    assert llm_call_counter.hits("rag_normalize") == 0
    assert llm_call_counter.hits("image_analysis") == 0
    assert llm_call_counter.total() == 2
    assert session.draft.visit_time == "6月4日 10时00分"


# ── 5. 修改描述（无新图，缓存命中）：2 次 ──────────────────────────────────
@pytest.mark.asyncio
async def test_modify_description_cached_image_budget(llm_call_counter, monkeypatch):
    session = await create_session(client_id="test")
    session.state = AgentState.CONFIRMING
    session.draft.description = "空调不制冷"
    session.draft.estate = "园区A"
    session.draft.building = "T1"
    session.draft.floor = "3楼"
    session.draft.visit_time = "6月3日 14时00分"
    session.draft.image_urls = ["/uploads/old.jpg"]
    # 已有 VLM 缓存
    session.image_analysis = ImageAnalysis(
        image_url="/uploads/old.jpg",
        visual_description="天花板有水渍。",
        visual_fault_summary="天花板水渍",
        visual_fields=VisualFields(),
        visual_confidence="medium",
        is_unclear=False,
    )

    # mock RAG 检索（避免加载 ChromaDB）
    async def fake_search(description, *, visual_fault_summary=None, ignore_image=False):
        return None

    monkeypatch.setattr("app.services.rag.search_fault", fake_search)

    llm_call_counter.set(
        "confirmation_intent",
        ConfirmationIntent(
            intent="modify",
            modified_fields=TextExtraction(description="天花板漏水"),
        ),
    )

    events = []
    async for ev in process_message(session, "描述改成天花板漏水", None):
        events.append(ev)

    assert llm_call_counter.hits("confirmation_intent") == 1
    assert llm_call_counter.hits("image_analysis") == 0  # 缓存命中
    # description 变更触发 RAG，但本测试 mock 了 search_fault，不会走 _normalize_description
    assert llm_call_counter.total() == 1
    assert session.draft.description == "天花板漏水"


# ── 6. 修改描述（带新图）：image_analysis=1 + confirmation_intent=1 ────────
@pytest.mark.asyncio
async def test_modify_description_new_image_budget(llm_call_counter, monkeypatch):
    session = await create_session(client_id="test")
    session.state = AgentState.CONFIRMING
    session.draft.description = "空调不制冷"
    session.draft.estate = "园区A"
    session.draft.building = "T1"
    session.draft.floor = "3楼"
    session.draft.visit_time = "6月3日 14时00分"

    async def fake_search(description, *, visual_fault_summary=None, ignore_image=False):
        return None

    monkeypatch.setattr("app.services.rag.search_fault", fake_search)

    llm_call_counter.set(
        "confirmation_intent",
        ConfirmationIntent(
            intent="modify",
            modified_fields=TextExtraction(description="天花板漏水"),
        ),
    )
    llm_call_counter.set(
        "image_analysis",
        ImageAnalysis(
            image_url="/uploads/new.jpg",
            visual_description="天花板大面积水渍。",
            visual_fault_summary="天花板水渍渗漏",
            visual_fields=VisualFields(),
            visual_confidence="high",
            is_unclear=False,
        ),
    )

    events = []
    async for ev in process_message(session, "改成天花板漏水", "/uploads/new.jpg"):
        events.append(ev)

    assert llm_call_counter.hits("confirmation_intent") == 1
    assert llm_call_counter.hits("image_analysis") == 1
    assert llm_call_counter.total() == 2
    assert session.image_analysis is not None
    assert session.image_analysis.image_url == "/uploads/new.jpg"


# ── 7. LLM 返回 ErrorResult：节点降级，draft 未被污染 ──────────────────────
@pytest.mark.asyncio
async def test_error_result_busy_response(llm_call_counter):
    session = await create_session(client_id="test")
    original_desc = session.draft.description

    llm_call_counter.set(
        "text_extract",
        ErrorResult(code="llm_call_failed", purpose="text_extract"),
    )

    events = []
    async for ev in process_message(session, "空调坏了", None):
        events.append(ev)

    # 命中"系统繁忙"分支
    text_events = [e for e in events if e.get("type") == "text_delta"]
    assert any("系统繁忙" in e["content"] for e in text_events)
    assert session.draft.description == original_desc  # 未被污染


# ── 8. 修改时间不应合并旧图片字段 ────────────────────────────────────────────
@pytest.mark.asyncio
async def test_modify_time_does_not_merge_cached_visual_fields(llm_call_counter):
    session = await create_session(client_id="test")
    session.state = AgentState.CONFIRMING
    session.draft.description = "空调不制冷"
    session.draft.estate = "园区A"
    session.draft.building = "T1"
    session.draft.floor = "3楼"
    session.draft.visit_time = "6月3日 14时00分"
    session.draft.image_urls = ["/uploads/old.jpg"]
    session.image_analysis = ImageAnalysis(
        image_url="/uploads/old.jpg",
        visual_description="我看到天花板有水渍。",
        visual_fault_summary="天花板水渍",
        visual_fields=VisualFields(description="天花板漏水", building="T9", floor="9楼"),
        visual_confidence="high",
        is_unclear=False,
    )

    llm_call_counter.set(
        "confirmation_intent",
        ConfirmationIntent(
            intent="modify",
            modified_fields=TextExtraction(visit_time_text="明天上午十点"),
        ),
    )
    llm_call_counter.set("resolve_visit_time", "6月4日 10时00分")

    events = []
    async for ev in process_message(session, "时间改成明天上午十点", None):
        events.append(ev)

    assert llm_call_counter.hits("image_analysis") == 0
    assert session.draft.description == "空调不制冷"
    assert session.draft.building == "T1"
    assert session.draft.floor == "3楼"
    assert session.draft.visit_time == "6月4日 10时00分"


# ── 9. 图片识别失败不写图、不继续推进 ───────────────────────────────────────
@pytest.mark.asyncio
async def test_image_analysis_error_does_not_write_image_or_continue(llm_call_counter, monkeypatch):
    session = await create_session(client_id="test")

    async def fail_if_called(description, *, visual_fault_summary=None, ignore_image=False):
        raise AssertionError("RAG should not run when image analysis failed")

    monkeypatch.setattr("app.services.rag.search_fault", fail_if_called)
    llm_call_counter.set("text_extract", TextExtraction(description="漏水"))
    llm_call_counter.set(
        "image_analysis",
        ErrorResult(code="llm_call_failed", purpose="image_analysis"),
    )

    events = []
    async for ev in process_message(session, "这里漏水", "/uploads/bad.jpg"):
        events.append(ev)

    assert llm_call_counter.hits("text_extract") == 1
    assert llm_call_counter.hits("image_analysis") == 1
    assert "/uploads/bad.jpg" not in session.draft.image_urls
    assert session.draft.description is None
    assert any("图片识别失败" in e.get("content", "") for e in events)


# ── 10. 流式追问空输出使用模板兜底 ──────────────────────────────────────────
@pytest.mark.asyncio
async def test_stream_reply_empty_uses_template_fallback(llm_call_counter):
    session = await create_session(client_id="test")
    llm_call_counter.set("text_extract", TextExtraction(description="空调不制冷"))
    llm_call_counter.set("reply_stream", [])

    events = []
    async for ev in process_message(session, "空调不制冷", None):
        events.append(ev)

    assistant_messages = [m["content"] for m in session.history if m["role"] == "assistant"]
    assert "" not in assistant_messages
    assert any("楼盘/项目名称" in e.get("content", "") for e in events)
    assert any("楼盘/项目名称" in msg for msg in assistant_messages)
