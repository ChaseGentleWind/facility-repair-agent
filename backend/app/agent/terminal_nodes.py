from __future__ import annotations

from app.agent.graph_state import GraphState
from app.agent.state import AgentState


def escalated(state: GraphState) -> dict:
    session = state["session"]
    if session.state != AgentState.ESCALATED:
        session.state = AgentState.ESCALATED
    return {"events": [{"type": "text_delta", "content": "已为您转接人工客服，如有其他问题请刷新页面重新发起。"}]}


def submitted(state: GraphState) -> dict:
    return {"events": [{"type": "text_delta", "content": "工单已提交，如有其他问题请刷新页面重新发起。"}]}


def completed(state: GraphState) -> dict:
    return {"events": [{"type": "text_delta", "content": "报修单已提交，如有其他问题请刷新页面重新发起。"}]}


def finalize(state: GraphState) -> dict:
    return {}
