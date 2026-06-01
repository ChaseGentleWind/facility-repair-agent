from __future__ import annotations

import logging
from typing import AsyncIterator

from langgraph.graph import StateGraph, END

from app.agent import edges, nodes
from app.agent.graph_state import GraphState
from app.agent.state import Session

logger = logging.getLogger(__name__)


def build_graph() -> StateGraph:
    """构建 LangGraph 状态机"""
    graph = StateGraph(GraphState)

    # 添加节点
    graph.add_node(edges.NODE_ENTRY, nodes.entry_router)
    graph.add_node(edges.NODE_COLLECT_EXTRACT, nodes.collect_extract)
    graph.add_node(edges.NODE_COLLECT_DECIDE, nodes.collect_decide)
    graph.add_node(edges.NODE_ASK_IMAGE, nodes.ask_image)
    graph.add_node(edges.NODE_WAIT_IMAGE, nodes.wait_image)
    graph.add_node(edges.NODE_RAG_CONFIRM, nodes.rag_and_confirm)
    graph.add_node(edges.NODE_CONFIRMING, nodes.confirming)
    graph.add_node(edges.NODE_PREVIEW_EDIT, nodes.preview_edit)
    graph.add_node(edges.NODE_STREAM_REPLY, nodes.stream_reply)
    graph.add_node(edges.NODE_ESCALATED, nodes.escalated)
    graph.add_node(edges.NODE_SUBMITTED, nodes.submitted)
    graph.add_node(edges.NODE_COMPLETED, nodes.completed)
    graph.add_node(edges.NODE_FINALIZE, nodes.finalize)

    # 设置入口
    graph.set_entry_point(edges.NODE_ENTRY)

    # entry_router → 按 state 分派
    graph.add_conditional_edges(
        edges.NODE_ENTRY,
        edges.route_by_state,
        {
            edges.NODE_COLLECT_EXTRACT: edges.NODE_COLLECT_EXTRACT,
            edges.NODE_WAIT_IMAGE: edges.NODE_WAIT_IMAGE,
            edges.NODE_CONFIRMING: edges.NODE_CONFIRMING,
            edges.NODE_PREVIEW_EDIT: edges.NODE_PREVIEW_EDIT,
            edges.NODE_ESCALATED: edges.NODE_ESCALATED,
            edges.NODE_SUBMITTED: edges.NODE_SUBMITTED,
            edges.NODE_COMPLETED: edges.NODE_COMPLETED,
            edges.NODE_FINALIZE: edges.NODE_FINALIZE,
        }
    )

    # collect_extract → 根据提取结果分派
    graph.add_conditional_edges(
        edges.NODE_COLLECT_EXTRACT,
        edges.after_collect_extract,
        {
            edges.NODE_FINALIZE: edges.NODE_FINALIZE,
            edges.NODE_ESCALATED: edges.NODE_ESCALATED,
            edges.NODE_COLLECT_DECIDE: edges.NODE_COLLECT_DECIDE,
            edges.NODE_ASK_IMAGE: edges.NODE_ASK_IMAGE,
            edges.NODE_RAG_CONFIRM: edges.NODE_RAG_CONFIRM,
        }
    )

    # collect_decide → stall 或 stream_reply
    graph.add_conditional_edges(
        edges.NODE_COLLECT_DECIDE,
        edges.after_collect_decide,
        {
            edges.NODE_ESCALATED: edges.NODE_ESCALATED,
            edges.NODE_STREAM_REPLY: edges.NODE_STREAM_REPLY,
        }
    )

    # stream_reply → finalize
    graph.add_edge(edges.NODE_STREAM_REPLY, edges.NODE_FINALIZE)

    # ask_image → finalize
    graph.add_edge(edges.NODE_ASK_IMAGE, edges.NODE_FINALIZE)

    # wait_image → rag_confirm 或 finalize
    graph.add_conditional_edges(
        edges.NODE_WAIT_IMAGE,
        edges.after_wait_image,
        {
            edges.NODE_RAG_CONFIRM: edges.NODE_RAG_CONFIRM,
            edges.NODE_FINALIZE: edges.NODE_FINALIZE,
        }
    )

    # rag_and_confirm → finalize
    graph.add_edge(edges.NODE_RAG_CONFIRM, edges.NODE_FINALIZE)

    # confirming → confirmed/restart/modify
    graph.add_conditional_edges(
        edges.NODE_CONFIRMING,
        edges.after_confirming,
        {
            edges.NODE_FINALIZE: edges.NODE_FINALIZE,
            edges.NODE_COLLECT_EXTRACT: edges.NODE_COLLECT_EXTRACT,
        }
    )

    # preview_edit → rerag 或 finalize
    graph.add_conditional_edges(
        edges.NODE_PREVIEW_EDIT,
        edges.after_preview_edit,
        {
            edges.NODE_RAG_CONFIRM: edges.NODE_RAG_CONFIRM,
            edges.NODE_FINALIZE: edges.NODE_FINALIZE,
        }
    )

    # 终态节点 → finalize
    graph.add_edge(edges.NODE_ESCALATED, edges.NODE_FINALIZE)
    graph.add_edge(edges.NODE_SUBMITTED, edges.NODE_FINALIZE)
    graph.add_edge(edges.NODE_COMPLETED, edges.NODE_FINALIZE)

    # finalize → END
    graph.add_edge(edges.NODE_FINALIZE, END)

    return graph


compiled_graph = build_graph().compile()


async def process_message(
    session: Session,
    user_message: str,
    image_url: str | None = None,
) -> AsyncIterator[dict]:
    """
    兼容包装函数，保持与旧 agent/core.py 相同的签名。
    供 api/v1/chat.py 调用。
    """
    try:
        input_state = {
            "session": session,
            "user_message": user_message,
            "image_url": image_url,
            "events": [],
        }

        async for chunk in compiled_graph.astream(input_state, stream_mode="updates"):
            for node_name, node_output in chunk.items():
                if node_output and "events" in node_output:
                    for event in node_output["events"]:
                        yield event

    except Exception as exc:
        logger.exception("process_message error: %s", exc)
        yield {"type": "error", "code": "INTERNAL_ERROR", "message": "服务异常，请稍后重试"}

    yield {"type": "done"}
