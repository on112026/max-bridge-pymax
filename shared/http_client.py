"""HTTP-клиент для внутренних вызовов API моста (этап 2, без headful).

Этот модуль НЕ зависит от FastAPI, чтобы его мог импортировать
и бот, и max-процесс, не подтягивая за собой лишние зависимости.
"""

from __future__ import annotations

import os
from typing import Optional

import httpx

from shared.http_client_chat_ops import ChatOpsHttpMixin


def api_base_url() -> str:
    # В монолитном деплое (например, Railway) все процессы крутятся в одном
    # контейнере, и api доступен на localhost.
    host = os.getenv("API_HOST_INTERNAL", "localhost")
    port = int(os.getenv("API_PORT", "8000"))
    return f"http://{host}:{port}"


class ApiClient(ChatOpsHttpMixin):
    """Тонкая обёртка над httpx + chat-ops mixin (``/chat_ops/...``)."""

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None) -> None:
        self.base_url = base_url or api_base_url()
        self.api_key = api_key or os.getenv("BRIDGE_API_KEY", "")
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict:
        return {"X-Api-Key": self.api_key}

    async def post_event(self, event: dict) -> dict:
        r = await self._client.post("/events", json=event, headers=self._headers())
        r.raise_for_status()
        return r.json()

    async def post_chat(self, chat: dict) -> dict:
        r = await self._client.post("/chats", json=chat, headers=self._headers())
        r.raise_for_status()
        return r.json()

    async def list_undelivered(self, limit: int = 50) -> list:
        r = await self._client.get(
            "/events", params={"undelivered": "1", "limit": str(limit)}, headers=self._headers()
        )
        r.raise_for_status()
        return r.json()

    async def list_events_for_chat(self, chat_id: str, limit: int = 20) -> list:
        r = await self._client.get(
            f"/events/by-chat/{chat_id}",
            params={"limit": str(limit)},
            headers=self._headers(),
        )
        r.raise_for_status()
        return r.json()

    async def mark_delivered(self, event_id: int) -> None:
        r = await self._client.post(
            f"/events/{event_id}/delivered", headers=self._headers()
        )
        r.raise_for_status()

    async def list_chats(self) -> list:
        r = await self._client.get("/chats", headers=self._headers())
        r.raise_for_status()
        return r.json()

    async def enqueue_send(self, payload: dict) -> dict:
        r = await self._client.post("/send", json=payload, headers=self._headers())
        r.raise_for_status()
        return r.json()

    async def get_send(self, item_id: int) -> dict:
        r = await self._client.get(f"/send/{item_id}", headers=self._headers())
        r.raise_for_status()
        return r.json()

    async def put_2fa(self, request_id: int, code: str) -> dict:
        r = await self._client.post(
            "/auth/2fa",
            json={"request_id": request_id, "code": code},
            headers=self._headers(),
        )
        r.raise_for_status()
        return r.json()

    async def status(self) -> dict:
        r = await self._client.get("/status", headers=self._headers())
        r.raise_for_status()
        return r.json()