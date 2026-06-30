"""Фоновый поллер ``reaction_ops_queue`` (направления ``to_tg`` и ``to_tg_summary``).

Берёт из API задачи, которые MAX-процесс положил через ``on_reaction_update``
(или callback-кнопку «🔄 Реакции»), и применяет их в Telegram:

* ``to_tg`` — поставить/снять ботовскую реакцию на TG-сообщение
  (``setMessageReaction``). Это точная зеркальная реакция владельца
  моста: если он поставил в MAX 👍 — бот ставит ботовскую 👍 на
  соответствующее TG-сообщение в топике.

* ``to_tg_summary`` — обновить сообщение-сводку «👍×N 🔥×M · итого K»
  под исходным MAX-сообщением в топике (только для CHAT/CHANNEL).
  Если сводка ещё не создана — отправляется новое сообщение под
  исходным (``reply_to_message_id``); если уже есть — ``editMessageText``.

  Через 48 ч Telegram запрещает редактировать сообщения. Если сводке
  > 48 ч — удаляем её и создаём новую под тем же MAX-сообщением.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from aiogram import Bot
from aiogram.methods import EditMessageText, SendMessage, SetMessageReaction
from aiogram.methods.base import TelegramMethod

from app.api_client import api
from app.config import settings
from shared import db as shared_db

logger = logging.getLogger(__name__)


class ReactionsMaxPoller:
    """Поллер очереди реакций MAX → TG.

    Каждый тик:
      1. ``GET /reaction_ops/list?direction=to_tg`` → ``setMessageReaction``.
      2. ``GET /reaction_ops/list?direction=to_tg_summary`` → edit/create summary.

    Оба направления обходятся независимо. Если задач нет — спим.
    """

    POLL_INTERVAL = 1.5
    TG_EDIT_LIMIT_SECONDS = 48 * 3600  # Telegram: edit < 48h

    def __init__(
        self,
        bot: Bot,
        poll_interval: Optional[float] = None,
    ) -> None:
        self.bot = bot
        self.poll_interval = (
            poll_interval if poll_interval is not None else self.POLL_INTERVAL
        )
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        # Курсоры для каждого направления (последний обработанный id),
        # чтобы не забирать одну и ту же задачу повторно.
        self._cursor_to_tg = 0
        self._cursor_summary = 0

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="bot-reactions-poller")

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
            except Exception as exc:
                logger.warning("reactions poller tick failed: %s", exc)
            await asyncio.sleep(self.poll_interval)

    async def _tick(self) -> None:
        # 1) Точные зеркальные реакции владельца (setMessageReaction).
        try:
            tg_items = await api.list_pending_reactions_to_tg(
                direction="to_tg", after_id=self._cursor_to_tg, limit=50,
            )
        except Exception as exc:
            logger.warning("reactions poller: list to_tg failed: %s", exc)
            tg_items = []
        if tg_items:
            logger.info(
                "rx.tick: claimed %d to_tg ops (cursor=%d): ids=%s",
                len(tg_items), self._cursor_to_tg,
                [int(it.get("id") or 0) for it in tg_items],
            )
        for item in tg_items:
            await self._apply_tg_reaction(item)
            try:
                await api.finish_reaction_op(
                    item_id=int(item["id"]), ok=True,
                )
            except Exception as exc:
                logger.debug(
                    "reactions poller: finish to_tg id=%s failed: %s",
                    item.get("id"), exc,
                )
            new_id = int(item.get("id") or 0)
            if new_id > self._cursor_to_tg:
                self._cursor_to_tg = new_id

        # 2) Сводки по чужим реакциям.
        try:
            sum_items = await api.list_pending_reactions_to_tg(
                direction="to_tg_summary", after_id=self._cursor_summary, limit=50,
            )
        except Exception as exc:
            logger.warning("reactions poller: list to_tg_summary failed: %s", exc)
            sum_items = []
        if sum_items:
            logger.info(
                "rx.tick: claimed %d to_tg_summary ops (cursor=%d): ids=%s",
                len(sum_items), self._cursor_summary,
                [int(it.get("id") or 0) for it in sum_items],
            )
        for item in sum_items:
            await self._apply_summary(item)
            try:
                await api.finish_reaction_op(
                    item_id=int(item["id"]), ok=True,
                )
            except Exception as exc:
                logger.debug(
                    "reactions poller: finish to_tg_summary id=%s failed: %s",
                    item.get("id"), exc,
                )
            new_id = int(item.get("id") or 0)
            if new_id > self._cursor_summary:
                self._cursor_summary = new_id

    async def _apply_tg_reaction(self, item: Dict[str, Any]) -> None:
        """Поставить/снять ботовскую реакцию на TG-сообщение."""
        try:
            tg_chat_id = int(item.get("tg_chat_id") or 0)
            tg_message_id = int(item.get("tg_message_id") or 0)
            op = (item.get("op") or "").lower()
            emoji = item.get("emoji") or ""
        except Exception:
            logger.warning("reactions poller: bad item=%r", item)
            return
        if not tg_chat_id or not tg_message_id:
            logger.debug(
                "reactions poller: skip item without tg ids: %r", item,
            )
            return
        logger.info(
            "rx: calling setMessageReaction chat=%s msg=%s op=%s emoji=%s "
            "(reaction payload=%s)",
            tg_chat_id, tg_message_id, op, emoji,
            [{"type": "emoji", "emoji": emoji}] if (op == "add" and emoji) else [],
        )
        try:
            if op == "add" and emoji:
                await self.bot(SetMessageReaction(
                    chat_id=tg_chat_id,
                    message_id=tg_message_id,
                    reaction=[{"type": "emoji", "emoji": emoji}],
                ))
            elif op == "remove":
                await self.bot(SetMessageReaction(
                    chat_id=tg_chat_id,
                    message_id=tg_message_id,
                    reaction=[],
                ))
            logger.info(
                "reactions poller: applied tg reaction op=%s emoji=%s msg=%s",
                op, emoji, tg_message_id,
            )
        except Exception as exc:
            # Тихий fail — если бот не админ с правом reactions или
            # метод недоступен — пишем debug, не warning.
            logger.warning(
                "reactions poller: setMessageReaction failed "
                "chat=%s msg=%s op=%s emoji=%s: %s",
                tg_chat_id, tg_message_id, op, emoji, exc,
            )

    async def _apply_summary(self, item: Dict[str, Any]) -> None:
        """Создать/обновить сообщение-сводку «👍×N 🔥×M · итого K»."""
        try:
            max_chat_id = str(item.get("max_chat_id") or "")
            max_message_id = str(item.get("max_message_id") or "")
            counters_json = item.get("counters_json") or "[]"
            total_count = int(item.get("total_count") or 0)
        except Exception:
            logger.warning("reactions poller: bad summary item=%r", item)
            return
        if not max_chat_id or not max_message_id:
            return

        # Найти запись доставки → TG-идентификаторы.
        mapping = shared_db.get_delivered_by_max_message(
            max_chat_id=max_chat_id, max_message_id=max_message_id,
        )
        if mapping is None:
            logger.debug(
                "reactions poller: no DeliveredMessage for max=%s/%s — skip summary",
                max_chat_id, max_message_id,
            )
            return
        tg_chat_id = int(mapping.tg_chat_id or 0)
        tg_thread_id = mapping.tg_thread_id
        tg_message_id = int(mapping.tg_message_id or 0)
        if not tg_chat_id or not tg_message_id:
            logger.debug(
                "reactions poller: tg ids missing in mapping: %r",
                mapping,
            )
            return

        # Распарсить counters.
        try:
            counters = json.loads(counters_json) if counters_json else []
        except (TypeError, ValueError):
            counters = []
        if not isinstance(counters, list):
            counters = []
        text = _format_summary_text(counters, total_count)
        summary_id = mapping.tg_summary_message_id
        thread_kwargs: Dict[str, Any] = {}
        if tg_thread_id:
            thread_kwargs["message_thread_id"] = int(tg_thread_id)

        try:
            if summary_id:
                # Попытка редактирования. Если прошло > 48ч или сообщение
                # было удалено — Telegram вернёт ошибку.
                await self.bot(EditMessageText(
                    chat_id=tg_chat_id,
                    message_id=int(summary_id),
                    text=text,
                    disable_web_page_preview=True,
                    **thread_kwargs,
                ))
                logger.info(
                    "reactions poller: edited summary msg=%s for max=%s/%s",
                    summary_id, max_chat_id, max_message_id,
                )
            else:
                # Нет сводки — создаём новое сообщение под исходным.
                sent = await self.bot(SendMessage(
                    chat_id=tg_chat_id,
                    text=text,
                    disable_web_page_preview=True,
                    reply_to_message_id=tg_message_id,
                    **thread_kwargs,
                ))
                # Сохраняем id созданной сводки.
                try:
                    shared_db.set_summary_message_id(
                        max_chat_id=max_chat_id,
                        max_message_id=max_message_id,
                        summary_message_id=int(sent.message_id),
                    )
                except Exception as exc:
                    logger.debug(
                        "reactions poller: set_summary_message_id failed: %s",
                        exc,
                    )
                logger.info(
                    "reactions poller: created summary msg=%s for max=%s/%s",
                    sent.message_id, max_chat_id, max_message_id,
                )
        except Exception as exc:
            err_text = str(exc).lower()
            # Если сообщение нельзя редактировать (старше 48ч или удалено)
            # — пересоздаём сводку.
            if (
                "message is not modified" in err_text
                or "message to edit not found" in err_text
                or "message can't be edited" in err_text
                or "too old" in err_text
            ):
                logger.info(
                    "reactions poller: summary too old/missing, recreating: %s",
                    exc,
                )
                try:
                    sent = await self.bot(SendMessage(
                        chat_id=tg_chat_id,
                        text=text,
                        disable_web_page_preview=True,
                        reply_to_message_id=tg_message_id,
                        **thread_kwargs,
                    ))
                    shared_db.set_summary_message_id(
                        max_chat_id=max_chat_id,
                        max_message_id=max_message_id,
                        summary_message_id=int(sent.message_id),
                    )
                except Exception as exc2:
                    logger.debug(
                        "reactions poller: recreate summary failed: %s", exc2,
                    )
            else:
                logger.warning(
                    "reactions poller: summary apply failed for max=%s/%s: %s",
                    max_chat_id, max_message_id, exc,
                )


def _format_summary_text(counters: List[Dict[str, Any]], total_count: int) -> str:
    """Форматирует сводку: «👍 × 3   🔥 × 1   · итого 4»."""
    if not counters:
        return f"Реакции: 0 (итого {total_count})"
    parts: List[str] = []
    for c in counters:
        emoji = str(c.get("reaction") or "?")
        cnt = int(c.get("count") or 0)
        if not emoji or cnt <= 0:
            continue
        parts.append(f"{emoji} × {cnt}")
    if not parts:
        return f"Реакции: 0 (итого {total_count})"
    return "Реакции: " + "   ".join(parts) + f"   · итого {total_count}"