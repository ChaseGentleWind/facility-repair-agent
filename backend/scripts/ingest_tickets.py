"""
数据入库入口脚本。
用法:
    python -m scripts.ingest_tickets              # 完整流程（清洗 + 入库）
    python -m scripts.ingest_tickets --skip-clean # 跳过清洗，直接从 cleaned JSON 入库
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from scripts.cleaner import clean_csv
from scripts.embedder import embed_and_store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
CSV_PATH = BASE_DIR / "data" / "CIP7867.csv"
CLEANED_PATH = BASE_DIR / "data" / "cleaned" / "cleaned_tickets.json"


async def run_clean():
    if not settings.deepseek_api_key:
        logger.error("DEEPSEEK_API_KEY 未配置，请在 .env 中设置")
        sys.exit(1)

    await clean_csv(
        csv_path=CSV_PATH,
        output_path=CLEANED_PATH,
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        concurrency=15,
    )


def run_embed():
    if not CLEANED_PATH.exists():
        logger.error("清洗结果文件不存在: %s", CLEANED_PATH)
        logger.error("请先运行完整流程（不带 --skip-clean）")
        sys.exit(1)

    count = embed_and_store(
        cleaned_path=CLEANED_PATH,
        chroma_dir=settings.chroma_persist_dir,
        embedding_model_path=settings.embedding_model_path,
    )
    logger.info("总计入库 %d 条记录", count)


def main():
    parser = argparse.ArgumentParser(description="历史工单数据入库")
    parser.add_argument("--skip-clean", action="store_true", help="跳过清洗步骤，直接从 cleaned JSON 入库")
    args = parser.parse_args()

    if not args.skip_clean:
        logger.info("=== 第一步：数据清洗 ===")
        asyncio.run(run_clean())

    logger.info("=== 第二步：去重聚合 + 向量化入库 ===")
    run_embed()

    logger.info("=== 全部完成 ===")


if __name__ == "__main__":
    main()
