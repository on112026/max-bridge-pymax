"""Методы BotApi для работы с чатами MAX (``/chats``) и read receipts."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class ChatsApiMixin:
    """``list_chats`` / ``mark_chat_read_up_to`` / ``get_pending_read_receipts``."""

    _client: object

    async def list_chats(self) -> List[Dict[str, Any]]:
        return await self._client.list_chats()

    async def mark_chat_read_up_to(self, chat_id: str) -> None:
        """Пометить все сообщения чата до текущего момента как прочитанные.

        Бот вызывает при любом действии пользователя (REPLY, SHOWID,
        ввод текста через ``/reply``). MAX-процесс заберёт эти данные
        и вызовет ``client.read_message`` для каждого сообщения.
        """
        import httpx
        async with httpx.AsyncClient(
            base_url=self._client.base_url, timeout=10.0
        ) as c:
            r = await c.post(
                f"/chats/{chat_id}/read-up-to",
                headers=self._client._headers(),
            )
            # Не падаем, если ошибка — пометка прочтения не критична.
            if r.status_code >= 400:
                logger.warning(
                    "mark_chat_read_up_to %s failed: %s %s",
                    chat_id, r.status_code, r.text[:200],
                )

    async def get_pending_read_receipts(self) -> List[Dict[str, Any]]:
        """MAX-процесс забирает список доставленных сообщений, которые
        пользователь прочитал в TG. Не используется ботом, но пригодится
        для тестов.
        """
        return await self._client.get_pending_read_receipts()