"""Мост PyMax → api: слушаем события MAX, складываем в БД через api.

Регистрирует:
- @client.on_start() — auth_state = ok, fetch chats
- @client.on_message() — кладём Event в api + (опц.) ChatInfo
- @client.on_chat_update() — обновляем ChatInfo
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any, Optional

import httpx

from pymax.files import File as MaxFile
from pymax.files import Photo as MaxPhoto
from pymax.files import Video as MaxVideo
from pymax.types import Chat as MaxChat
from pymax.types import Message as MaxMessage
from pymax.types.domain.attachments import (
    FileAttachment,
    PhotoAttachment,
    VideoAttachment,
)

logger = logging.getLogger(__name__)


API_BASE = os.getenv("API_BASE_URL", "http://localhost:8000")
API_KEY = os.getenv("BRIDGE_API_KEY", "")
MEDIA_DIR = os.getenv("MEDIA_DIR", "/data/media")
MAX_MEDIA_DOWNLOAD_BYTES = 49 * 1024 * 1024  # совпадает с TG-лимитом


def _headers() -> dict:
    return {"X-Api-Key": API_KEY}


def _chat_to_dict(chat: MaxChat) -> dict:
    return {
        "max_chat_id": str(chat.id),
        "title": chat.title or "",
        "type": str(chat.type) if chat.type else "chat",
        "last_message_preview": (chat.last_message.text[:200] if chat.last_message and chat.last_message.text else None),
        "last_message_at": (
            datetime.utcfromtimestamp(chat.last_event_time / 1000).isoformat()
            if chat.last_event_time
            else None
        ),
        "unread": chat.new_messages if chat.new_messages is not None else None,
    }


async def _post(path: str, json: dict | None = None) -> None:
    try:
        async with httpx.AsyncClient(base_url=API_BASE, timeout=30.0) as c:
            r = await c.post(path, json=json, headers=_headers())
            r.raise_for_status()
    except Exception as exc:
        logger.warning("api POST %s failed: %s", path, exc)


async def _download_to_file(url: str, dest_path: str) -> int:
    """Скачивает файл по url в dest_path. Возвращает размер в байтах."""
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
                        # Слишком большой — обрезаем, чтобы TG смог переслать
                        logger.warning("file too large, truncated at %d bytes", written)
                        break
            return written


def _media_subdir(kind: str) -> str:
    return os.path.join(MEDIA_DIR, "inbox", kind)


def _safe_filename(name: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in (name or "file"))[:120]


async def _process_photo(att: PhotoAttachment, chat_id: str, msg_id: str) -> Optional[dict]:
    """Скачивает фото (если не превью) и возвращает media dict для EventIn."""
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


async def _process_video(att: VideoAttachment, client, chat_id: int, msg_id: str) -> Optional[dict]:
    """Скачивает видео через client.get_video_by_id."""
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


async def _process_file(att: FileAttachment, client, chat_id: int, msg_id: str) -> Optional[dict]:
    """Скачивает файл через client.get_file_by_id."""
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


def register_bridge(client) -> None:
    """Регистрирует обработчики на готовом client (после Client(...))."""

    @client.on_start()
    async def _on_start(client) -> None:
        logger.info("PyMax client started, marking auth=ok")
        # ВАЖНО: передаём ``clear_error=True``, чтобы прошлая ошибка
        # (например, ``error.limit.violate`` от прошлой неудачной попытки)
        # не висела в /status после успешной авторизации. Без этого
        # AuthWatcher в боте не увидит переход need_2fa → ok и не пришлёт
        # сообщение «✅ MAX: вход выполнен успешно».
        try:
            async with httpx.AsyncClient(base_url=API_BASE, timeout=10.0) as c:
                r = await c.post(
                    "/auth/state",
                    json={
                        "status": "ok",
                        "last_login": True,
                        "clear_error": True,
                    },
                    headers=_headers(),
                )
                r.raise_for_status()
        except Exception as exc:
            logger.warning("on_start: post auth_state ok failed: %s", exc)

        # Синхронизируем список чатов MAX с БД. Без этого /chats в боте
        # показывает пустой список, потому что @on_chat_update срабатывает
        # только при активности в чате (новое сообщение, переименование и
        # т.п.), а на старте MAX может не отправить ни одного события.
        try:
            chats_attr = getattr(client, "chats", None)
            if chats_attr is None and hasattr(client, "get_chats"):
                chats_attr = client.get_chats
            chats_list: Optional[list] = None
            if callable(chats_attr):
                try:
                    chats_list = await chats_attr()
                except TypeError:
                    chats_list = chats_attr()
            elif chats_attr is not None:
                chats_list = chats_attr
            if chats_list:
                synced = 0
                for chat in chats_list:
                    try:
                        await _post("/chats", _chat_to_dict(chat))
                        synced += 1
                    except Exception as exc:
                        logger.warning(
                            "chat upsert on start failed for %s: %s",
                            getattr(chat, "id", "?"), exc,
                        )
                logger.info("synced %d chats on start", synced)
        except Exception as exc:
            logger.warning("on_start: fetch chats failed: %s", exc)

    @client.on_chat_update()
    async def _on_chat_update(chat: MaxChat) -> None:
        try:
            await _post("/chats", _chat_to_dict(chat))
        except Exception as exc:
            logger.warning("chat update failed: %s", exc)

    @client.on_message()
    async def _on_message(message: MaxMessage, client) -> None:
        try:
            if message.chat_id is None:
                return
            chat_id = str(message.chat_id)
            msg_id = str(message.id)

            # 1) Пытаемся достать имя чата из локального кеша ``client.chats``
            #    (заполняется на login/sync).
            chat_title: Optional[str] = None
            chat_info_obj = None
            try:
                cached = getattr(client, "chats", None)
                if isinstance(cached, list):
                    for c in cached:
                        if str(c.id) == chat_id:
                            chat_title = getattr(c, "title", None) or None
                            chat_info_obj = c
                            break
            except Exception as exc:
                logger.debug("lookup chat in client.chats failed for %s: %s", chat_id, exc)

            # 2) Если в кеше нет (новый чат, личный диалог или sync ещё не
            #    прошёл) — догружаем чат с сервера. ``client.get_chat`` есть
            #    в pymax (см. ``vendor/pymax/infra/chat.py``).
            if not chat_title:
                try:
                    chat_info_obj = await client.get_chat(int(chat_id))
                    chat_title = getattr(chat_info_obj, "title", None) or None
                except Exception as exc:
                    logger.debug("get_chat for %s failed: %s", chat_id, exc)

            # 3) Для личных диалогов MAX обычно не кладёт имя собеседника в
            #    ``chat.title`` — оно лежит в ``client.users[user_id].name``.
            #    Достаём user_id из ``chat.participants`` (user_id → role,
            #    см. ``vendor/pymax/types/domain/chat.py``) и пропускаем себя.
            if not chat_title and chat_info_obj is not None:
                try:
                    participants = getattr(chat_info_obj, "participants", None) or {}
                    me_id = None
                    me = getattr(client, "me", None)
                    if me is not None:
                        contact = getattr(me, "contact", None)
                        me_id = getattr(contact, "id", None) if contact else None
                    users_map = getattr(client, "users", None) or {}
                    for uid in participants.keys():
                        if me_id is not None and uid == me_id:
                            continue
                        user = users_map.get(uid)
                        name = getattr(user, "name", None) if user else None
                        if name:
                            chat_title = name
                            break
                    # Если это DIALOG и participants пуст (бывает на старте),
                    # пробуем вычислить собеседника перебором users_map.
                    if (
                        not chat_title
                        and str(getattr(chat_info_obj, "type", "")) == "ChatType.DIALOG"
                        and me_id is not None
                    ):
                        for uid in users_map.keys():
                            if uid == me_id:
                                continue
                            candidate_name = getattr(users_map[uid], "name", None)
                            if candidate_name:
                                chat_title = candidate_name
                                break
                except Exception as exc:
                    logger.debug("lookup user for dialog %s failed: %s", chat_id, exc)

            text = message.text or ""
            event: dict[str, Any] = {
                "max_chat_id": chat_id,
                "max_message_id": msg_id,
                "chat_title": chat_title,
                "sender": str(message.sender) if message.sender else None,
                "sender_id": str(message.sender) if message.sender else None,
                "text": text,
                "kind": "text",
                "timestamp": datetime.utcfromtimestamp(message.time / 1000).isoformat()
                if message.time
                else None,
                "is_outgoing": False,
            }

            # Обработка вложений
            for att in message.attaches or []:
                if isinstance(att, PhotoAttachment):
                    media = await _process_photo(att, chat_id, msg_id)
                    if media:
                        event.update(media)
                        break  # одно фото на событие (TG send_photo принимает 1)
                elif isinstance(att, VideoAttachment):
                    media = await _process_video(att, client, int(chat_id), msg_id)
                    if media:
                        event.update(media)
                        break
                elif isinstance(att, FileAttachment):
                    media = await _process_file(att, client, int(chat_id), msg_id)
                    if media:
                        event.update(media)
                        break
                else:
                    # voice/audio/sticker/control/share/contact — пропускаем
                    logger.debug("skip attachment type=%s", getattr(att, "type", "?"))

            await _post("/events", event)
            logger.info(
                "forwarded message chat=%s msg=%s kind=%s title=%r",
                chat_id, msg_id, event.get("kind"), chat_title,
            )
        except Exception as exc:
            logger.exception("on_message handler failed: %s", exc)
