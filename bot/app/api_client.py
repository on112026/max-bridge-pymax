"""Клиент к внутреннему API моста (используется ботом, этап 2 без headful)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings
from shared.http_client import ApiClient

logger = logging.getLogger(__name__)


class BotApi:
    def __init__(self) -> None:
        self._client = ApiClient(api_key=settings.bridge_api_key)

    async def close(self) -> None:
        await self._client.close()

    async def list_undelivered(self, limit: int = 50) -> List[Dict[str, Any]]:
        return await self._client.list_undelivered(limit=limit)

    async def list_events_for_chat(self, chat_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        return await self._client.list_events_for_chat(chat_id, limit=limit)

    async def get_event(self, event_id: int) -> Optional[Dict[str, Any]]:
        """Получить одно событие по id (нужно для callback'ов reply/showid/history,
        которые передают только короткий ``event_id`` в callback_data).
        Возвращает ``None``, если событие не найдено.
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

    async def list_chats(self) -> List[Dict[str, Any]]:
        return await self._client.list_chats()

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

    async def post_auth_state(self, status: str, error: Optional[str] = None) -> None:
        """Обновить auth_state (нужно для /reauth_sms)."""
        async with httpx.AsyncClient(
            base_url=self._client.base_url, timeout=10.0
        ) as c:
            r = await c.post(
                "/auth/state",
                json={"status": status, "error": error},
                headers=self._client._headers(),
            )
            r.raise_for_status()

    async def post_2fa_code(self, request_id: int, code: str) -> None:
        await self._client.put_2fa(request_id, code)

    async def put_2fa(self, request_id: int, code: str) -> None:
        await self._client.put_2fa(request_id, code)

    async def request_2fa(self) -> int:
        """Открыть новый pending 2FA-запрос (вызывается max-процессом; ботом не используется)."""
        async with httpx.AsyncClient(
            base_url=self._client.base_url, timeout=10.0
        ) as c:
            r = await c.post(
                "/auth/2fa/request", headers=self._client._headers()
            )
            r.raise_for_status()
            return r.json()["request_id"]

    async def post_auth_action(self, action: str) -> Dict[str, Any]:
        """Команда от владельца supervisor'у: 'sms' | 'session' | 'cancel'.

        Бот вызывает этот метод после нажатия inline-кнопки «🔐 SMS» /
        «📂 Подключиться по сессии» / «⛔ Отмена». Supervisor на следующей
        итерации заберёт ``pending_action`` и обработает.
        """
        async with httpx.AsyncClient(
            base_url=self._client.base_url, timeout=10.0
        ) as c:
            r = await c.post(
                "/auth/action",
                json={"action": action},
                headers=self._client._headers(),
            )
            r.raise_for_status()
            return r.json() if r.content else {"ok": True}

    async def consume_notify(self) -> Dict[str, Any]:
        """Сбросить одноразовое ``auth_state.notify_message`` в api.

        AuthWatcher вызывает этот метод после того, как переслал
        ``notify_message`` владельцу. Без этого поле будет приходить
        в каждом ``/status`` (раз в 3 секунды), и бот будет спамить.
        """
        async with httpx.AsyncClient(
            base_url=self._client.base_url, timeout=5.0
        ) as c:
            r = await c.post(
                "/auth/notify/consume",
                headers=self._client._headers(),
            )
            r.raise_for_status()
            return r.json() if r.content else {"ok": True}

    async def upload_session_file(
        self,
        file_bytes: bytes,
        filename: str = "bridge.db",
        content_type: str = "application/octet-stream",
    ) -> Dict[str, Any]:
        """Отправить загруженный владельцем session-файл MAX в api.

        Файл сохраняется в ``CACHE_DIR/bridge.db`` (PyMax session), а
        ``auth_state`` переводится в ``session_attached`` — supervisor
        ждёт явной команды «Подключиться по сессии».
        """
        async with httpx.AsyncClient(
            base_url=self._client.base_url, timeout=120.0
        ) as c:
            r = await c.post(
                "/admin/session/upload",
                files={"file": (filename, file_bytes, content_type)},
                headers=self._client._headers(),
            )
            r.raise_for_status()
            return r.json() if r.content else {"ok": True}

    async def list_sessions(self) -> Dict[str, Any]:
        """Получить список доступных session-файлов из кэш-директории."""
        async with httpx.AsyncClient(
            base_url=self._client.base_url, timeout=10.0
        ) as c:
            r = await c.get(
                "/admin/session/list",
                headers=self._client._headers(),
            )
            r.raise_for_status()
            return r.json() if r.content else {"sessions": []}

    # Алиас для обратной совместимости со старым кодом ``handlers.py``
    # (вызывающим ``api.get_session_list()``). Оба имени работают.
    get_session_list = list_sessions

    async def use_session(self, session_name: str) -> Dict[str, Any]:
        """Выбрать session-файл для использования (скопировать в bridge.db)."""
        async with httpx.AsyncClient(
            base_url=self._client.base_url, timeout=10.0
        ) as c:
            r = await c.post(
                "/admin/session/use",
                json={"session_name": session_name},
                headers=self._client._headers(),
            )
            r.raise_for_status()
            return r.json() if r.content else {"ok": True}

    async def mark_chat_read_up_to(self, chat_id: str) -> None:
        """Пометить все сообщения чата до текущего момента как прочитанные.

        Бот вызывает при любом действии пользователя (REPLY, SHOWID,
        ввод текста через /reply). MAX-процесс заберёт эти данные
        и вызовет client.read_message для каждого сообщения.
        """
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
        async with httpx.AsyncClient(
            base_url=self._client.base_url, timeout=10.0
        ) as c:
            r = await c.get(
                "/chats/pending-reads",
                headers=self._client._headers(),
            )
            r.raise_for_status()
            return r.json() if r.content else []

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
        """Список stale-топиков владельца."""
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
        """Пометить топик закрытым (``stale=2``) после успешного closeForumTopic."""
        async with httpx.AsyncClient(
            base_url=self._client.base_url, timeout=10.0
        ) as c:
            r = await c.post(
                f"/topics/{max_chat_id}/close",
                json={"owner_user_id": int(owner_user_id)},
                headers=self._client._headers(),
            )
            r.raise_for_status()


api = BotApi()
