from fastapi import APIRouter

from app.api.v1.chat import router as chat_router
from app.api.v1.upload import router as upload_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(chat_router)
api_router.include_router(upload_router)
