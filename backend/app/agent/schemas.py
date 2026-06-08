"""Agent 层结构化输出契约（Pydantic v2）。

所有 LLM 结构化调用都应返回这里的模型，由 LLMClient.structured_call 统一校验，
节点层面拿到的就是已校验的对象，不再处理裸 dict。

设计原则：
- 每个模型对应一种 LLM 调用语义（图片分析 / 文本字段提取 / 确认意图分类等）
- ambiguous_fields 单独承载"歧义字段名"，修复 prompts 中"猜测 vs null"规则冲突：
  歧义字段进入列表，clarification_question 仅在列表非空时设置
- ErrorResult 是所有结构化调用的失败兜底，节点层判断 isinstance 即可降级
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class VisualFields(BaseModel):
    """从图片中观测到的结构化字段，每个均为可选。"""

    model_config = ConfigDict(extra="ignore")

    description: str | None = None
    building: str | None = None
    floor: str | None = None
    area: str | None = None
    room: str | None = None


class ImageAnalysis(BaseModel):
    """单次 VLM 调用的完整产出，按 image_url 缓存到 Session.image_analysis。

    visual_description 给用户看（自然语言 2-3 句）；
    visual_fault_summary 给 RAG 检索用（≤20 字、剔除位置信息的故障摘要）；
    visual_fields 是图片中观察到的位置/描述字段，供 merge_extraction 合并。
    """

    model_config = ConfigDict(extra="ignore")

    image_url: str
    visual_description: str
    visual_fault_summary: str
    visual_fields: VisualFields = Field(default_factory=VisualFields)
    visual_confidence: Literal["high", "medium", "low"] = "medium"
    is_unclear: bool = False


class TextExtraction(BaseModel):
    """从用户文本中提取的字段，不包含图片观察结果。

    user_confirmed_description_priority: 用户表态"以我的描述为准"，触发 RAG ignore_image。
    ambiguous_fields: LLM 不确定归属的字段名列表（如 ["building", "floor"]），
                      非空时必须配套 clarification_question。
    """

    model_config = ConfigDict(extra="ignore")

    description: str | None = None
    estate: str | None = None
    building: str | None = None
    floor: str | None = None
    area: str | None = None
    room: str | None = None
    visit_time_text: str | None = None
    needs_human: bool = False
    user_confirmed_description_priority: bool = False
    clarification_question: str | None = None
    ambiguous_fields: list[str] = Field(default_factory=list)


class ConfirmationIntent(BaseModel):
    """confirming / preview_edit 一次调用即返回意图与修改字段。

    替代旧链路 check_user_confirmed → classify_denial_intent → extract_fields_editing 三连发。
    intent=modify 时 modified_fields 必填；其他意图时可省。
    """

    model_config = ConfigDict(extra="ignore")

    intent: Literal["confirm", "modify", "restart", "unclear"]
    modified_fields: TextExtraction | None = None


class ErrorResult(BaseModel):
    """LLMClient.structured_call 失败兜底。节点判断 isinstance 后降级"系统繁忙"。"""

    model_config = ConfigDict(extra="ignore")

    code: Literal["llm_call_failed", "llm_invalid_json", "llm_validation_failed"]
    message: str = ""
    purpose: str = ""
