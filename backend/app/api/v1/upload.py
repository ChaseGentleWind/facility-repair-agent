"""图片上传接口：POST /api/v1/upload/image"""
from __future__ import annotations

import logging

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.agent.state import get_session, refresh_session
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

    refresh_session(session)

    content_type = file.content_type or "image/jpeg"
    if content_type == "image/jpg":
        content_type = "image/jpeg"
    if content_type in ("image/heic", "image/heif"):
        content_type = "image/jpeg"
    logger.info("upload: session_id=%s content_type=%s filename=%s", session_id, content_type, file.filename)
    if content_type not in ALLOWED_CONTENT_TYPES:
        logger.warning("upload rejected: content_type=%r not in allowed list", file.content_type)
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型 {content_type}，仅支持 JPEG/PNG/WebP",
        )

    data = await file.read()
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="文件超过 10MB 限制")

    try:
        image_url, file_size = upload_image(data, content_type, session_id)
    except Exception as exc:
        logger.exception("Image upload failed: %s", exc)
        raise HTTPException(status_code=500, detail="图片上传失败，请稍后重试")

    return UploadResponse(image_url=image_url, file_size=file_size)
