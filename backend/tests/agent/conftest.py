"""测试夹具：用 monkeypatch 把 llm_client 的对外入口替换为可计数 mock。

提供：
- llm_call_counter：fixture，返回 dict 形式的"按 purpose 计数 + 调用记录"
- make_image_analysis / make_text_extraction：构造 schema 实例的工厂
- 默认每个 purpose 的 mock 返回值，测试可以自由覆盖
"""
from __future__ import annotations

from collections import Counter
from typing import Any

import pytest

from app.agent.schemas import (
    ConfirmationIntent,
    ErrorResult,
    ImageAnalysis,
    TextExtraction,
    VisualFields,
)
from app.services.session_store import init_session_store
from app.services.ticket_counter import init_ticket_counter


@pytest.fixture(autouse=True)
def _setup_memory_store():
    init_session_store(backend="memory")
    init_ticket_counter(backend="memory", seed=1726198)


def make_image_analysis(
    image_url: str = "/uploads/test.jpg",
    *,
    visual_description: str = "我看到天花板有水渍渗漏。",
    visual_fault_summary: str = "天花板水渍渗漏",
    visual_fields: dict[str, Any] | None = None,
    visual_confidence: str = "medium",
    is_unclear: bool = False,
) -> ImageAnalysis:
    fields = VisualFields(**(visual_fields or {}))
    return ImageAnalysis(
        image_url=image_url,
        visual_description=visual_description,
        visual_fault_summary=visual_fault_summary,
        visual_fields=fields,
        visual_confidence=visual_confidence,
        is_unclear=is_unclear,
    )


def make_text_extraction(**kwargs: Any) -> TextExtraction:
    return TextExtraction(**kwargs)


class _CallTracker:
    """记录每次 LLMClient 调用：bucket = Counter(purpose -> count)。"""

    def __init__(self):
        self.bucket: Counter[str] = Counter()
        self.responses: dict[str, Any] = {}

    def set(self, purpose: str, value: Any) -> None:
        self.responses[purpose] = value

    def hits(self, purpose: str) -> int:
        return self.bucket[purpose]

    def total(self) -> int:
        return sum(self.bucket.values())


@pytest.fixture
def llm_call_counter(monkeypatch) -> _CallTracker:
    tracker = _CallTracker()

    async def fake_structured_call(schema, *, purpose: str, system, user, **kwargs):
        tracker.bucket[purpose] += 1
        if purpose in tracker.responses:
            return tracker.responses[purpose]
        # 默认值：每个 schema 给一个稳定的"什么都没说"的实例
        if schema is TextExtraction:
            return TextExtraction()
        if schema is ImageAnalysis:
            return make_image_analysis(image_url="/uploads/test.jpg")
        if schema is ConfirmationIntent:
            return ConfirmationIntent(intent="unclear")
        return ErrorResult(code="llm_call_failed", purpose=purpose)

    async def fake_text_call(*, purpose: str, system, user, **kwargs):
        tracker.bucket[purpose] += 1
        return tracker.responses.get(purpose, "")

    async def fake_stream_call(*, purpose: str, messages, **kwargs):
        tracker.bucket[purpose] += 1
        chunks = tracker.responses.get(purpose, ["请补充信息。"])
        for chunk in chunks:
            yield chunk

    # Mock 图片编码，避免读取本地文件
    def fake_encode_image(image_url: str) -> str | None:
        return "data:image/jpeg;base64,fake_base64_data"

    monkeypatch.setattr("app.services.llm_client.structured_call", fake_structured_call)
    monkeypatch.setattr("app.services.llm_client.text_call", fake_text_call)
    monkeypatch.setattr("app.services.llm_client.stream_call", fake_stream_call)
    monkeypatch.setattr("app.services.llm._encode_image_to_data_uri", fake_encode_image)
    return tracker
