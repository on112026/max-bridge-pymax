"""Методы BotApi для chat-операций MAX (``/chat_ops/*``).

Под chat-операциями понимаются действия MAX-аккаунта владельца над
группами/каналами и пользователями:

* ``join_chat`` / ``resolve_chat`` — вступить в чат по ссылке или
  получить превью чата.
* ``invite_to_chat`` — пригласить пользователей в чат MAX.
* ``list_join_requests`` / ``confirm_join_requests`` /
  ``decline_join_requests`` — заявки на вступление.
* ``search_user_by_phone`` — поиск пользователя по номеру телефона.

Результаты синхронных операций (``search_user_by_phone``,
``list_join_requests``) читаются через polling:

* ``wait_chat_op(item_id)`` — крутится в ожидании результата.
* ``get_chat_op(item_id)`` — текущий статус.
* ``chat_op_stats`` — статистика очереди.

Все операции кладутся в очередь ``chat_ops_queue`` на стороне api
(см. ``api/routers/chat_ops.py`` и ``shared/db/chat_ops_queue.py``).
MAX-процесс забирает их через ``GET /chat_ops/next``, выполняет
через ``pymax.Client`` и сообщает о результате через
``POST /chat_ops/{id}/finish``. Бот получает результат либо по
``wait_chat_op`` (polling внутри), либо по ``get_chat_op`` из
обработчика.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class ChatOpsApiMixin:
    """``join_chat`` / ``resolve_chat`` / ``invite_to_chat`` /
    ``list_join_requests`` / ``confirm_join_requests`` /
    ``decline_join_requests`` / ``search_user_by_phone`` /
    ``wait_chat_op`` / ``get_chat_op`` / ``chat_op_stats``.

    Тонкая обёртка над ``self._client`` (см. :class:`BotApi`),
    в который уже подмешан :class:`shared.http_client_chat_ops.ChatOpsHttpMixin`.
    HTTP-вызовы делаются там, здесь только тонкие методы с
    говорящими именами и сигнатурами под нужды ботовых хендлеров.
    """

    _client: object

    # ------------------------------------------------------------------
    # Enqueue (от бота)
    # ------------------------------------------------------------------

    async def join_chat(
        self,
        link: str,
        kind: Optional[str] = None,
        created_by: Optional[int] = None,
    ) -> Dict[str, Any]:
        """``POST /chat_ops/join`` — вступить в группу/канал MAX по ссылке.

        ``link`` — полная ссылка вида ``https://max.ru/join/<token>``
        (или просто ``join/<token>``). ``kind`` — подсказка
        (``"group"`` / ``"channel"``), фактический выбор делает pymax.
        ``created_by`` — TG user_id владельца (для аудита).
        """
        return await self._client.enqueue_chat_op_join(
            link=link, kind=kind, created_by=created_by,
        )

    async def resolve_chat(self, link: str) -> Dict[str, Any]:
        """``POST /chat_ops/resolve`` — превью чата по ссылке (без вступления)."""
        return await self._client.enqueue_chat_op_resolve(link=link)

    async def invite_to_chat(
        self,
        chat_id: str,
        user_ids: List[int],
        show_history: bool = True,
    ) -> Dict[str, Any]:
        """``POST /chat_ops/invite`` — пригласить пользователей в чат MAX."""
        return await self._client.enqueue_chat_op_invite(
            chat_id=chat_id,
            user_ids=user_ids,
            show_history=show_history,
        )

    async def list_join_requests(self, chat_id: str) -> Dict[str, Any]:
        """``POST /chat_ops/list_join_requests`` — список заявок на вступление."""
        return await self._client.enqueue_chat_op_list_join_requests(
            chat_id=chat_id,
        )

    async def confirm_join_requests(
        self,
        chat_id: str,
        user_ids: List[int],
    ) -> Dict[str, Any]:
        """``POST /chat_ops/confirm_join_request`` — принять заявки."""
        return await self._client.enqueue_chat_op_confirm(
            chat_id=chat_id, user_ids=user_ids,
        )

    async def decline_join_requests(
        self,
        chat_id: str,
        user_ids: List[int],
    ) -> Dict[str, Any]:
        """``POST /chat_ops/decline_join_request`` — отклонить заявки."""
        return await self._client.enqueue_chat_op_decline(
            chat_id=chat_id, user_ids=user_ids,
        )

    async def search_user_by_phone(self, phone: str) -> Dict[str, Any]:
        """``POST /chat_ops/search_user`` — поиск пользователя по номеру телефона."""
        return await self._client.enqueue_chat_op_search_user(phone=phone)

    # ------------------------------------------------------------------
    # Polling результата (от бота)
    # ------------------------------------------------------------------

    async def get_chat_op(self, item_id: int) -> Dict[str, Any]:
        """``GET /chat_ops/{id}`` — текущий статус задачи (без ожидания)."""
        return await self._client.get_chat_op(item_id=item_id, wait=False)

    async def wait_chat_op(
        self,
        item_id: int,
        timeout: float = 30.0,
        poll_interval: float = 0.5,
    ) -> Dict[str, Any]:
        """``GET /chat_ops/{id}?wait=true`` — крутится в polling до результата.

        Для синхронных операций (``search_user_by_phone``,
        ``list_join_requests``) удобно дёрнуть этот метод сразу после
        enqueue и получить ответ одним вызовом, без отдельного
        polling-цикла в обработчике.
        """
        return await self._client.get_chat_op(
            item_id=item_id,
            wait=True,
            timeout=timeout,
            poll_interval=poll_interval,
        )

    async def chat_op_stats(self) -> Dict[str, Any]:
        """``GET /chat_ops/stats`` — статистика очереди (для ``/status``)."""
        return await self._client.get_chat_op_stats()