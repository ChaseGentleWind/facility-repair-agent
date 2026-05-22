"""图片存储服务：本地文件系统（开发）或 MinIO（生产）。"""
from __future__ import annotations

import io
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic", "image/heif"}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
PRESIGNED_URL_TTL = timedelta(hours=72)

_EXT_MAP = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def _build_object_name(session_id: str, content_type: str) -> str:
    now = datetime.now()
    ext = _EXT_MAP.get(content_type, ".jpg")
    short_session = session_id[:8]
    short_uuid = uuid.uuid4().hex[:8]
    return f"{now:%Y/%m/%d}/{short_session}_{short_uuid}{ext}"


# ── 本地存储 ─────────────────────────────────────────────────────────────────

def _upload_local(data: bytes, content_type: str, session_id: str) -> tuple[str, int]:
    object_name = _build_object_name(session_id, content_type)
    dest = Path(settings.local_upload_dir) / object_name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    logger.info("Saved locally: %s (%d bytes)", dest, len(data))
    url = f"/uploads/{object_name}"
    return url, len(data)


# ── MinIO 存储 ────────────────────────────────────────────────────────────────

def _get_minio_client():
    from minio import Minio
    global _minio_client
    if _minio_client is None:
        _minio_client = Minio(
            endpoint=settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        bucket = settings.minio_bucket
        if not _minio_client.bucket_exists(bucket):
            _minio_client.make_bucket(bucket)
            logger.info("Created MinIO bucket: %s", bucket)
    return _minio_client

_minio_client = None


def _upload_minio(data: bytes, content_type: str, session_id: str) -> tuple[str, int]:
    client = _get_minio_client()
    object_name = _build_object_name(session_id, content_type)
    size = len(data)
    client.put_object(
        bucket_name=settings.minio_bucket,
        object_name=object_name,
        data=io.BytesIO(data),
        length=size,
        content_type=content_type,
    )
    logger.info("Uploaded to MinIO: %s (%d bytes)", object_name, size)
    if settings.minio_public_base:
        url = f"{settings.minio_public_base.rstrip('/')}/{object_name}"
    else:
        url = client.presigned_get_object(
            settings.minio_bucket, object_name, expires=PRESIGNED_URL_TTL
        )
    return url, size


# ── 公共入口 ──────────────────────────────────────────────────────────────────

def upload_image(data: bytes, content_type: str, session_id: str) -> tuple[str, int]:
    """上传图片，返回 (访问 URL, 文件大小)。本地开发用文件系统，生产用 MinIO。"""
    if settings.use_local_storage:
        return _upload_local(data, content_type, session_id)
    return _upload_minio(data, content_type, session_id)
