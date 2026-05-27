"""
运行时 RAG 检索服务：标准化用户描述 → 向量化 → ChromaDB 检索 → 返回 fault_type + priority。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

from openai import AsyncOpenAI

from app.agent.prompts import NORMALIZE_SYSTEM
from app.config import settings

logger = logging.getLogger(__name__)

# 图片故障视觉描述 Prompt
_IMAGE_DESCRIBE_SYSTEM = """\
你是设施报修图片分析助手。请仔细观察图片，用一句简洁的中文描述图片中可见的故障现象。
要求：
- 聚焦于故障实体和现象，如"天花板大面积水渍渗漏"、"空调出风口有黑色霉斑"、"墙面瓷砖大面积开裂脱落"
- 不超过20字
- 若图片模糊无法判断，输出：无法识别
只输出描述结果，不要解释。"""

# 图文语义冲突判断 Prompt
_CONFLICT_CHECK_SYSTEM = """\
判断以下两段报修信息是否描述的是同一个故障。

规则：
- 若两者指向不同故障类型（如用户说门锁问题，图片显示漏水；用户说灯坏了，图片显示墙面水渍），返回 conflict
- 若用户描述模糊（如"处理一下"、"修一下"、"看看"、"帮忙"）且图片提供了具体故障信息，返回 complement
- 若两者描述同一故障的不同方面（如用户说"空调不制冷"，图片显示"空调出风口结冰"；用户说"门关不上"，图片显示"门锁损坏"），返回 complement
- 若用户描述具体但图片显示不同设备或不同故障现象，返回 conflict

只输出 conflict 或 complement，不要任何解释。"""


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


async def _describe_image_fault(image_url: str) -> str | None:
    """用视觉模型提取图片中的故障现象描述，用于增强 RAG 检索文本。失败返回 None。"""
    from app.services.llm import _encode_image_to_data_uri
    data_uri = _encode_image_to_data_uri(image_url)
    if data_uri is None:
        return None

    client = AsyncOpenAI(api_key=settings.qwen_api_key, base_url=settings.qwen_base_url)
    try:
        resp = await client.chat.completions.create(
            model=settings.qwen_model,
            messages=[
                {"role": "system", "content": _IMAGE_DESCRIBE_SYSTEM},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "请描述图片中的故障现象。"},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                },
            ],
            max_tokens=60,
            temperature=0.1,
            extra_body={"enable_thinking": False},
        )
        result = resp.choices[0].message.content.strip()
        if result == "无法识别" or not result:
            return None
        logger.info("图片故障描述: %s", result)
        return result
    except Exception as exc:
        logger.warning("_describe_image_fault failed: %s", exc)
        return None


async def _check_semantic_conflict(description: str, visual_desc: str) -> bool:
    """判断用户描述与图片描述是否语义冲突。返回 True 表示冲突（应只用用户描述）。"""
    client = AsyncOpenAI(api_key=settings.qwen_api_key, base_url=settings.qwen_base_url)
    prompt = f"""用户描述：{description}
图片观察：{visual_desc}"""
    try:
        resp = await client.chat.completions.create(
            model=settings.qwen_model,
            messages=[
                {"role": "system", "content": _CONFLICT_CHECK_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            max_tokens=10,
            temperature=0,
            extra_body={"enable_thinking": False},
        )
        result = resp.choices[0].message.content.strip().lower()
        is_conflict = result == "conflict"
        logger.info("语义冲突检测: '%s' vs '%s' → %s", description, visual_desc, "冲突" if is_conflict else "互补")
        return is_conflict
    except Exception as exc:
        logger.warning("_check_semantic_conflict failed: %s, 默认视为互补", exc)
        return False  # 调用失败时保守处理，视为互补（保留原有拼接行为）


async def search_fault(description: str, image_url: str | None = None, ignore_image: bool = False) -> RagResult | None:
    """
    根据用户报修描述检索历史工单，返回最匹配的 fault_type 和 priority。
    若提供 image_url，将图片视觉描述与文字描述合并后再检索，提升匹配准确率。
    如果 ChromaDB 未初始化或无结果，返回 None。

    Args:
        description: 用户报修描述
        image_url: 图片 URL（可选）
        ignore_image: 是否忽略图片（即使提供了 image_url 也不拼接），用于用户明确要求"以描述为准"时
    """
    collection = _get_collection()
    if collection is None:
        return None

    # 图片描述增强：将视觉观察到的故障现象追加到文字描述中
    query_text = description or ""
    if image_url and not ignore_image:
        visual_desc = await _describe_image_fault(image_url)
        if visual_desc:
            # 语义冲突检测：判断用户描述与图片描述是否冲突
            if description and await _check_semantic_conflict(description, visual_desc):
                # 冲突：只用用户描述，忽略图片
                logger.info("检测到图文语义冲突，RAG 只使用用户描述: '%s'（忽略图片: '%s'）", description, visual_desc)
            else:
                # 互补或用户描述模糊：拼接增强
                query_text = f"{query_text}；{visual_desc}" if query_text else visual_desc
                logger.info("RAG 查询增强（图文互补）: '%s' + 图片 → '%s'", description, query_text)
        else:
            logger.info("图片描述提取失败，只使用用户描述: '%s'", description)
    elif ignore_image and image_url:
        logger.info("RAG 查询（用户已确认以描述为准）: 只使用描述 '%s'", description)

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
