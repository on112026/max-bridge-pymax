"""Логика чатов MAX: сериализация и обогащение ``title``.

PyMax часто возвращает пустой ``chat.title`` (особенно для личных
диалогов DIALOG). Чтобы Telegram-топики получали осмысленные имена,
мы подменяем ``chat.title`` на имя собеседника / первого участника.

Используется из:

* ``on_start`` — батчем перед ``sync_topics``.
* ``_on_message`` — для каждого нового сообщения.

Поток:

1. ``chat_to_dict(chat)`` — сериализация MAX-чата в ``dict`` для
   ``POST /chats``.
2. ``display_name_of(chat)`` — обогащённое ``title`` (читает уже
   подменённый атрибут).
3. ``enrich_chat_titles(client, chats)`` — обогащает ``title`` для
   чатов с пустым именем (DIALOG/CHAT/CHANNEL).

Системные чаты MAX с ``chat_id == 0`` — это «Избранное» (Saved Messages):
peer_id = 0 ^ me_id = me_id, и в ``client.users[me_id]`` лежит мусор,
поэтому для них возвращаем фиксированное имя «Избранное».
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from pymax.types import Chat as MaxChat
from pymax.types.domain.enums import ChatType

from app.bridge.users import user_display_name

logger = logging.getLogger(__name__)


# Хардкод для известных системных чатов MAX. ``0`` — это всегда
# «Избранное» (Saved Messages), peer_id = 0 ^ me_id = me_id, и в
# ``client.users[me_id]`` лежит какой-то мусор — поэтому лучше отдать
# фиксированное русское имя.
SYSTEM_NAMES = {
    0: "Избранное",
}


def chat_to_dict(chat: MaxChat) -> dict:
    """Сериализовать MAX-чат в ``dict`` для ``POST /chats``."""
    return {
        "max_chat_id": str(chat.id),
        "title": chat.title or "",
        "type": str(chat.type) if chat.type else "chat",
        "last_message_preview": (
            chat.last_message.text[:200] if chat.last_message and chat.last_message.text else None
        ),
        "last_message_at": (
            datetime.utcfromtimestamp(chat.last_event_time / 1000).isoformat()
            if chat.last_event_time
            else None
        ),
        "unread": chat.new_messages if chat.new_messages is not None else None,
    }


def display_name_of(chat: Any) -> Optional[str]:
    """«Человеческое» имя чата: ``chat.title`` (уже обогащённое)."""
    title = (getattr(chat, "title", None) or "").strip()
    return title or None


async def enrich_chat_titles(client: Any, chats: list) -> None:
    """Обогатить ``chat.title`` для чатов с пустым именем.

    Нужно, чтобы ``TopicSyncWorker`` создал топики сразу с человеческими
    именами, а не с ``(MAX: <id>)`` или пустой строкой.

    Логика:
      * **Системные чаты** (``chat_id == 0`` и т.п.) — фиксированные имена.
      * **DIALOG**: ``peer_id = chat_id ^ me_id`` → ``client.users[peer_id]``
        или ``await client.get_user(peer_id)`` → ``user_display_name``.
        Если ``peer_id == me_id`` (диалог с самим собой, не 0) — берём
        собственное имя из ``client.me``.
      * **CHAT / CHANNEL**: перебираем ``chat.participants`` (исключая self),
        для каждого берём ``client.users[uid]`` или догружаем через
        ``client.get_user(uid)``; имя первого непустого становится title.
      * **Фолбэк**: для любого необогащённого DIALOG/CHAT — ставим
        ``"MAX чат #<id>"``, чтобы топик в Telegram получил хоть какое-то
        осмысленное имя (а не пустую строку или ``(MAX: <id>)`` без контекста).
    """
    if not chats:
        return
    me = getattr(client, "me", None)
    me_id = getattr(getattr(me, "contact", None), "id", None) if me else None
    me_id_int = int(me_id) if me_id is not None else None

    enriched = 0
    for chat in chats:
        try:
            existing_title = (getattr(chat, "title", None) or "").strip()
            if existing_title:
                continue
            chat_id_raw = getattr(chat, "id", None)
            if chat_id_raw is None:
                continue
            chat_id_int = int(chat_id_raw)
            chat_type = getattr(chat, "type", None)
            users_map = getattr(client, "users", None) or {}
            resolved_name: Optional[str] = None
            resolved_peer_id: Optional[int] = None

            # 1) Системные чаты с фиксированным именем.
            if chat_id_int in SYSTEM_NAMES:
                resolved_name = SYSTEM_NAMES[chat_id_int]

            # 2) DIALOG → peer через XOR, fallback на собственное имя,
            #    если peer == me_id (диалог с самим собой не через 0).
            elif chat_type == ChatType.DIALOG and me_id_int is not None:
                try:
                    peer_id = chat_id_int ^ me_id_int
                except (TypeError, ValueError):
                    peer_id = None
                if peer_id is not None and peer_id > 0:
                    user = users_map.get(peer_id)
                    if user is None and hasattr(client, "get_user"):
                        try:
                            fetched = await client.get_user(peer_id)
                            if fetched is not None:
                                user = fetched
                                try:
                                    users_map[peer_id] = fetched
                                except Exception:
                                    pass
                        except Exception as exc:
                            logger.info(
                                "enrich: get_user(%s) failed: %s",
                                peer_id, exc,
                            )
                    if user is not None:
                        resolved_name = user_display_name(user)
                        resolved_peer_id = peer_id
                if (
                    not resolved_name
                    and peer_id is not None
                    and peer_id == me_id_int
                ):
                    me_name = (
                        user_display_name(me) if me is not None else None
                    )
                    if me_name:
                        resolved_name = me_name
                        resolved_peer_id = me_id_int

            # 3) CHAT / CHANNEL: первый участник ≠ self с непустым именем.
            else:
                participants = getattr(chat, "participants", None) or {}
                for uid in participants.keys():
                    if me_id_int is not None and uid == me_id_int:
                        continue
                    user = users_map.get(uid)
                    if user is None and hasattr(client, "get_user"):
                        try:
                            fetched = await client.get_user(uid)
                            if fetched is not None:
                                user = fetched
                                try:
                                    users_map[uid] = fetched
                                except Exception:
                                    pass
                        except Exception as exc:
                            logger.info(
                                "enrich: get_user(%s) failed: %s", uid, exc,
                            )
                            user = None
                    candidate = user_display_name(user)
                    if candidate:
                        resolved_name = candidate
                        resolved_peer_id = int(uid)
                        break

            # 4) Финальный фолбэк: для любого необогащённого чата даём
            # честное имя ``"MAX чат #<id>"``.
            if not resolved_name:
                resolved_name = f"MAX чат #{chat_id_int}"
                logger.info(
                    "enrich: chat=%s type=%s — fallback to generic name",
                    chat_id_raw, chat_type,
                )

            # Подменяем атрибут ``title`` на обогащённое имя. pymax
            # использует Pydantic v1/v2 — в обоих случаях ``__setattr__``
            # работает (модель mutable, allow_mutations=True в v1).
            try:
                chat.title = resolved_name
                enriched += 1
                logger.info(
                    "enrich: chat=%s type=%s peer=%s → %r",
                    chat_id_raw, chat_type, resolved_peer_id, resolved_name,
                )
            except Exception as exc:
                logger.info(
                    "enrich: failed to set title for chat=%s: %s",
                    chat_id_raw, exc,
                )
        except Exception as exc:
            logger.warning(
                "enrich: chat=%s failed: %s", getattr(chat, "id", "?"), exc,
            )
    if enriched:
        logger.info("enrich: обогатили %d/%d чатов именами", enriched, len(chats))