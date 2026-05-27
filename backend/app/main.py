import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.v1 import api_router
from app.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理：启动时预热加载 RAG 模型，关闭时清理资源。"""
    # 启动时预热加载
    logger.info("🚀 开始预热加载 RAG 模型...")
    try:
        from app.services.rag import _get_embedding_model, _get_collection

        # 预加载 Embedding 模型（耗时 2-5 秒）
        logger.info("📦 加载 Embedding 模型: %s", settings.embedding_model_path)
        _get_embedding_model()
        logger.info("✅ Embedding 模型加载完成")

        # 预加载 ChromaDB Collection（耗时 0.5-1 秒）
        logger.info("📦 加载 ChromaDB Collection...")
        collection = _get_collection()
        if collection is not None:
            logger.info("✅ ChromaDB Collection 加载完成")
        else:
            logger.warning("⚠️  ChromaDB Collection 不可用，RAG 检索将被跳过")

        logger.info("🎉 RAG 模型预热完成，服务已就绪")
    except Exception as e:
        logger.error("❌ RAG 模型预热失败: %s", e)
        logger.warning("⚠️  服务将继续启动，但 RAG 功能可能不可用")

    yield  # 应用运行期间

    # 关闭时清理资源（可选）
    logger.info("🛑 应用关闭，清理资源...")


def create_app() -> FastAPI:
    app = FastAPI(
        title="设施报修 Agent",
        version="0.1.0",
        docs_url="/docs",
        lifespan=lifespan,  # 注册生命周期管理器
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router)

    if settings.use_local_storage:
        upload_dir = Path(settings.local_upload_dir)
        upload_dir.mkdir(parents=True, exist_ok=True)
        app.mount("/uploads", StaticFiles(directory=str(upload_dir)), name="uploads")

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


app = create_app()
