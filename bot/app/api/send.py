"""Методы BotApi для отправки в MAX (``/send``) и общий ``status``."""

from __future__ import annotations

from typing import Any, Dict, Optional


class SendApiMixin:
    """``enqueue_send`` (положить задачу в очередь) и ``status`` (снимок состояния)."""

    _client: object

    async def enqueue_send(
        self,
        target_chat_id: str,
        kind: str,
        text: Optional[str] = None,
        media_path: Optional[str] = None,
        media_mime: Optional[str] = None,
        media_filename: Optional[str] = None,
        created_by: Optional[int] = None,
        thread_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload = {
            "target_chat_id": target_chat_id,
            "kind": kind,
            "text": text,
            "media_path": media_path,
            "media_mime": media_mime,
            "media_filename": media_filename,
            "created_by": created_by,
            "thread_id": thread_id,
        }
        return await self._client.enqueue_send(payload)

    async def status(self) -> Dict[str, Any]:
        return await self._client.status()