"""Методы BotApi для работы с событиями MAX (``/events``)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx


class EventsApiMixin:
    """``list_undelivered`` / ``list_events_for_chat`` / ``get_event`` / ``mark_delivered``."""

    _client: object  # declared in BotApi core

    async def list_undelivered(self, limit: int = 50) -> List[Dict[str, Any]]:
        return await self._client.list_undelivered(limit=limit)

    async def list_events_for_chat(
        self, chat_id: str, limit: int = 20
    ) -> List[Dict[str, Any]]:
        return await self._client.list_events_for_chat(chat_id, limit=limit)

    async def get_event(self, event_id: int) -> Optional[Dict[str, Any]]:
        """Получить одно событие по id (для callback'ов reply/showid/history).

        Возвращает ``None``, если событие не найдено или ошибка.
        """
        async with httpx.AsyncClient(
            base_url=self._client.base_url, timeout=10.0
        ) as c:
            try:
                r = await c.get(
                    f"/events/{event_id}",
                    headers=self._client._headers(),
                )
            except httpx.HTTPError:
                return None
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json() if r.content else None

    async def mark_delivered(self, event_id: int) -> None:
        await self._client.mark_delivered(event_id)