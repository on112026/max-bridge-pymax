"""edit_worker.py — применяет правки сообщений MAX → Telegram.

Когда собеседник (или ИИ в режиме стриминга) редактирует сообщение в MAX,
bridge записывает новый текст в БД и выставляет флаг ``text_edited_at``.
EditWorker каждые ``poll_interval`` секунд:

1. Берёт все события с ``text_edited_at IS NOT NULL``.
2. Для каждого находит ``DeliveredMessage`` (tg_chat_id + tg_message_id).
3. Вызывает ``bot.edit_message_text()``.
4. Сбрасывает флаг ``text_edited_at = NULL``.

Стриминг ИИ-ответов: во время стрима приходят десятки правок в секунду.
Чтобы не спамить Telegram API (лимит: 1 edit/сек на сообщение),
EditWorker дебаунсирует — берёт только последнюю версию текста
и игнорирует промежуточные, если они успели накопиться за интервал опроса.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from shared import db as shared_db

logger = logging.getLogger(__name__)

# Интервал опроса. 2 секунды — достаточно для стриминга ИИ:
# промежуточные версии будут игнорироваться, в TG уйдёт только финальная.
_DEFAULT_POLL = 2.0


class EditWorker:
    """Фоновый воркер: редактирует TG-сообщения по флагу text_edited_at."""

    def __init__(self, bot: Bot, poll_interval: float = _DEFAULT_POLL) -> None:
        self.bot = bot
        self.poll_interval = poll_interval
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="edit-worker")
        logger.info("EditWorker started (poll=%.1fs)", self.poll_interval)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("EditWorker stopped")

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("EditWorker tick error: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        pending = shared_db.list_pending_edits(limit=50)
        if not pending:
            return

        for event in pending:
            await self._apply_edit(event)

    async def _apply_edit(self, event) -> None:
        """Отредактировать одно TG-сообщение."""
        mapping = shared_db.get_delivered_by_max_message(
            event.max_chat_id,
            event.max_message_id,
        )
        if not mapping:
            # Событие не доставлено в TG — сбрасываем флаг, текст придёт
            # актуальным при следующем forward.
            logger.debug(
                "edit_worker: no TG mapping for chat=%s msg=%s — clearing flag",
                event.max_chat_id, event.max_message_id,
            )
            shared_db.clear_edit_flag(event.id)
            return

        tg_chat_id = int(mapping.tg_chat_id or 0)
        tg_message_id = int(mapping.tg_message_id or 0)
        new_text = event.text or ""

        if not tg_chat_id or not tg_message_id:
            logger.warning(
                "edit_worker: incomplete TG mapping for event %s — skip", event.id
            )
            shared_db.clear_edit_flag(event.id)
            return

        try:
            await self.bot.edit_message_text(
                chat_id=tg_chat_id,
                message_id=tg_message_id,
                text=new_text or "_(пустое сообщение)_",
            )
            logger.info(
                "edit_worker: edited tg_msg=%s in chat=%s (event=%s, text_len=%d)",
                tg_message_id, tg_chat_id, event.id, len(new_text),
            )
        except TelegramBadRequest as exc:
            err = str(exc).lower()
            if "message is not modified" in err:
                # Текст совпадает с текущим в TG — просто снимаем флаг.
                pass
            elif "message to edit not found" in err:
                logger.warning(
                    "edit_worker: TG message %s not found (deleted?) — clearing flag",
                    tg_message_id,
                )
            else:
                logger.error(
                    "edit_worker: TelegramBadRequest for tg_msg=%s: %s",
                    tg_message_id, exc,
                )
                # Не сбрасываем флаг — попробуем снова на следующем тике.
                return
        except TelegramRetryAfter as exc:
            logger.warning(
                "edit_worker: rate limited, retry after %s s", exc.retry_after
            )
            await asyncio.sleep(exc.retry_after)
            return  # Флаг остаётся — повторим на следующем тике.
        except Exception as exc:
            logger.error("edit_worker: unexpected error for event %s: %s", event.id, exc)
            return

        shared_db.clear_edit_flag(event.id)
