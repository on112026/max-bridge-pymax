"""HTTP-mixin для chat-операций MAX.

Добавляется в :class:`shared.http_client.ApiClient` через множественное
наследование:

.. code-block:: python

    class ApiClient(ChatOpsHttpMixin, _BaseApiClient):
        ...

Все методы — тонкие обёртки над эндпойнтами ``api/routers/chat_ops.py``.
Бот использует эти методы в :mod:`bot.app.api.chat_ops`.
"""

from __future__ import annotations

from typing import Any, List, Optional


class ChatOpsHttpMixin:
    """Mixin: методы ``/chat_ops/...`` через :class:`httpx.AsyncClient`.

    Ожидается, что у класса есть:

    * ``self._client: httpx.AsyncClient`` — уже созданный клиент с ``base_url``.
    * ``self._headers() -> dict`` — хелпер для авторизационных заголовков.
    """

    # ------------------------------------------------------------------
    # Enqueue (от бота)
    # ------------------------------------------------------------------

    async def enqueue_chat_op_join(self, link: str, kind: Optional[str] = None,
                                   created_by: Optional[int] = None) -> dict:
        """``POST /chat_ops/join`` — вступить в группу/канал MAX по ссылке."""
        body: dict = {"link": link}
        if kind:
            body["kind"] = kind
        params: dict = {}
        if created_by is not None:
            params["created_by"] = str(int(created_by))
        r = await self._client.post(  # type: ignore[attr-defined]
            "/chat_ops/join", json=body, params=params, headers=self._headers(),  # type: ignore[attr-defined]
        )
        r.raise_for_status()
        return r.json()

    async def enqueue_chat_op_resolve(self, link: str) -> dict:
        """``POST /chat_ops/resolve`` — превью чата по ссылке."""
        r = await self._client.post(  # type: ignore[attr-defined]
            "/chat_ops/resolve", json={"link": link}, headers=self._headers(),  # type: ignore[attr-defined]
        )
        r.raise_for_status()
        return r.json()

    async def enqueue_chat_op_invite(
        self,
        chat_id: str,
        user_ids: List[int],
        show_history: bool = True,
    ) -> dict:
        """``POST /chat_ops/invite`` — пригласить пользователей в чат MAX."""
        r = await self._client.post(  # type: ignore[attr-defined]
            "/chat_ops/invite",
            json={
                "chat_id": str(chat_id),
                "user_ids": [int(x) for x in user_ids],
                "show_history": bool(show_history),
            },
            headers=self._headers(),  # type: ignore[attr-defined]
        )
        r.raise_for_status()
        return r.json()

    async def enqueue_chat_op_list_join_requests(self, chat_id: str) -> dict:
        """``POST /chat_ops/list_join_requests`` — список заявок."""
        r = await self._client.post(  # type: ignore[attr-defined]
            "/chat_ops/list_join_requests",
            json={"chat_id": str(chat_id)},
            headers=self._headers(),  # type: ignore[attr-defined]
        )
        r.raise_for_status()
        return r.json()

    async def enqueue_chat_op_confirm(self, chat_id: str,
                                      user_ids: List[int]) -> dict:
        """``POST /chat_ops/confirm_join_request`` — принять заявки."""
        r = await self._client.post(  # type: ignore[attr-defined]
            "/chat_ops/confirm_join_request",
            json={"chat_id": str(chat_id), "user_ids": [int(x) for x in user_ids]},
            headers=self._headers(),  # type: ignore[attr-defined]
        )
        r.raise_for_status()
        return r.json()

    async def enqueue_chat_op_decline(self, chat_id: str,
                                      user_ids: List[int]) -> dict:
        """``POST /chat_ops/decline_join_request`` — отклонить заявки."""
        r = await self._client.post(  # type: ignore[attr-defined]
            "/chat_ops/decline_join_request",
            json={"chat_id": str(chat_id), "user_ids": [int(x) for x in user_ids]},
            headers=self._headers(),  # type: ignore[attr-defined]
        )
        r.raise_for_status()
        return r.json()

    async def enqueue_chat_op_search_user(self, phone: str) -> dict:
        """``POST /chat_ops/search_user`` — поиск по телефону."""
        r = await self._client.post(  # type: ignore[attr-defined]
            "/chat_ops/search_user",
            json={"phone": str(phone)},
            headers=self._headers(),  # type: ignore[attr-defined]
        )
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------
    # Polling-ожидание результата
    # ------------------------------------------------------------------

    async def get_chat_op(self, item_id: int,
                          wait: bool = False,
                          timeout: float = 30.0,
                          poll_interval: float = 0.5) -> dict:
        """``GET /chat_ops/{id}`` — polling-ожидание результата."""
        params = {
            "wait": "true" if wait else "false",
            "timeout": str(float(timeout)),
            "poll_interval": str(float(poll_interval)),
        }
        r = await self._client.get(  # type: ignore[attr-defined]
            f"/chat_ops/{int(item_id)}", params=params, headers=self._headers(),  # type: ignore[attr-defined]
        )
        r.raise_for_status()
        return r.json()

    async def get_chat_op_stats(self) -> dict:
        """``GET /chat_ops/stats`` — статистика очереди."""
        r = await self._client.get(  # type: ignore[attr-defined]
            "/chat_ops/stats", headers=self._headers(),  # type: ignore[attr-defined]
        )
        r.raise_for_status()
        return r.json()

    async def requeue_chat_op(self, item_id: int) -> dict:
        """``POST /chat_ops/{id}/requeue`` — переставить failed-задачу."""
        r = await self._client.post(  # type: ignore[attr-defined]
            f"/chat_ops/{int(item_id)}/requeue", headers=self._headers(),  # type: ignore[attr-defined]
        )
        r.raise_for_status()
        return r.json()