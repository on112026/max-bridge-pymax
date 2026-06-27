"""Методы BotApi для работы с топиками (``/topic_jobs/*`` и ``/topics/*``)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx


class TopicsApiMixin:
    """``claim_topic_jobs`` / ``finish_topic_job`` / ``topic_jobs_stats``
    / ``list_stale_topics`` / ``close_stale_topic``."""

    _client: object

    # ---- Очередь задач синка топиков (TopicSyncWorker) ----

    async def claim_topic_jobs(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Забрать пачку pending-джобов на создание/переименование топика.

        Используется ``TopicSyncWorker`` (см. ``bot/app/topic_worker.py``).
        API переводит джобы в ``in_progress`` и возвращает уже обновлённые строки.
        """
        async with httpx.AsyncClient(
            base_url=self._client.base_url, timeout=10.0
        ) as c:
            r = await c.get(
                "/topic_jobs/claim",
                params={"limit": str(limit)},
                headers=self._client._headers(),
            )
            r.raise_for_status()
            data = r.json() if r.content else {"jobs": []}
            return list(data.get("jobs") or [])

    async def finish_topic_job(
        self,
        job_id: int,
        ok: bool = True,
        error: Optional[str] = None,
    ) -> None:
        """Сообщить API, что джоб выполнен (или провалился)."""
        async with httpx.AsyncClient(
            base_url=self._client.base_url, timeout=10.0
        ) as c:
            r = await c.post(
                f"/topic_jobs/{job_id}/finish",
                json={"ok": ok, "error": error},
                headers=self._client._headers(),
            )
            r.raise_for_status()

    async def topic_jobs_stats(self) -> Dict[str, Any]:
        """Сводка по очереди (для логов и /status)."""
        async with httpx.AsyncClient(
            base_url=self._client.base_url, timeout=10.0
        ) as c:
            r = await c.get(
                "/topic_jobs/stats",
                headers=self._client._headers(),
            )
            r.raise_for_status()
            return r.json() if r.content else {}

    # ---- Stale-топики (/prune_topics) ----

    async def list_stale_topics(self, owner_user_id: int) -> List[Dict[str, Any]]:
        """Список stale-топиков владельца (``stale=1``)."""
        async with httpx.AsyncClient(
            base_url=self._client.base_url, timeout=10.0
        ) as c:
            r = await c.get(
                "/topics/stale",
                params={"owner_user_id": str(int(owner_user_id))},
                headers=self._client._headers(),
            )
            r.raise_for_status()
            data = r.json() if r.content else {"topics": []}
            return list(data.get("topics") or [])

    async def close_stale_topic(self, max_chat_id: str, owner_user_id: int) -> None:
        """Пометить топик закрытым (``stale=2``) после успешного ``closeForumTopic``."""
        async with httpx.AsyncClient(
            base_url=self._client.base_url, timeout=10.0
        ) as c:
            r = await c.post(
                f"/topics/{max_chat_id}/close",
                json={"owner_user_id": int(owner_user_id)},
                headers=self._client._headers(),
            )
            r.raise_for_status()