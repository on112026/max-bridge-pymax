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
        tg_chat_id: Optional[int] = None,
        tg_message_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Положить задачу в очередь отправки в MAX.

        ``tg_chat_id`` / ``tg_message_id`` — id TG-сообщения, из которого
        ушёл ответ (``message.message_id`` в aiogram). MAX-процесс после
        ``client.send_message`` создаст ``DeliveredMessage``-строку,
        связывающую ``(max_chat_id, str(msg.id))`` ↔ ``(tg_chat_id,
        thread_id, tg_message_id)``. Без этого мост MAX→TG-реакций не
        сможет зеркалить реакции на наши же сообщения (логирует
        «DIALOG-mirror skip, no DeliveredMessage»).
        """
        payload = {
            "target_chat_id": target_chat_id,
            "kind": kind,
            "text": text,
            "media_path": media_path,
            "media_mime": media_mime,
            "media_filename": media_filename,
            "created_by": created_by,
            "thread_id": thread_id,
            "tg_chat_id": tg_chat_id,
            "tg_message_id": tg_message_id,
        }
        return await self._client.enqueue_send(payload)

    async def status(self) -> Dict[str, Any]:
        return await self._client.status()