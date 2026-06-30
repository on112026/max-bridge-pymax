"""Методы BotApi для работы с очередью реакций (``/reaction_ops``)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class ReactionsApiMixin:
    """``enqueue_reaction_op_to_max`` / ``list_pending_reactions_to_tg`` /
    ``finish_reaction_op_to_tg`` / ``reaction_op_stats``.

    MAX-сторона обращается к тем же эндпоинтам напрямую через httpx
    (``max/app/reactions_loop.py``), но для бота используем миксин
    через ``ApiClient`` (тот же паттерн, что в ``ChatOpsApiMixin``).
    """

    _client: object  # declared in BotApi core

    async def enqueue_reaction_op_to_max(
        self,
        op: str,
        max_chat_id: str,
        max_message_id: str,
        emoji: str,
    ) -> Dict[str, Any]:
        """Положить ``add``/``remove`` реакцию в очередь ``to_max``.

        ``op`` — ``"add"`` или ``"remove"``. ``emoji`` — emoji-реакция
        (для ``add``).
        """
        return await self._client.post(
            "/reaction_ops",
            json={
                "direction": "to_max",
                "op": op,
                "max_chat_id": max_chat_id,
                "max_message_id": max_message_id,
                "emoji": emoji,
            },
            headers=self._client._headers(),
        )

    async def list_pending_reactions_to_tg(
        self,
        direction: str = "to_tg",
        after_id: int = 0,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Забрать ``pending`` задачи ``to_tg`` / ``to_tg_summary``.

        Используется :class:`bot.app.handlers.reactions_max.ReactionsMaxPoller`
        для применения реакций в TG и обновления сводки.
        """
        return await self._client.list_pending_reaction_ops(
            direction=direction, after_id=after_id, limit=limit,
        )

    async def finish_reaction_op(
        self, item_id: int, ok: bool = True, error: Optional[str] = None
    ) -> None:
        """Пометить задачу ``done``/``failed`` после применения в TG."""
        return await self._client.finish_reaction_op(
            item_id=item_id, ok=ok, error=error,
        )

    async def enqueue_summary_update(
        self,
        max_chat_id: str,
        max_message_id: str,
        counters_json: str,
        total_count: int,
    ) -> Dict[str, Any]:
        """Положить ``summary_update`` задачу (для callback-кнопки «🔄 Реакции»).

        Альтернативный источник сводки — ``on_reaction_update`` в MAX-процессе,
        который сам кладёт ``to_tg_summary`` при изменениях. Эта функция
        используется, когда пользователь нажал кнопку «обновить реакции».
        """
        return await self._client.post(
            "/reaction_ops",
            json={
                "direction": "to_tg_summary",
                "op": "summary_update",
                "max_chat_id": max_chat_id,
                "max_message_id": max_message_id,
                "counters_json": counters_json,
                "total_count": int(total_count),
            },
            headers=self._client._headers(),
        )