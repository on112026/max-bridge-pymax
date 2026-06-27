"""Ядро ``BotApi`` — единая обёртка над ``ApiClient``.

``BotApi`` — фасад для всех эндпоинтов моста. Хранит ``ApiClient`` с
HTTPX-сессией, инжектит ``X-Api-Key`` из настроек и закрывает сессию
в ``shutdown`` (вызывается из ``bot/run.py``).

Методы разнесены по модулям:

* ``events``   — ``list_undelivered``, ``list_events_for_chat``,
                 ``get_event``, ``mark_delivered``.
* ``chats``    — ``list_chats``, ``mark_chat_read_up_to``,
                 ``get_pending_read_receipts``.
* ``send``     — ``enqueue_send``, ``status``.
* ``auth``     — ``post_auth_state``, ``put_2fa`` / ``post_2fa_code``,
                 ``request_2fa``, ``post_auth_action``,
                 ``consume_notify``.
* ``sessions`` — ``upload_session_file``, ``list_sessions`` /
                 ``get_session_list``, ``use_session``.
* ``topics``   — ``claim_topic_jobs``, ``finish_topic_job``,
                 ``topic_jobs_stats``, ``list_stale_topics``,
                 ``close_stale_topic``.
"""

from __future__ import annotations

from shared.http_client import ApiClient

from app.config import settings


class BotApi:
    """Фасад над ``ApiClient`` с методами, разнесёнными по доменам."""

    def __init__(self) -> None:
        self._client = ApiClient(api_key=settings.bridge_api_key)

    async def close(self) -> None:
        await self._client.close()