from __future__ import annotations

import operator
from typing import Annotated, Any

from typing_extensions import TypedDict

from app.agent.state import Session


class GraphState(TypedDict):
    session: Session
    user_message: str
    image_url: str | None
    # SSE 事件累积通道，各节点 append，reducer 合并
    events: Annotated[list[dict[str, Any]], operator.add]
    # 节点间传递的临时标志，用于条件路由
    _extraction: dict[str, Any]
    _proceed_to_rag: bool
    _intent: str
    _need_rerag: bool
