"""Загрузка медиа-вложений MAX на диск (в ``MEDIA_DIR/inbox/<kind>/``).

Используется из ``_on_message`` в ``bridge.py``: для каждого вложения
(``PhotoAttachment`` / ``VideoAttachment`` / ``FileAttachment``) вызывается
соответствующий ``process_*``, который скачивает файл и возвращает
``dict`` с полями ``kind/media_path/media_mime/media_filename/media_size``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

from pymax.types.domain.attachments import (
    FileAttachment,
    PhotoAttachment,
    VideoAttachment,
)

logger = logging.getLogger(__name__)


# Лимит на размер скачиваемого файла. Совпадает с TG-лимитом (49 МБ).
# Больше у MAX всё равно не бывает — просто обрезаем, чтобы TG смог переслать.
MAX_MEDIA_DOWNLOAD_BYTES = 49 * 1024 * 1024


def _media_subdir(kind: str) -> str:
    """``inbox/<kind>/`` относительно ``MEDIA_DIR``."""
    return os.path.join(MEDIA_DIR, "inbox", kind)


def _safe_filename(name: str) -> str:
    """Заменяет опасные символы в имени файла на подчёркивания."""
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in (name or "file"))[:120]


async def _download_to_file(url: str, dest_path: str) -> int:
    """Скачивает файл по ``url`` в ``dest_path``. Возвращает размер в байтах."""
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as c:
        async with c.stream("GET", url) as r:
            r.raise_for_status()
            written = 0
            with open(dest_path, "wb") as f:
                async for chunk in r.aiter_bytes(64 * 1024):
                    f.write(chunk)
                    written += len(chunk)
                    if written > MAX_MEDIA_DOWNLOAD_BYTES:
                        logger.warning("file too large, truncated at %d bytes", written)
                        break
            return written


# Константа ``MEDIA_DIR`` импортируется из окружения (см. ``max/run.py``).
import os as _os  # noqa: E402
MEDIA_DIR = _os.getenv("MEDIA_DIR", "/data/media")


async def process_photo(
    att: PhotoAttachment, chat_id: str, msg_id: str
) -> Optional[dict]:
    """Скачивает фото (если не превью) и возвращает ``media dict`` для EventIn."""
    # У PhotoAttachment уже есть base_url + photo_token
    url = f"{att.base_url.rstrip('/')}/{att.photo_token}"
    fname = _safe_filename(f"{chat_id}_{msg_id}_{att.photo_id}.jpg")
    rel = f"inbox/photo/{fname}"
    abs_path = os.path.join(MEDIA_DIR, rel)
    try:
        size = await _download_to_file(url, abs_path)
    except Exception as exc:
        logger.warning("photo download failed chat=%s msg=%s: %s", chat_id, msg_id, exc)
        return None
    return {
        "kind": "photo",
        "media_path": rel,
        "media_mime": "image/jpeg",
        "media_filename": fname,
        "media_size": size,
    }


async def process_video(
    att: VideoAttachment, client: Any, chat_id: int, msg_id: str
) -> Optional[dict]:
    """Скачивает видео через ``client.get_video_by_id``."""
    try:
        vreq = await client.get_video_by_id(chat_id, msg_id, att.video_id)
    except Exception as exc:
        logger.warning("get_video_by_id failed chat=%s msg=%s: %s", chat_id, msg_id, exc)
        return None
    if not vreq or not getattr(vreq, "url", None):
        logger.info("video url missing chat=%s msg=%s", chat_id, msg_id)
        return None
    url = vreq.url
    ext = "mp4"
    fname = _safe_filename(f"{chat_id}_{msg_id}_{att.video_id}.{ext}")
    rel = f"inbox/video/{fname}"
    abs_path = os.path.join(MEDIA_DIR, rel)
    try:
        size = await _download_to_file(url, abs_path)
    except Exception as exc:
        logger.warning("video download failed chat=%s msg=%s: %s", chat_id, msg_id, exc)
        return None
    return {
        "kind": "video",
        "media_path": rel,
        "media_mime": "video/mp4",
        "media_filename": fname,
        "media_size": size,
    }


async def process_file(
    att: FileAttachment, client: Any, chat_id: int, msg_id: str
) -> Optional[dict]:
    """Скачивает файл через ``client.get_file_by_id``."""
    try:
        freq = await client.get_file_by_id(chat_id, msg_id, att.file_id)
    except Exception as exc:
        logger.warning("get_file_by_id failed chat=%s msg=%s: %s", chat_id, msg_id, exc)
        return None
    if not freq or not getattr(freq, "url", None):
        logger.info("file url missing chat=%s msg=%s", chat_id, msg_id)
        return None
    url = freq.url
    fname = _safe_filename(att.name or f"file_{att.file_id}")
    rel = f"inbox/file/{fname}"
    abs_path = os.path.join(MEDIA_DIR, rel)
    try:
        size = await _download_to_file(url, abs_path)
    except Exception as exc:
        logger.warning("file download failed chat=%s msg=%s: %s", chat_id, msg_id, exc)
        return None
    return {
        "kind": "document",
        "media_path": rel,
        "media_mime": None,
        "media_filename": fname,
        "media_size": size,
    }