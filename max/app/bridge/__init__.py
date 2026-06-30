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

import json
import logging
import time
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
from pymax.types.events import ReactionUpdateEvent
from pymax.protocol import InboundFrame
from pymax.dispatch.enums import EventType

from app.bridge.chats import chat_to_dict, display_name_of
from app.bridge.media import process_file, process_photo, process_video
from app.bridge.on_start import on_start_actions
from app.bridge.users import user_display_name
from app.pymax_patches import apply as apply_pymax_patches
from shared import db as shared_db

logger = logging.getLogger(__name__)


def _resolve_chat_type(client, chat_id: Any) -> Optional[ChatType]:
    """Определить :class:`ChatType` MAX-чата по ``chat_id``.

    Используется в ``_on_reaction_update``, чтобы выбрать стратегию
    зеркалирования реакций в TG:

    * ``DIALOG`` — мост зеркалит в TG **любую** реакцию оппонента
      (берёт первую эмодзи из ``counters``), потому что в ЛС моста
      «владелец ↔ оппонент» реакции владельца в MAX — это единственные
      реакции, которые есть смысл отражать в TG.
    * ``CHAT`` / ``CHANNEL`` — мост НЕ зеркалит «свои» реакции
      (для группы в MAX с десятками участников это бессмысленно),
      а только сводку ``to_tg_summary`` под исходным сообщением.

    Сначала смотрим локальный кеш ``client.chats`` (заполняется на
    ``on_start`` через ``fetch_chats``), иначе — догружаем чат
    с сервера ``client.get_chat(int(chat_id))``. Если ничего не
    вышло — возвращаем ``None`` (хендлер должен в этом случае
    остаться на старом пути «your_reaction / get_reactions»).
    """
    try:
        cached = getattr(client, "chats", None)
        if isinstance(cached, list):
            cid_int = int(chat_id) if chat_id is not None else None
            for c in cached:
                try:
                    if cid_int is not None and int(getattr(c, "id", 0)) == cid_int:
                        ct = getattr(c, "type", None)
                        if ct is not None:
                            return ct
                except Exception:
                    continue
    except Exception as exc:
        logger.debug(
            "_resolve_chat_type: client.chats lookup failed for %s: %s",
            chat_id, exc,
        )
    # Fallback: ``get_chat`` с сервера.
    try:
        if hasattr(client, "get_chat") and chat_id is not None:
            chat_obj = client.get_chat(int(chat_id))
            return getattr(chat_obj, "type", None)
    except Exception as exc:
        logger.info(
            "_resolve_chat_type: get_chat(%s) failed: %s",
            chat_id, exc,
        )
    return None


def _event_map_has_reaction_changed() -> bool:
    """Проверить, что в ``EVENT_MAP`` есть opcode реакции сообщения."""
    try:
        from pymax.dispatch import mapping as dispatch_mapping
        from pymax.protocol import Opcode
        opcode = getattr(Opcode, "NOTIF_MSG_REACTIONS_CHANGED", None)
        if opcode is None:
            return False
        return opcode in dispatch_mapping.EVENT_MAP
    except Exception:
        return False


def _event_map_has_you_reacted() -> bool:
    """Проверить, что в ``EVENT_MAP`` есть opcode своей реакции."""
    try:
        from pymax.dispatch import mapping as dispatch_mapping
        from pymax.protocol import Opcode
        opcode = getattr(Opcode, "NOTIF_MSG_YOU_REACTED", None)
        if opcode is None:
            return False
        return opcode in dispatch_mapping.EVENT_MAP
    except Exception:
        return False


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
    # Наложить monkey-patches на PyMax до того, как Client.start()
    # начнёт слушать long-poll и кто-либо вызовет add_reaction /
    # remove_reaction. См. ``max/app/pymax_patches.py``.
    apply_pymax_patches()

    @client.on_start()
    async def _on_start(client) -> None:
        await on_start_actions(client)

    # Логируем КАЖДЫЙ фрейм long-poll — нужно для диагностики источника
    # обновлений реакций (см. задачу «MAX шлёт реакции как opcode=135, а не
    # 155/156»). Включается через env ``MAX_BRIDGE_RAW_LOG=all`` (по
    # умолчанию — оставляем старое поведение, фильтр по «REACT»/reaction,
    # чтобы не засорять логи). Чтобы не упустить ничего важного, но и не
    # топить лог PING-ами, opcode 1 (PING) всегда пропускается.
    import os
    _raw_log_mode = (os.getenv("MAX_BRIDGE_RAW_LOG", "react") or "react").lower()
    # Режимы:
    #   ``off``   — не логировать ничего (полностью выключить трейс).
    #   ``react`` — старое поведение: только фреймы, похожие на реакции
    #               (по opcode "REACT" или по подстроке ``reaction`` в payload).
    #   ``all``   — логировать вообще все inbound-фреймы (кроме PING).
    #                payload обрезается до ``MAX_BRIDGE_RAW_LOG_MAX`` символов
    #                (по умолчанию 800), чтобы не раздувать лог.
    _raw_log_max = int(os.getenv("MAX_BRIDGE_RAW_LOG_MAX", "800"))

    def _looks_like_reaction_payload(payload: Any) -> bool:
        if payload is None:
            return False
        try:
            text = repr(payload).lower()
        except Exception:
            return False
        return (
            "reaction" in text
            or "notif_msg_reactions_changed" in text
            or "notif_msg_you_reacted" in text
        )

    def _opcode_name(opcode: Any) -> str:
        """Превратить числовой opcode в имя ``Opcode.<NAME>`` (если есть)."""
        if opcode is None:
            return "?"
        try:
            from pymax.protocol import Opcode
            for member in Opcode:
                if int(member.value) == int(opcode):
                    return f"Opcode.{member.name}(={opcode})"
        except Exception:
            pass
        return str(opcode)

    @client.on_raw()
    async def _on_raw_reaction_trace(frame: InboundFrame, client) -> None:
        opcode = getattr(frame, "opcode", None)
        # PING (1) и пустые фреймы пропускаем в любом режиме — это шум.
        if opcode in (None, 1):
            return
        op_str = str(opcode).upper() if opcode is not None else ""
        is_react_related = (
            "REACT" in op_str
            or _looks_like_reaction_payload(getattr(frame, "payload", None))
        )
        if _raw_log_mode == "off":
            return
        if _raw_log_mode == "react" and not is_react_related:
            return
        try:
            payload_repr = (
                repr(frame.payload) if frame.payload else "None"
            )
            if len(payload_repr) > _raw_log_max:
                payload_repr = payload_repr[:_raw_log_max] + "...<truncated>"
        except Exception as exc:
            payload_repr = f"<unreprable: {exc}>"
        op_name = _opcode_name(opcode)
        if is_react_related:
            logger.info(
                "raw.reaction frame: opcode=%s(%s) cmd=%s seq=%s payload=%s",
                opcode,
                op_name,
                getattr(frame, "cmd", "?"),
                getattr(frame, "seq", "?"),
                payload_repr,
            )
        else:
            logger.info(
                "raw.all frame: opcode=%s(%s) cmd=%s seq=%s payload=%s",
                opcode,
                op_name,
                getattr(frame, "cmd", "?"),
                getattr(frame, "seq", "?"),
                payload_repr,
            )

    # Факт регистрации хендлеров — для отладки.
    try:
        router = getattr(client, "root_router", None) or getattr(
            getattr(client, "dispatcher", None), "root_router", None
        )
        handlers_map = getattr(router, "handlers", {}) if router else {}
        logger.info(
            "register_bridge: handlers registered: "
            "on_start=%s on_chat_update=%s on_message=%s "
            "on_reaction_update=%s on_raw=%s "
            "EVENT_MAP contains REACTION_UPDATE-resolvable opcodes: "
            "MSG_REACTIONS_CHANGED=%s MSG_YOU_REACTED=%s",
            getattr(client, "on_start_handler", None) is not None,
            bool(handlers_map.get(EventType.CHAT_UPDATE)),
            bool(handlers_map.get(EventType.MESSAGE_NEW)),
            bool(handlers_map.get(EventType.REACTION_UPDATE)),
            bool(handlers_map.get(EventType.RAW)),
            "yes" if _event_map_has_reaction_changed() else "no",
            "yes" if _event_map_has_you_reacted() else "no",
        )
    except Exception as exc:
        logger.debug("register_bridge: handler count log failed: %s", exc)


    @client.on_chat_update()
    async def _on_chat_update(chat: MaxChat, client) -> None:
        # PyMax 2.2.0 диспатчит обработчики как ``handler(event, client)``
        # (см. ``vendor/pymax/dispatch/router.py::HandlerCallback``).
        # ``client`` не используем, но принимаем обязательно — иначе
        # dispatcher бросит TypeError на каждом chat update.
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

    @client.on_reaction_update()
    async def _on_reaction_update(event: ReactionUpdateEvent, client) -> None:
        """Прокидываем обновления реакций из MAX в мост.

        Три независимых потока:

        1. ``to_tg_summary`` — сводка «👍×N 🔥×M · итого K» под
           входящим сообщением из MAX в топике супергруппы. Используется
           ВСЕМИ реакциями в группе/канале (включая чужие). **В DIALOG НЕ
           шлётся** — в ЛС владелец видит только свою реакцию через
           ``setMessageReaction``, отдельное сообщение-сводка под исходным
           сообщением не нужна.

        2. ``to_tg`` для DIALOG — точная зеркальная реакция оппонента в ЛС
           (берём первую эмодзи из ``counters``). В DIALOG моста MAX ↔ TG
           (владелец ↔ оппонент) бот в TG ставит ту же эмодзи, что поставил
           оппонент в MAX: так в TG-топике видно «лайкнул ли меня собеседник».
           Дополнительно: если в MAX реакцию сняли (``counters=[]``,
           ``total=0``) — бот снимает свою зеркальную реакцию в TG
           (``op=remove``). Реализовано без state, что для DIALOG эквивалентно
           «помнить какую эмодзи бот ставил» (в DIALOG оппонент в принципе
           может поставить только одну эмодзи на сообщение).

        3. ``to_tg`` для CHAT/CHANNEL (старый путь) — точная зеркальная
           реакция бота в TG на ту же эмодзи, которую поставил владелец
           моста в MAX (через ``client.get_reactions`` узнаём ``your_reaction``).
           Если владелец ничего не ставил — задача не создаётся.
        """
        try:
            chat_id_str = str(event.chat_id)
            msg_id_str = str(event.message_id)

            logger.info(
                "bridge.on_reaction_update: event received chat=%s msg=%s "
                "counters=%s total=%d",
                chat_id_str, msg_id_str,
                [getattr(c, "reaction", "?") for c in (event.counters or [])],
                int(getattr(event, "total_count", 0) or 0),
            )

            # Подготовим counters/total заранее — они нужны и для сводки,
            # и для DIALOG-ветки (выбор первой эмодзи).
            counters = [
                {"reaction": getattr(c, "reaction", "?"), "count": int(getattr(c, "count", 0))}
                for c in (event.counters or [])
            ]
            total = int(getattr(event, "total_count", 0) or 0)

            # Определяем тип чата ОДИН раз: DIALOG → зеркалим оппонента
            # (ветка 2) и НЕ шлём сводку, CHAT/CHANNEL → зеркалим только
            # свою реакцию (ветка 3) и шлём сводку. ``_resolve_chat_type``
            # сперва смотрит ``client.chats``, затем — ``client.get_chat``.
            # Если определить не удалось — ``is_dialog`` остаётся ``False``,
            # и мы ведём себя по старому «безопасному» пути (сводка шлётся).
            chat_type = _resolve_chat_type(client, event.chat_id)
            is_dialog = (chat_type == ChatType.DIALOG)
            logger.info(
                "bridge.on_reaction_update: chat_type=%s is_dialog=%s "
                "chat=%s msg=%s",
                chat_type, is_dialog, chat_id_str, msg_id_str,
            )

            # 1) Сводка по счётчикам — кидаем ТОЛЬКО для CHAT/CHANNEL.
            #    В DIALOG сводка не нужна: в ЛС владелец видит свою реакцию
            #    через ``setMessageReaction`` (``to_tg``), а лишнее
            #    сообщение-«итого реакций» под исходным сообщением только
            #    шумит. Если chat_type не определился — оставляем старое
            #    поведение и шлём сводку (безопасный fallback).
            if is_dialog:
                if counters or total > 0:
                    logger.info(
                        "bridge.on_reaction_update: DIALOG chat, skip "
                        "to_tg_summary chat=%s msg=%s counters=%s total=%d",
                        chat_id_str, msg_id_str, counters, total,
                    )
            elif counters or total > 0:
                await _post(
                    "/reaction_ops",
                    {
                        "direction": "to_tg_summary",
                        "op": "summary_update",
                        "max_chat_id": chat_id_str,
                        "max_message_id": msg_id_str,
                        "counters_json": json.dumps(counters, ensure_ascii=False),
                        "total_count": total,
                    },
                )
                logger.info(
                    "bridge.on_reaction_update: enqueued summary_update "
                    "chat=%s msg=%s counters=%s total=%d",
                    chat_id_str, msg_id_str, counters, total,
                )

            # 2/3) Выбор ветки зеркалирования в TG. ``chat_type`` /
            # ``is_dialog`` уже определены выше, повторно не вызываем.

            if is_dialog:
                # Ветка DIALOG: зеркалим первую эмодзи из counters.
                # ``to_tg`` handler в боте (``_apply_tg_reaction``) требует
                # ``tg_chat_id`` и ``tg_message_id`` — иначе skip, поэтому
                # заранее резолвим их через ``DeliveredMessage`` (тот же
                # путь, что и для ``to_tg_summary``).
                tg_chat_id: Optional[int] = None
                tg_message_id: Optional[int] = None
                try:
                    mapping = shared_db.get_delivered_by_max_message(
                        max_chat_id=chat_id_str,
                        max_message_id=msg_id_str,
                    )
                    if mapping is not None:
                        tg_chat_id = int(mapping.tg_chat_id or 0) or None
                        tg_message_id = int(mapping.tg_message_id or 0) or None
                except Exception as exc:
                    logger.debug(
                        "on_reaction_update: get_delivered_by_max_message "
                        "failed for %s/%s: %s",
                        chat_id_str, msg_id_str, exc,
                    )
                if not tg_chat_id or not tg_message_id:
                    logger.info(
                        "bridge.on_reaction_update: DIALOG-mirror skip, "
                        "no DeliveredMessage chat=%s msg=%s",
                        chat_id_str, msg_id_str,
                    )
                    return

                if counters:
                    first_emoji = str(counters[0].get("reaction") or "").strip()
                    if not first_emoji:
                        logger.info(
                            "bridge.on_reaction_update: DIALOG-mirror skip, "
                            "empty first emoji chat=%s msg=%s",
                            chat_id_str, msg_id_str,
                        )
                        return
                    await _post(
                        "/reaction_ops",
                        {
                            "direction": "to_tg",
                            "op": "add",
                            "max_chat_id": chat_id_str,
                            "max_message_id": msg_id_str,
                            "tg_chat_id": tg_chat_id,
                            "tg_message_id": tg_message_id,
                            "emoji": first_emoji,
                        },
                    )
                    logger.info(
                        "bridge.on_reaction_update: DIALOG-mirror enqueued "
                        "to_tg add chat=%s msg=%s tg=%s/%s emoji=%s",
                        chat_id_str, msg_id_str, tg_chat_id, tg_message_id,
                        first_emoji,
                    )
                else:
                    # counters пуст → оппонент снял реакцию → снимаем
                    # зеркальную реакцию бота в TG.
                    await _post(
                        "/reaction_ops",
                        {
                            "direction": "to_tg",
                            "op": "remove",
                            "max_chat_id": chat_id_str,
                            "max_message_id": msg_id_str,
                            "tg_chat_id": tg_chat_id,
                            "tg_message_id": tg_message_id,
                            "emoji": None,
                        },
                    )
                    logger.info(
                        "bridge.on_reaction_update: DIALOG-mirror enqueued "
                        "to_tg remove chat=%s msg=%s tg=%s/%s",
                        chat_id_str, msg_id_str, tg_chat_id, tg_message_id,
                    )
                return

            # Ветка CHAT/CHANNEL (старая логика): зеркалим только свою
            # реакцию (``your_reaction`` через ``get_reactions``).
            try:
                reactions_map = await client.get_reactions(
                    chat_id=event.chat_id,
                    message_ids=[str(event.message_id)],
                )
            except Exception as exc:
                logger.debug(
                    "on_reaction_update: get_reactions(%s/%s) failed: %s",
                    chat_id_str, msg_id_str, exc,
                )
                reactions_map = None
            your_reaction: Optional[str] = None
            if reactions_map:
                # PyMax возвращает ``dict[str, ReactionInfo]`` — ключ — message_id.
                ri = reactions_map.get(str(event.message_id))
                if ri is not None:
                    your_reaction = getattr(ri, "your_reaction", None)
            logger.info(
                "bridge.on_reaction_update: your_reaction=%r reactions_map=%s "
                "chat=%s msg=%s",
                your_reaction,
                bool(reactions_map),
                chat_id_str, msg_id_str,
            )
            if not your_reaction:
                # Нет своей реакции у владельца моста — зеркалить нечего.
                logger.info(
                    "bridge.on_reaction_update: no your_reaction, skip to_tg "
                    "chat=%s msg=%s total=%d",
                    chat_id_str, msg_id_str, total,
                )
                return
            await _post(
                "/reaction_ops",
                {
                    "direction": "to_tg",
                    "op": "add",
                    "max_chat_id": chat_id_str,
                    "max_message_id": msg_id_str,
                    "emoji": your_reaction,
                },
            )
            logger.info(
                "bridge.on_reaction_update: enqueued to_tg add chat=%s msg=%s emoji=%s",
                chat_id_str, msg_id_str, your_reaction,
            )
        except Exception as exc:
            logger.exception("on_reaction_update handler failed: %s", exc)
