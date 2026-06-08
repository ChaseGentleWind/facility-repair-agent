"""运行时 RAG 检索：标准化用户描述 → 向量化 → ChromaDB 检索 → fault_type + priority。

简化点（refactor/agent-architecture）：
- 不再持有 VLM：图片观察由 services/llm.py::analyze_image 统一负责
- 不再做图文冲突判断：统一在 agent/draft_ops.py::merge_extraction 裁决
- 仅保留 _normalize_description（用户决策：质量优先），改走 llm_client（拿到 timeout/重试/计数）
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

from app.agent.prompts import NORMALIZE_SYSTEM
from app.config import settings
from app.services import llm_client

logger = logging.getLogger(__name__)


@dataclass
class RagResult:
    fault_type_code: str
    fault_type_name: str
    repair_priority: str
    repair_type: str
    normalized_description: str
    match_score: float
    confidence: str  # high / medium / low


# 延迟加载，避免启动时就加载大模型
_embedding_model = None
_chroma_collection = None

_OBJECT_TERMS = {
    "空调",
    "出风口",
    "天花板",
    "吊顶",
    "灯",
    "灯具",
    "水龙头",
    "马桶",
    "门锁",
    "门",
    "电梯",
    "地面",
    "墙面",
    "管道",
    "插座",
    "开关",
    "玻璃",
    "窗",
    "下水道",
    "排水",
    "卫生间",
    "厕所",
    "消防",
    "门禁",
}
_FAULT_TERMS = {
    "漏水",
    "渗漏",
    "水渍",
    "不制冷",
    "不冷",
    "不亮",
    "损坏",
    "破损",
    "堵塞",
    "堵",
    "异响",
    "打不开",
    "无法打开",
    "坏",
    "脱落",
    "裂",
    "烧焦",
    "冒烟",
    "停电",
}
_FAULT_GROUPS = [
    {"漏水", "渗漏", "水渍"},
    {"不制冷", "不冷"},
    {"不亮", "停电"},
    {"堵塞", "堵"},
    {"损坏", "破损", "坏", "脱落", "裂"},
    {"打不开", "无法打开"},
    {"烧焦", "冒烟"},
]


def _get_embedding_model():
    global _embedding_model
    if _embedding_model is None:
        from sentence_transformers import SentenceTransformer
        logger.info("加载 Embedding 模型: %s", settings.embedding_model_path)
        _embedding_model = SentenceTransformer(settings.embedding_model_path)
    return _embedding_model


def _get_collection():
    global _chroma_collection
    if _chroma_collection is None:
        import chromadb
        chroma_dir = settings.chroma_persist_dir
        if not Path(chroma_dir).exists():
            logger.warning("ChromaDB 目录不存在: %s, RAG 检索将不可用", chroma_dir)
            return None
        client = chromadb.PersistentClient(path=chroma_dir)
        try:
            _chroma_collection = client.get_collection("historical_tickets")
        except Exception:
            logger.warning("ChromaDB collection 'historical_tickets' 不存在，RAG 检索将不可用")
            return None
    return _chroma_collection


async def _normalize_description(description: str) -> str:
    """用 Qwen 将描述标准化为'物理实体+故障现象'。失败返回原描述。"""
    raw = await llm_client.text_call(
        purpose="rag_normalize",
        system=NORMALIZE_SYSTEM,
        user=description,
        temperature=0.1,
        max_tokens=50,
    )
    if raw:
        normalized = raw.strip()
        if normalized:
            return normalized
    return description


def _terms_in(text: str, terms: set[str]) -> set[str]:
    return {term for term in terms if term in text}


def _content_chars(text: str) -> set[str]:
    return set(re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text.lower()))


def _has_same_fault_group(left: str, right: str) -> bool:
    for group in _FAULT_GROUPS:
        if _terms_in(left, group) and _terms_in(right, group):
            return True
    return False


def is_visual_summary_relevant(description: str, visual_fault_summary: str | None) -> bool:
    """判断图片故障摘要是否可参与描述规范化/RAG。

    规则偏保守：用户文字始终是主事实；当文字和图片明显不是同一对象/现象时，
    图片仍作为附件保存，但不拼入 RAG 查询。
    """
    if not description or not visual_fault_summary:
        return False

    desc = description.strip().lower()
    summary = visual_fault_summary.strip().lower()
    if not desc or not summary or summary == "无法识别":
        return False

    desc_objects = _terms_in(desc, _OBJECT_TERMS)
    summary_objects = _terms_in(summary, _OBJECT_TERMS)
    if desc_objects and summary_objects:
        return bool(desc_objects & summary_objects)

    if desc_objects or summary_objects:
        if desc_objects & summary_objects:
            return True
        # 一边有明确对象、一边没有对象时，可以用故障现象判断是否辅助。
        return bool(_terms_in(desc, _FAULT_TERMS) & _terms_in(summary, _FAULT_TERMS)) or _has_same_fault_group(desc, summary)

    desc_faults = _terms_in(desc, _FAULT_TERMS)
    summary_faults = _terms_in(summary, _FAULT_TERMS)
    if desc_faults or summary_faults:
        return bool(desc_faults & summary_faults) or _has_same_fault_group(desc, summary)

    desc_chars = _content_chars(desc)
    summary_chars = _content_chars(summary)
    if not desc_chars or not summary_chars:
        return False
    return len(desc_chars & summary_chars) / min(len(desc_chars), len(summary_chars)) >= 0.5


async def search_fault(
    description: str,
    visual_fault_summary: str | None = None,
    *,
    ignore_image: bool = False,
) -> RagResult | None:
    """根据用户报修描述检索历史工单。

    Args:
        description: 用户文字报修描述（可能为空）
        visual_fault_summary: 由 analyze_image 产出的 ≤20 字图片故障摘要，传入即可拼接
        ignore_image: True 时即使传入了 visual_fault_summary 也忽略（用户已表态以描述为准）

    冲突判断已上移到节点层 merge_extraction；本函数只决定"用不用图"。
    """
    collection = _get_collection()
    if collection is None:
        return None

    query_text = description or ""
    if visual_fault_summary and not ignore_image and is_visual_summary_relevant(description, visual_fault_summary):
        if query_text:
            query_text = f"{query_text}；{visual_fault_summary}"
        else:
            query_text = visual_fault_summary
        logger.info("RAG 查询拼接图片摘要: '%s'", query_text)
    elif visual_fault_summary and not ignore_image:
        logger.info(
            "RAG 查询跳过不相关图片摘要: description='%s', visual='%s'",
            description,
            visual_fault_summary,
        )
    elif ignore_image and visual_fault_summary:
        logger.info("RAG 查询（用户已确认以描述为准）: 只使用描述 '%s'", description)

    if not query_text:
        return None

    normalized = await _normalize_description(query_text)
    logger.info("标准化描述: '%s' → '%s'", query_text, normalized)

    model = _get_embedding_model()
    embedding = model.encode([normalized], normalize_embeddings=True).tolist()

    results = collection.query(
        query_embeddings=embedding,
        n_results=3,
    )

    if not results["metadatas"] or not results["metadatas"][0]:
        return None

    top_meta = results["metadatas"][0][0]
    distance = results["distances"][0][0]
    score = 1.0 - distance

    if score < 0.3:
        return None

    if score > 0.85:
        confidence = "high"
    elif score > 0.65:
        confidence = "medium"
    else:
        confidence = "low"

    return RagResult(
        fault_type_code=top_meta["fault_type_code"],
        fault_type_name=top_meta["fault_type_name"],
        repair_priority=top_meta["repair_priority"],
        repair_type=top_meta.get("repair_type", ""),
        normalized_description=normalized,
        match_score=round(score, 4),
        confidence=confidence,
    )
