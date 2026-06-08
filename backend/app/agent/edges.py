from __future__ import annotations

from app.agent.graph_state import GraphState
from app.agent.state import AgentState

# 节点名常量
NODE_ENTRY = "entry_router"
NODE_COLLECT_EXTRACT = "collect_extract"
NODE_COLLECT_DECIDE = "collect_decide"
NODE_ASK_IMAGE = "ask_image"
NODE_WAIT_IMAGE = "wait_image"
NODE_RAG_CONFIRM = "rag_and_confirm"
NODE_CONFIRMING = "confirming"
NODE_RE_CONFIRM = "re_confirm"
NODE_PREVIEW_EDIT = "preview_edit"
NODE_STREAM_REPLY = "stream_reply"
NODE_ESCALATED = "escalated"
NODE_SUBMITTED = "submitted"
NODE_COMPLETED = "completed"
NODE_FINALIZE = "finalize"


def route_by_state(state: GraphState) -> str:
    """entry_router 后根据 session.state 分派到对应节点"""
    s = state["session"].state
    if s == AgentState.COLLECTING:
        return NODE_COLLECT_EXTRACT
    elif s == AgentState.WAITING_IMAGE:
        return NODE_WAIT_IMAGE
    elif s == AgentState.CONFIRMING:
        return NODE_CONFIRMING
    elif s == AgentState.PREVIEW_READY:
        return NODE_PREVIEW_EDIT
    elif s == AgentState.ESCALATED:
        return NODE_ESCALATED
    elif s == AgentState.SUBMITTED:
        return NODE_SUBMITTED
    elif s == AgentState.COMPLETED:
        return NODE_COMPLETED
    else:
        return NODE_FINALIZE


def after_collect_extract(state: GraphState) -> str:
    """collect_extract 后根据提取结果决定下一步"""
    session = state["session"]
    extraction = state.get("_extraction", {})

    if extraction.get("_error"):
        return NODE_FINALIZE

    if extraction.get("needs_human"):
        return NODE_ESCALATED

    if extraction.get("clarification_question"):
        return NODE_FINALIZE

    missing = session.draft.missing_required()
    if missing:
        return NODE_COLLECT_DECIDE

    return NODE_RAG_CONFIRM


def after_collect_decide(state: GraphState) -> str:
    """collect_decide 后：stall → escalated，否则 → stream_reply"""
    session = state["session"]
    if session.state == AgentState.ESCALATED:
        return NODE_ESCALATED
    return NODE_STREAM_REPLY


def after_wait_image(state: GraphState) -> str:
    """wait_image 后：收到图或跳过 → rag_confirm，否则 → finalize"""
    if state.get("_proceed_to_rag"):
        return NODE_RAG_CONFIRM
    return NODE_FINALIZE


def after_confirming(state: GraphState) -> str:
    """confirming 后路由：
    - confirmed / restart / unclear → finalize
    - modify + need_rerag → rag_and_confirm
    - modify + no_rerag → re_confirm（跳过 RAG 直接重生成确认摘要）
    """
    intent = state.get("_intent")
    if intent == "modify":
        if state.get("_need_rerag"):
            return NODE_RAG_CONFIRM
        return NODE_RE_CONFIRM
    return NODE_FINALIZE


def after_preview_edit(state: GraphState) -> str:
    """preview_edit 后：description/image 变 → rag_confirm，否则 → finalize"""
    if state.get("_need_rerag"):
        return NODE_RAG_CONFIRM
    return NODE_FINALIZE
