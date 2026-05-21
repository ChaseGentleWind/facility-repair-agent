"""MinIO 对象存储服务：上传图片、生成签名 URL。"""
from __future__ import annotations

import io
import logging
import uuid
from datetime import datetime, timedelta

from minio import Minio
from minio.error import S3Error

from app.config import settings

logger = logging.getLogger(__name__)

_client: Minio | None = None

ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
PRESIGNED_URL_TTL = timedelta(hours=72)


def _get_client() -> Minio:
    global _client
    if _client is None:
        _client = Minio(
            endpoint=settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        _ensure_bucket()
    return _client


def _ensure_bucket() -> None:
    assert _client is not None
    bucket = settings.minio_bucket
    if not _client.bucket_exists(bucket):
        _client.make_bucket(bucket)
        logger.info("Created MinIO bucket: %s", bucket)


def _build_object_name(session_id: str, content_type: str) -> str:
    now = datetime.now()
    ext = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }.get(content_type, ".jpg")
    short_session = session_id[:8]
    short_uuid = uuid.uuid4().hex[:8]
    return f"{now:%Y/%m/%d}/{short_session}_{short_uuid}{ext}"


def upload_image(
    data: bytes,
    content_type: str,
    session_id: str,
) -> tuple[str, int]:
    """上传图片到 MinIO，返回 (访问 URL, 文件大小)。"""
    client = _get_client()
    object_name = _build_object_name(session_id, content_type)
    size = len(data)

    client.put_object(
        bucket_name=settings.minio_bucket,
        object_name=object_name,
        data=io.BytesIO(data),
        length=size,
        content_type=content_type,
    )
    logger.info("Uploaded %s (%d bytes)", object_name, size)

    if settings.minio_public_base:
        url = f"{settings.minio_public_base.rstrip('/')}/{object_name}"
    else:
        url = client.presigned_get_object(
            settings.minio_bucket, object_name, expires=PRESIGNED_URL_TTL
        )

    return url, size
