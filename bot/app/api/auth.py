"""Методы BotApi для авторизации MAX (``/auth/*``)."""

from __future__ import annotations

from typing import Any, Dict, Optional

import httpx


class AuthApiMixin:
    """``post_auth_state`` / ``put_2fa`` / ``request_2fa`` / ``post_auth_action`` / ``consume_notify``."""

    _client: object

    async def post_auth_state(
        self, status: str, error: Optional[str] = None
    ) -> None:
        """Обновить ``auth_state`` (нужно для ``/reauth_sms``)."""
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
        """Алиас ``put_2fa`` для обратной совместимости со старым кодом."""
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