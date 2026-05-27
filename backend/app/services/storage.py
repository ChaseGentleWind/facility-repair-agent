from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic", "image/heif"}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

_EXT_MAP = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def _build_object_name(session_id: str, content_type: str) -> str:
    now = datetime.now()
    ext = _EXT_MAP.get(content_type, ".jpg")
    short_session = session_id[:8]
    short_uuid = uuid.uuid4().hex[:8]
    return f"{now:%Y/%m/%d}/{short_session}_{short_uuid}{ext}"


def upload_image(data: bytes, content_type: str, session_id: str) -> tuple[str, int]:
    object_name = _build_object_name(session_id, content_type)
    dest = Path(settings.local_upload_dir) / object_name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    logger.info("Saved locally: %s (%d bytes)", dest, len(data))
    return f"/uploads/{object_name}", len(data)


def read_image_bytes(image_url: str) -> bytes | None:
    try:
        relative = image_url.lstrip("/")
        local_path = Path(settings.local_upload_dir).parent / relative
        if not local_path.exists():
            logger.warning("read_image_bytes: file not found: %s", local_path)
            return None
        return local_path.read_bytes()
    except Exception as exc:
        logger.warning("read_image_bytes failed for %s: %s", image_url, exc)
        return None
