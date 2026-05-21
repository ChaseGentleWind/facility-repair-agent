"""
去重聚合 + 向量化入库模块：
读取 cleaned_tickets.json → GroupBy normalized_text → bge 向量化 → 写入 ChromaDB。
"""
from __future__ import annotations

import json
import logging
import os
from collections import Counter
from pathlib import Path
from typing import Any

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import chromadb
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)


def _aggregate(records: list[dict]) -> list[dict[str, Any]]:
    """按 normalized_text 去重聚合，取 fault_type 和 priority 的众数。"""
    groups: dict[str, list[dict]] = {}
    for r in records:
        key = r["normalized_text"]
        groups.setdefault(key, []).append(r)

    aggregated: list[dict[str, Any]] = []
    for text, items in groups.items():
        ft_counter = Counter((it["fault_type_code"], it["fault_type_name"]) for it in items)
        priority_counter = Counter(it["repair_priority"] for it in items)
        repair_type_counter = Counter(it.get("repair_type", "") for it in items)

        top_ft = ft_counter.most_common(1)[0][0]
        top_priority = priority_counter.most_common(1)[0][0]
        top_repair_type = repair_type_counter.most_common(1)[0][0]

        aggregated.append({
            "normalized_text": text,
            "fault_type_code": top_ft[0],
            "fault_type_name": top_ft[1],
            "repair_priority": top_priority,
            "repair_type": top_repair_type,
            "weight": len(items),
        })

    logger.info("去重聚合: %d 条 → %d 条唯一描述", len(records), len(aggregated))
    return aggregated


def embed_and_store(
    cleaned_path: Path,
    chroma_dir: str,
    embedding_model_path: str,
    collection_name: str = "historical_tickets",
    batch_size: int = 64,
) -> int:
    """
    读取清洗结果，去重聚合，向量化后写入 ChromaDB。
    返回入库条数。
    """
    with open(cleaned_path, "r", encoding="utf-8") as f:
        records = json.load(f)

    if not records:
        logger.warning("无有效记录可入库")
        return 0

    aggregated = _aggregate(records)

    logger.info("加载 Embedding 模型: %s", embedding_model_path)
    model = SentenceTransformer(embedding_model_path)

    texts = [item["normalized_text"] for item in aggregated]
    logger.info("向量化 %d 条文本...", len(texts))
    embeddings = model.encode(texts, show_progress_bar=True, normalize_embeddings=True)

    logger.info("写入 ChromaDB: %s", chroma_dir)
    client = chromadb.PersistentClient(path=chroma_dir)
    collection = client.get_or_create_collection(
        name=collection_name,
        metadata={"hnsw:space": "cosine"},
    )

    # 清空旧数据后重新写入
    existing = collection.count()
    if existing > 0:
        logger.info("清空旧数据 (%d 条)...", existing)
        collection.delete(where={"weight": {"$gte": 0}})

    # 分批 upsert
    for i in range(0, len(aggregated), batch_size):
        batch = aggregated[i:i + batch_size]
        batch_embeddings = embeddings[i:i + batch_size].tolist()
        ids = [f"ticket_{i + j}" for j in range(len(batch))]
        documents = [item["normalized_text"] for item in batch]
        metadatas = [
            {
                "fault_type_code": item["fault_type_code"],
                "fault_type_name": item["fault_type_name"],
                "repair_priority": item["repair_priority"],
                "repair_type": item["repair_type"],
                "weight": item["weight"],
                "normalized_text": item["normalized_text"],
            }
            for item in batch
        ]
        collection.upsert(
            ids=ids,
            embeddings=batch_embeddings,
            documents=documents,
            metadatas=metadatas,
        )

    total = collection.count()
    logger.info("入库完成: %d 条向量记录", total)
    return total
