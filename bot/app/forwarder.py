"""Фоновый поллер: забирает недоставленные события и пересылает в Telegram."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from aiogram import Bot

from app.api_client import api
from app.keyboards import event_inline_keyboard
from app.sender import forward_event

logger = logging.getLogger(__name__)


class EventPoller:
    def __init__(self, bot: Bot, target_chat_id: int, poll_interval: float = 1.5) -> None:
        self.bot = bot
        self.target_chat_id = target_chat_id
        self.poll_interval = poll_interval
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

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

    async def _tick(self) -> None:
        events: List[Dict[str, Any]] = await api.list_undelivered(limit=50)
        for ev in events:
            try:
                # Inline-клавиатура прикрепляется прямо к сообщению
                # с самим событием (текст/медиа), а не отдельным
                # сообщением «—» — иначе кнопки «отваливаются»
                # от контекста и через 48ч Telegram запрещает на них нажимать.
                kb = event_inline_keyboard(
                    ev.get("id", 0), ev.get("max_chat_id", "")
                )
                await forward_event(
                    self.bot, self.target_chat_id, ev, reply_markup=kb
                )
                await api.mark_delivered(ev["id"])
            except Exception as exc:
                logger.warning("forward event %s failed: %s", ev.get("id"), exc)
                # Не помечаем delivered — повторим на следующем тике
