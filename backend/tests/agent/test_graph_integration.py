"""
LangGraph 重构后的集成测试（需要 mock LLM）
"""
import pytest
from unittest.mock import AsyncMock, patch

from app.agent.graph import process_message
from app.agent.state import AgentState, Session, TicketDraft
from app.services.session_store import init_session_store, create_session


@pytest.fixture(autouse=True)
def setup_memory_store():
    """每个测试前初始化内存存储后端"""
    init_session_store(backend="memory")


@pytest.mark.asyncio
async def test_graph_basic_flow():
    """测试基本流程：entry → collect_extract → collect_decide → stream_reply"""
    session = await create_session(client_id="test_client")

    # Mock LLM 返回提取结果
    mock_extraction = {
        "description": "空调不制冷",
        "estate": None,
        "building": None,
        "floor": None,
    }

    with patch("app.services.llm.extract_fields", new_callable=AsyncMock) as mock_extract, \
         patch("app.services.llm.generate_reply_stream") as mock_reply:

        mock_extract.return_value = mock_extraction

        async def mock_stream():
            yield "请问您在哪个园区？"

        mock_reply.return_value = mock_stream()

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
async def test_needs_human_flow():
    """测试转人工流程"""
    session = await create_session(client_id="test_client")

    mock_extraction = {
        "needs_human": True,
    }

    with patch("app.services.llm.extract_fields", new_callable=AsyncMock) as mock_extract:
        mock_extract.return_value = mock_extraction

        events = []
        async for event in process_message(session, "我要找人工客服", None):
            events.append(event)

        assert session.state == AgentState.ESCALATED
        assert any(e["type"] == "human_service" for e in events)


@pytest.mark.asyncio
async def test_image_skip_flow():
    """测试跳过图片流程"""
    session = await create_session(client_id="test_client")
    session.state = AgentState.WAITING_IMAGE
    session.draft.description = "水龙头漏水"
    session.draft.estate = "园区A"
    session.draft.building = "T1"
    session.draft.floor = "3楼"
    session.draft.visit_time = "2026-06-02 10:00"

    with patch("app.services.rag.search_fault", new_callable=AsyncMock) as mock_rag, \
         patch("app.services.llm.generate_confirmation_stream") as mock_confirm:

        mock_rag.return_value = None

        async def mock_stream():
            yield "请确认以下信息..."

        mock_confirm.return_value = mock_stream()

        events = []
        async for event in process_message(session, "跳过", None):
            events.append(event)

        assert session.state == AgentState.CONFIRMING


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
