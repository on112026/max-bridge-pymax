"""Фоновый поллер: забирает недоставленные события и пересылает в Telegram.

Каждое событие идёт в свой Telegram-топик в приватной supergroup
владельца (создаётся через ``/setup``). Имя топика — ``chat_title``
с подписью типа чата: ``(ЛС: <id>)`` / ``(группа: <id>)`` / ``(канал: <id>)``
(см. ``app/topics.py``). Если тип чата неизвестен — используется старый
формат ``(MAX: <id>)``.

Если пользователь ещё не сделал ``/setup`` — события остаются
``undelivered=False`` и будут отправлены, как только появится supergroup.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from aiogram import Bot
from aiogram.methods import SetMessageReaction

from app.api_client import api
from app.config import settings
from app.keyboards import event_inline_keyboard
from app.sender import forward_event
from app.topics import get_or_create_topic
from shared import db as shared_db

logger = logging.getLogger(__name__)


class EventPoller:
    """Поллер событий из API → Telegram-топики в supergroup."""

    def __init__(
        self,
        bot: Bot,
        owner_user_id: int,
        poll_interval: float = 1.5,
    ) -> None:
        self.bot = bot
        self.owner_user_id = int(owner_user_id)
        self.poll_interval = poll_interval
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # Запоминаем supergroup_chat_id после первого успешного lookup-а,
        # чтобы не лезть в БД на каждом тике.
        self._supergroup_chat_id: Optional[int] = None
        # ``_compact_topic_messages`` фиксируется при первом тике из
        # ``settings.compact_topic_messages`` и далее не перечитывается,
        # чтобы поведение внутри сессии было стабильным.
        self._compact_topic_messages: Optional[bool] = None

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="bot-event-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except Exception:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as exc:  # pragma: no cover
                logger.warning("event poller tick failed: %s", exc)
            await asyncio.sleep(self.poll_interval)

    async def _resolve_supergroup(self) -> Optional[int]:
        """Возвращает ``supergroup_chat_id`` владельца или ``None``.

        Если группа ещё не создана (пользователь не сделал ``/setup``) —
        события в БД накапливаются (delivered=False), но в TG не шлются.
        Это нормально: пользователь должен сначала сделать /setup.
        """
        if self._supergroup_chat_id:
            return self._supergroup_chat_id
        sg = shared_db.get_supergroup_for_owner(self.owner_user_id)
        if sg:
            self._supergroup_chat_id = sg.supergroup_chat_id
        return self._supergroup_chat_id

    def _is_compact(self) -> bool:
        """Compact-режим оформления сообщений в топиках (``COMPACT_TOPIC_MESSAGES``)."""
        if self._compact_topic_messages is None:
            self._compact_topic_messages = bool(
                getattr(settings, "compact_topic_messages", False)
            )
        return self._compact_topic_messages

    async def _mark_incoming_reaction(
        self,
        chat_id: int,
        message_id: int,
        emoji: str = "📨",
    ) -> None:
        """Поставить emoji-реакцию на входящее сообщение из MAX (compact-режим).

        Тихое подтверждение доставки. Если бот не админ с правом reactions
        или метод недоступен — просто ничего не делаем (как в ``topic_echo.py``).
        """
        try:
            await self.bot(SetMessageReaction(
                chat_id=chat_id,
                message_id=message_id,
                reaction=[{"type": "emoji", "emoji": emoji}],
            ))
        except Exception as exc:
            logger.debug(
                "set_reaction (incoming) failed for %s/%s: %s",
                chat_id, message_id, exc,
            )

    async def _tick(self) -> None:
        events: List[Dict[str, Any]] = await api.list_undelivered(limit=50)
        if not events:
            return

        sg_chat_id = await self._resolve_supergroup()
        if not sg_chat_id:
            # Пользователь ещё не сделал /setup. Не отправляем ничего —
            # сообщения остаются ``undelivered`` и будут отправлены,
            # как только появится supergroup.
            logger.debug(
                "event poller: no supergroup for owner %s; "
                "skipping %d events",
                self.owner_user_id, len(events),
            )
            return

        for ev in events:
            try:
                max_chat_id = str(ev.get("max_chat_id") or "")
                chat_title = ev.get("chat_title") or ""
                # Тип чата из кеша MAX (DIALOG/CHAT/CHANNEL). Если кеш
                # ещё не синхронизирован — ``None`` и имя топика соберётся
                # в старом формате ``(MAX: <id>)``.
                chat_type: Optional[str] = None
                try:
                    chat_row = shared_db.get_chat(max_chat_id)
                    if chat_row is not None:
                        chat_type = getattr(chat_row, "type", None) or None
                except Exception as exc:
                    logger.debug(
                        "forward event %s: get_chat(%s) failed: %s",
                        ev.get("id"), max_chat_id, exc,
                    )
                thread_id = await get_or_create_topic(
                    self.bot,
                    sg_chat_id,
                    max_chat_id,
                    chat_title,
                    chat_type=chat_type,
                )
                if thread_id is None:
                    logger.warning(
                        "forward event %s: failed to get/create topic for %s",
                        ev.get("id"), max_chat_id,
                    )
                    continue  # не помечаем delivered — повторим

                # Compact-режим (COMPACT_TOPIC_MESSAGES=true): в топике
                # убираем шапку «💬 чат / ↘️ автор · время» (топик уже
                # несёт контекст) и inline-кнопки (автоэхо в топике и так
                # работает, история чата лежит в нём, ID не нужен).
                # Подтверждение доставки — реакцией 📨.
                if self._is_compact():
                    kb = None
                    header: Optional[str] = ""
                else:
                    # Inline-клавиатура прикрепляется прямо к сообщению
                    # с самим событием (текст/медиа), а не отдельным
                    # сообщением «—» — иначе кнопки «отваливаются»
                    # от контекста и через 48ч Telegram запрещает на них нажимать.
                    kb = event_inline_keyboard(
                        ev.get("id", 0),
                        max_chat_id,
                        chat_type=chat_type,
                        max_message_id=str(ev.get("max_message_id") or ""),
                    )
                    header = None  # sender сам вызовет _format_header

                sent = await forward_event(
                    self.bot, sg_chat_id, ev,
                    reply_markup=kb,
                    message_thread_id=thread_id,
                    header_override=header,
                )
                # Сохраняем обратную TG-ссылку на сообщение (нужно для
                # двусторонней синхронизации реакций). Best-effort — если
                # запрос не прошёл, реакции просто не будут зеркалиться.
                if sent is not None:
                    try:
                        await api.record_tg_mapping(
                            event_id=int(ev.get("id") or 0),
                            tg_chat_id=sg_chat_id,
                            tg_thread_id=thread_id,
                            tg_message_id=int(sent.message_id),
                        )
                    except Exception as exc:
                        logger.debug(
                            "forward event %s: record_tg_mapping failed: %s",
                            ev.get("id"), exc,
                        )
                if self._is_compact() and sent is not None:
                    await self._mark_incoming_reaction(
                        sg_chat_id, sent.message_id,
                    )
                await api.mark_delivered(ev["id"])
            except Exception as exc:
                logger.warning(
                    "forward event %s failed: %s", ev.get("id"), exc,
                )
                # Не помечаем delivered — повторим на следующем тике