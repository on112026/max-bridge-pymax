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

from app.api_client import api
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

                # Inline-клавиатура прикрепляется прямо к сообщению
                # с самим событием (текст/медиа), а не отдельным
                # сообщением «—» — иначе кнопки «отваливаются»
                # от контекста и через 48ч Telegram запрещает на них нажимать.
                kb = event_inline_keyboard(ev.get("id", 0), max_chat_id)
                await forward_event(
                    self.bot, sg_chat_id, ev,
                    reply_markup=kb,
                    message_thread_id=thread_id,
                )
                await api.mark_delivered(ev["id"])
            except Exception as exc:
                logger.warning(
                    "forward event %s failed: %s", ev.get("id"), exc,
                )
                # Не помечаем delivered — повторим на следующем тике