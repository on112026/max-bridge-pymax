"""PyMax auth-flow, который вместо консоли берёт коды/пароли из БД.

SmsAuthFlow вызывает ``code_provider.get_code(phone)`` и
``password_provider.get_password(hint)``. Наши провайдеры
создают pending 2FA-запрос в БД и блокируются на asyncio.Event,
который выстреливает, когда бот положит ответ через ``/code``.

Если за timeoutSec секунд код не пришёл — провайдер выбрасывает
``TimeoutError``, что ловится в supervisor и приводит к
``auth_state.status = need_2fa`` + лог-сообщению.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


# API отдаёт коды/пароли через /auth/2fa (положить) и /auth/2fa/peek/{rid} (забрать).
# max-процесс САМ кладёт pending-2fa-запрос через POST /auth/2fa/request, чтобы бот
# увидел rid в /status и отправил уведомление владельцу.
API_BASE = os.getenv("API_BASE_URL", "http://localhost:8000")
API_KEY = os.getenv("BRIDGE_API_KEY", "")


def _headers() -> dict:
    return {"X-Api-Key": API_KEY}


async def _post(path: str, json: dict | None = None) -> dict:
    async with httpx.AsyncClient(base_url=API_BASE, timeout=30.0) as c:
        r = await c.post(path, json=json, headers=_headers())
        r.raise_for_status()
        return r.json() if r.content else {}


async def _get(path: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(base_url=API_BASE, timeout=30.0) as c:
        r = await c.get(path, params=params, headers=_headers())
        r.raise_for_status()
        return r.json() if r.content else {}


# Кэш событий в рамках одного процесса: после того, как бот положил код,
# провайдер просыпается и забирает значение из БД.
_EVENTS: dict[int, asyncio.Event] = {}
_VALUES: dict[int, str] = {}


def _register_request(rid: int) -> asyncio.Event:
    ev = asyncio.Event()
    _EVENTS[rid] = ev
    return ev


def _unregister(rid: int) -> None:
    _EVENTS.pop(rid, None)
    _VALUES.pop(rid, None)


class QueueSmsCodeProvider:
    """SmsCodeProvider, берущий код из БД (положен владельцем через /code)."""

    # 10 минут на ввод кода — иначе supervisor пойдёт пересоздавать Client,
    # а каждый новый Client = новый запрос SMS к api.oneme.ru (rate-limit).
    POLL_TIMEOUT = 600.0
    POLL_INTERVAL = 1.5

    async def get_code(self, phone: str) -> str:
        # 1) Сообщить api, что нужен SMS — откроется pending 2fa-запрос.
        data = await _post("/auth/2fa/request", json={"kind": "sms"})
        rid = data["request_id"]
        logger.info("requested SMS code rid=%s for phone=%s (kind=sms)", rid, phone)
        ev = _register_request(rid)
        try:
            await asyncio.wait_for(ev.wait(), timeout=self.POLL_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("SMS code timeout rid=%s (timed out after %.0fs)", rid, self.POLL_TIMEOUT)
            raise

        # 2) Забрать код из api (он уже удалён из БД после peek)
        data = await _get(f"/auth/2fa/peek/{rid}")
        code = (data or {}).get("code") or ""
        if not code:
            raise RuntimeError(f"SMS code missing for rid={rid}")
        logger.info("got SMS code rid=%s (len=%d)", rid, len(code))
        return code


class QueuePasswordProvider:
    """PasswordProvider, берущий 2FA-пароль из БД (владелец ввёл через /code)."""

    # 10 минут на ввод 2FA-пароля — иначе supervisor пойдёт пересоздавать Client,
    # а каждый новый Client = новый запрос к api.oneme.ru (rate-limit).
    POLL_TIMEOUT = 600.0
    POLL_INTERVAL = 1.5

    async def get_password(self, hint: str | None = None) -> str:
        data = await _post("/auth/2fa/request", json={"kind": "password"})
        rid = data["request_id"]
        logger.info("requested 2FA password rid=%s hint=%s (kind=password)", rid, hint)
        ev = _register_request(rid)
        try:
            await asyncio.wait_for(ev.wait(), timeout=self.POLL_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("2FA password timeout rid=%s (timed out after %.0fs)", rid, self.POLL_TIMEOUT)
            raise

        data = await _get(f"/auth/2fa/peek/{rid}")
        pwd = (data or {}).get("code") or ""
        if not pwd:
            raise RuntimeError(f"2FA password missing for rid={rid}")
        logger.info("got 2FA password rid=%s (len=%d)", rid, len(pwd))
        return pwd


def notify_code_received(request_id: int, code: str) -> None:
    """Вызывается из supervisor'ом, когда бот положил код через /code."""
    _VALUES[request_id] = code
    ev = _EVENTS.get(request_id)
    if ev is not None:
        ev.set()
    logger.info("notify_code_received rid=%s", request_id)


def notify_code_cleared(request_id: int) -> None:
    """Код/пароль уже не нужны (например, в случае ошибки)."""
    _unregister(request_id)