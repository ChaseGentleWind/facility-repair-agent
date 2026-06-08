"""
LangGraph 重构后的集成测试（需要 mock LLM）
"""
import pytest

from app.agent.graph import process_message
from app.agent.schemas import ImageAnalysis, TextExtraction, VisualFields
from app.agent.state import AgentState
from app.services.session_store import create_session


@pytest.mark.asyncio
async def test_graph_basic_flow(llm_call_counter):
    """测试基本流程：entry → collect_extract → collect_decide → stream_reply"""
    session = await create_session(client_id="test_client")

    llm_call_counter.set("text_extract", TextExtraction(description="空调不制冷"))
    llm_call_counter.set("reply_stream", ["请问您在哪个园区？"])

    events = []
    async for event in process_message(session, "空调不制冷", None):
        events.append(event)

    # 验证状态转换
    assert session.state == AgentState.COLLECTING
    assert session.draft.description == "空调不制冷"

    # 验证事件流
    text_events = [e for e in events if e["type"] == "text_delta"]
    assert len(text_events) > 0
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_needs_human_flow(llm_call_counter):
    """测试转人工流程"""
    session = await create_session(client_id="test_client")

    llm_call_counter.set("text_extract", TextExtraction(needs_human=True))

    events = []
    async for event in process_message(session, "我要找人工客服", None):
        events.append(event)

    assert session.state == AgentState.ESCALATED
    assert any(e["type"] == "human_service" for e in events)


@pytest.mark.asyncio
async def test_image_skip_flow(monkeypatch):
    """测试跳过图片流程"""
    session = await create_session(client_id="test_client")
    session.state = AgentState.WAITING_IMAGE
    session.draft.description = "水龙头漏水"
    session.draft.estate = "园区A"
    session.draft.building = "T1"
    session.draft.floor = "3楼"
    session.draft.visit_time = "2026-06-02 10:00"

    async def fake_search(description, *, visual_fault_summary=None, ignore_image=False):
        return None

    monkeypatch.setattr("app.services.rag.search_fault", fake_search)

    events = []
    async for event in process_message(session, "跳过", None):
        events.append(event)

    assert session.state == AgentState.CONFIRMING
    assert any(e.get("type") == "draft_confirm" for e in events)


@pytest.mark.asyncio
async def test_complete_text_flow_does_not_require_image(llm_call_counter, monkeypatch):
    """必填字段齐全后直接进入 RAG/确认，图片只是可选补充。"""
    session = await create_session(client_id="test_client")

    async def fake_search(description, *, visual_fault_summary=None, ignore_image=False):
        return None

    monkeypatch.setattr("app.services.rag.search_fault", fake_search)
    llm_call_counter.set(
        "text_extract",
        TextExtraction(
            description="空调不制冷",
            estate="园区A",
            building="T1",
            floor="3楼",
            visit_time_text="下午三点",
        ),
    )
    llm_call_counter.set("resolve_visit_time", "6月8日 15时00分")

    events = []
    async for event in process_message(session, "园区A T1 3楼空调不制冷，下午三点来", None):
        events.append(event)

    assert session.state == AgentState.CONFIRMING
    assert session.draft.image_urls == []
    assert any(e.get("type") == "draft_confirm" for e in events)


@pytest.mark.asyncio
async def test_image_first_can_fill_description(llm_call_counter):
    """用户先发图片时，图片可作为故障描述证据，再追问缺失字段。"""
    session = await create_session(client_id="test_client")

    llm_call_counter.set("text_extract", TextExtraction())
    llm_call_counter.set(
        "image_analysis",
        ImageAnalysis(
            image_url="/uploads/leak.jpg",
            visual_description="我看到天花板有明显水渍。",
            visual_fault_summary="天花板水渍渗漏",
            visual_fields=VisualFields(description="天花板漏水"),
            visual_confidence="high",
            is_unclear=False,
        ),
    )
    llm_call_counter.set("reply_stream", ["请问在哪个项目的哪栋楼几楼？"])

    events = []
    async for event in process_message(session, "图片已上传", "/uploads/leak.jpg"):
        events.append(event)

    assert session.state == AgentState.COLLECTING
    assert session.draft.description == "天花板漏水"
    assert any("请问在哪个项目" in e.get("content", "") for e in events)


@pytest.mark.asyncio
async def test_text_description_wins_when_image_fault_differs(llm_call_counter, monkeypatch):
    """图文故障描述不一致时默认以用户描述为准，不再打断流程追问。"""
    session = await create_session(client_id="test_client")

    async def fake_search(description, *, visual_fault_summary=None, ignore_image=False):
        assert description == "空调不制冷"
        assert visual_fault_summary == "天花板水渍渗漏"
        assert ignore_image is False
        return None

    monkeypatch.setattr("app.services.rag.search_fault", fake_search)
    llm_call_counter.set(
        "text_extract",
        TextExtraction(
            description="空调不制冷",
            estate="园区A",
            building="T1",
            floor="3楼",
            visit_time_text="下午三点",
        ),
    )
    llm_call_counter.set("resolve_visit_time", "6月8日 15时00分")
    llm_call_counter.set(
        "image_analysis",
        ImageAnalysis(
            image_url="/uploads/leak.jpg",
            visual_description="我看到天花板有明显水渍。",
            visual_fault_summary="天花板水渍渗漏",
            visual_fields=VisualFields(description="天花板漏水"),
            visual_confidence="high",
            is_unclear=False,
        ),
    )

    events = []
    async for event in process_message(session, "园区A T1 3楼空调不制冷，下午三点来", "/uploads/leak.jpg"):
        events.append(event)

    assert session.pending_conflict is None
    assert session.draft.description == "空调不制冷"
    assert session.state == AgentState.CONFIRMING
    draft_events = [e for e in events if e.get("type") == "draft_confirm"]
    assert draft_events
    assert draft_events[-1]["draft"]["description"] == "空调不制冷"
    assert draft_events[-1]["draft"]["image_urls"] == ["/uploads/leak.jpg"]


@pytest.mark.asyncio
async def test_graph_structure():
    """测试图结构完整性"""
    from app.agent.graph import compiled_graph

    # 验证图已编译
    assert compiled_graph is not None

    # 验证关键节点存在
    node_names = list(compiled_graph.nodes.keys())
    expected_nodes = [
        "entry_router",
        "collect_extract",
        "collect_decide",
        "stream_reply",
        "ask_image",
        "wait_image",
        "rag_and_confirm",
        "confirming",
        "preview_edit",
        "escalated",
        "submitted",
        "completed",
        "finalize",
    ]

    for node in expected_nodes:
        assert node in node_names, f"Missing node: {node}"
