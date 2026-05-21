"""
运行时 RAG 检索服务：标准化用户描述 → 向量化 → ChromaDB 检索 → 返回 fault_type + priority。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

from openai import AsyncOpenAI

from app.agent.prompts import NORMALIZE_SYSTEM
from app.config import settings

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
    """用 Qwen 将用户描述标准化为"物理实体+故障现象"格式。"""
    client = AsyncOpenAI(api_key=settings.qwen_api_key, base_url=settings.qwen_base_url)
    try:
        resp = await client.chat.completions.create(
            model=settings.qwen_model,
            messages=[
                {"role": "system", "content": NORMALIZE_SYSTEM},
                {"role": "user", "content": description},
            ],
            max_tokens=50,
            temperature=0.1,
            extra_body={"enable_thinking": False},
        )
        normalized = resp.choices[0].message.content.strip()
        return normalized if normalized else description
    except Exception as exc:
        logger.warning("标准化描述失败: %s, 使用原始描述", exc)
        return description


async def search_fault(description: str) -> RagResult | None:
    """
    根据用户报修描述检索历史工单，返回最匹配的 fault_type 和 priority。
    如果 ChromaDB 未初始化或无结果，返回 None。
    """
    collection = _get_collection()
    if collection is None:
        return None

    normalized = await _normalize_description(description)
    logger.info("标准化描述: '%s' → '%s'", description, normalized)

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
    # ChromaDB cosine distance = 1 - cosine_similarity
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
