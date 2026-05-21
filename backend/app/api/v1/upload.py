"""图片上传接口：POST /api/v1/upload/image"""
from __future__ import annotations

import logging

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.agent.state import get_session
from app.models.api_models import UploadResponse
from app.services.storage import ALLOWED_CONTENT_TYPES, MAX_FILE_SIZE, upload_image

router = APIRouter(prefix="/upload", tags=["upload"])
logger = logging.getLogger(__name__)


@router.post("/image", response_model=UploadResponse)
async def upload_image_endpoint(
    session_id: str = Form(...),
    file: UploadFile = File(...),
) -> UploadResponse:
    session = get_session(session_id)
    if session is None:
        raise HTTPException(status_code=400, detail="session_id 无效或已过期")

    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型 {file.content_type}，仅支持 JPEG/PNG/WebP",
        )

    data = await file.read()
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="文件超过 10MB 限制")

    try:
        image_url, file_size = upload_image(data, file.content_type, session_id)
    except Exception as exc:
        logger.exception("MinIO upload failed: %s", exc)
        raise HTTPException(status_code=500, detail="图片上传失败，请稍后重试")

    return UploadResponse(image_url=image_url, file_size=file_size)
