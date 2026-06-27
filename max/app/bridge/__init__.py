"""Пакет моста PyMax → api (этап 2, без headful).

Регистрирует колбэки на готовом ``Client`` (после ``Client(...)``):

* ``@client.on_start()`` — auth_state = ok, fetch chats.
* ``@client.on_message()`` — кладём Event в api + (опц.) ChatInfo.
* ``@client.on_chat_update()`` — обновляем ChatInfo.

Структура (по доменам):

* ``users`` — ``user_display_name``: имя пользователя из ``pymax.User.names``.
* ``chats`` — ``chat_to_dict``, ``display_name_of``, ``enrich_chat_titles``.
* ``media`` — ``process_photo``, ``process_video``, ``process_file``
  (скачивание вложений MAX на диск).
* ``on_start`` — ``on_start_actions``: sync чатов + sync топиков.

``register_bridge(client)`` определена прямо здесь (в ``__init__.py``
пакета), чтобы работал импорт ``from app.bridge import register_bridge``
из ``max/app/supervisor/client_runtime.py`` — Python при этом импортирует
пакет, и функция доступна как атрибут модуля.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

import httpx

from pymax.types import Chat as MaxChat
from pymax.types import Message as MaxMessage
from pymax.types.domain.attachments import (
    FileAttachment,
    PhotoAttachment,
    VideoAttachment,
)
from pymax.types.domain.enums import ChatType

from app.bridge.chats import chat_to_dict, display_name_of
from app.bridge.media import process_file, process_photo, process_video
from app.bridge.on_start import on_start_actions
from app.bridge.users import user_display_name

logger = logging.getLogger(__name__)


async def _post(path: str, json: dict = None) -> None:
    """Best-effort POST в API (без проверки ответа)."""
    import os
    api_base = os.getenv("API_BASE_URL", "http://localhost:8000")
    api_key = os.getenv("BRIDGE_API_KEY", "")
    try:
        async with httpx.AsyncClient(base_url=api_base, timeout=30.0) as c:
            r = await c.post(path, json=json or {}, headers={"X-Api-Key": api_key})
            r.raise_for_status()
    except Exception as exc:
        logger.warning("api POST %s failed: %s", path, exc)


def register_bridge(client) -> None:
    """Зарегистрировать обработчики на готовом client (после ``Client(...)``)."""

    @client.on_start()
    async def _on_start(client) -> None:
        await on_start_actions(client)

    @client.on_chat_update()
    async def _on_chat_update(chat: MaxChat) -> None:
        try:
            await _post("/chats", chat_to_dict(chat))
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
            #    ``chat.title`` — оно лежит в ``client.users[user_id].names``.
            #    Для DIALOG ID чата строится как XOR двух user_id
            #    (``first_user_id ^ second_user_id``, см.
            #    ``vendor/pymax/api/users/service.py:get_chat_id``) — это
            #    надёжнее перебора ``users_map``, потому что ``client.users``
            #    может быть пуст на старте (sync ещё не прошёл).
            if (
                not chat_title
                and chat_info_obj is not None
                and getattr(chat_info_obj, "type", None) == ChatType.DIALOG
            ):
                try:
                    me = getattr(client, "me", None)
                    me_id = getattr(getattr(me, "contact", None), "id", None) if me else None
                    users_count = len(getattr(client, "users", None) or {})
                    if me_id is not None:
                        try:
                            peer_id = int(chat_id) ^ int(me_id)
                        except (TypeError, ValueError):
                            peer_id = None
                        if peer_id is not None:
                            users_map = getattr(client, "users", None) or {}
                            user = users_map.get(peer_id)
                            found_after_fetch = user is not None
                            if user is None and hasattr(client, "get_user"):
                                try:
                                    fetched = await client.get_user(peer_id)
                                    if fetched is not None:
                                        user = fetched
                                        found_after_fetch = True
                                        try:
                                            users_map[peer_id] = fetched
                                        except Exception:
                                            pass
                                except Exception as exc:
                                    logger.info(
                                        "get_user(%s) failed: %s", peer_id, exc,
                                    )
                            chat_title = user_display_name(user)
                            logger.info(
                                "bridge DIALOG path: chat=%s me_id=%s peer_id=%s "
                                "users_count=%d found=%s found_after_fetch=%s title=%r",
                                chat_id, me_id, peer_id, users_count,
                                user is not None, found_after_fetch, chat_title,
                            )
                except Exception as exc:
                    logger.info("lookup dialog peer for %s failed: %s", chat_id, exc)

            # 4) Fallback для групповых чатов, у которых ``chat.title`` пуст:
            #    берём имя первого участника ≠ self из ``chat.participants``.
            if not chat_title and chat_info_obj is not None:
                try:
                    participants = getattr(chat_info_obj, "participants", None) or {}
                    me = getattr(client, "me", None)
                    me_id = getattr(getattr(me, "contact", None), "id", None) if me else None
                    users_map = getattr(client, "users", None) or {}
                    for uid in participants.keys():
                        if me_id is not None and uid == me_id:
                            continue
                        user = users_map.get(uid)
                        candidate = user_display_name(user)
                        if candidate:
                            chat_title = candidate
                            break
                except Exception as exc:
                    logger.debug("lookup participant for chat %s failed: %s", chat_id, exc)

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
                    media = await process_photo(att, chat_id, msg_id)
                    if media:
                        event.update(media)
                        break  # одно фото на событие (TG send_photo принимает 1)
                elif isinstance(att, VideoAttachment):
                    media = await process_video(att, client, int(chat_id), msg_id)
                    if media:
                        event.update(media)
                        break
                elif isinstance(att, FileAttachment):
                    media = await process_file(att, client, int(chat_id), msg_id)
                    if media:
                        event.update(media)
                        break
                else:
                    logger.debug("skip attachment type=%s", getattr(att, "type", "?"))

            await _post("/events", event)
            logger.info(
                "forwarded message chat=%s msg=%s kind=%s title=%r",
                chat_id, msg_id, event.get("kind"), chat_title,
            )
        except Exception as exc:
            logger.exception("on_message handler failed: %s", exc)