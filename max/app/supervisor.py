"""Supervisor max-процесса.

- Запускает PyMax Client в фоне (own task)
- На каждой итерации проверяет auth_state:
  - status == "ok" → ничего не делаем
  - status == "need_2fa" → стираем cache и пересоздаём Client (reauth)
- При падении Client ждёт и перезапускает
- При получении rid (от SmsCodeProvider/PasswordProvider) сам вызывает /auth/2fa/request
  (это делают провайдеры, supervisor не вмешивается)
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

import httpx
from pymax import Client
from pymax.config import ClientConfig, DeviceInfo
from pymax.auth.sms import SmsAuthFlow

from app.auth import (
    QueuePasswordProvider,
    QueueSmsCodeProvider,
    notify_code_received,
)
from app.bridge import register_bridge
from app.sender import sender_loop

logger = logging.getLogger(__name__)


API_BASE = os.getenv("API_BASE_URL", "http://localhost:8000")
API_KEY = os.getenv("BRIDGE_API_KEY", "")


def _headers() -> dict:
    return {"X-Api-Key": API_KEY}


async def _get_auth_state() -> dict:
    try:
        async with httpx.AsyncClient(base_url=API_BASE, timeout=10.0) as c:
            r = await c.get("/status", headers=_headers())
            r.raise_for_status()
            return (r.json() or {}).get("auth") or {}
    except Exception as exc:
        logger.debug("get_auth_state failed: %s", exc)
        return {}


async def _post_auth_state(status: str, error: Optional[str] = None) -> None:
    try:
        async with httpx.AsyncClient(base_url=API_BASE, timeout=10.0) as c:
            r = await c.post(
                "/auth/state",
                json={"status": status, "error": error},
                headers=_headers(),
            )
            r.raise_for_status()
    except Exception as exc:
        logger.warning("post_auth_state failed: %s", exc)


def _wipe_cache(cache_dir: str) -> None:
    """Стирает session (PyMax) в cache_dir, оставляя структуру каталога."""
    p = Path(cache_dir)
    if not p.exists():
        return
    for entry in p.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            try:
                entry.unlink()
            except OSError:
                pass
    logger.warning("cache wiped: %s", cache_dir)


def build_client(phone: str, cache_dir: str) -> Client:
    """Создаёт новый Client с нашими auth-провайдерами."""
    flow = SmsAuthFlow(
        code_provider=QueueSmsCodeProvider(),
        password_provider=QueuePasswordProvider(),
    )
    config = ClientConfig(
        phone=phone,
        work_dir=cache_dir,
        session_name="bridge",
    )
    # device оставим дефолтным (PyMax сгенерирует)
    client = Client(config=config, auth_flow=flow)
    register_bridge(client)
    return client


async def run() -> None:
    """Главный цикл supervisor'а: пересоздаёт Client при необходимости."""
    phone = os.getenv("MAX_PHONE", "")
    cache_dir = os.getenv("CACHE_DIR", "/app/cache")
    if not phone:
        logger.error("MAX_PHONE не задан, supervisor не может работать")
        await _post_auth_state("unknown", "MAX_PHONE env is empty")
        return

    stop_event = asyncio.Event()
    client: Optional[Client] = None
    client_task: Optional[asyncio.Task] = None
    sender_task: Optional[asyncio.Task] = None
    last_reauth_signal: Optional[str] = None  # анти-дубль: реагируем только на СМЕНУ need_2fa

    try:
        while not stop_event.is_set():
            # 1) Проверим, не сигналил ли бот reauth
            auth = await _get_auth_state()
            status = auth.get("status") or "unknown"
            need_reauth = (
                status == "need_2fa" and (auth.get("last_error") or "").startswith("reauth requested")
            )
            # Свежий «reauth requested» — сбрасываем кэш и пересоздаём Client
            sig = f"{status}|{auth.get('last_error') or ''}"
            if need_reauth and sig != last_reauth_signal:
                last_reauth_signal = sig
                logger.warning("reauth requested by owner, wiping cache and recreating client")
                # Остановим текущий client, если жив
                if client_task is not None and not client_task.done():
                    client_task.cancel()
                    try:
                        await client_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    client_task = None
                if sender_task is not None and not sender_task.done():
                    sender_task.cancel()
                    try:
                        await sender_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    sender_task = None
                if client is not None:
                    try:
                        await client.close()
                    except Exception:
                        pass
                _wipe_cache(cache_dir)
                # Сменим sig, чтобы следующий запуск Client прошёл как «нормальный»
                await _post_auth_state("unknown", error=None)
                status = "unknown"

            # 2) Создаём Client, если его нет или он мёртв
            if client_task is None or client_task.done():
                logger.info("creating PyMax client (status=%s)", status)
                client = build_client(phone, cache_dir)
                client_task = asyncio.create_task(client.start(), name="pymax-client")
                sender_task = asyncio.create_task(sender_loop(client, stop_event), name="pymax-sender")
                # Дождёмся немного, чтобы on_start успел выставить status=ok
                try:
                    await asyncio.wait_for(client_task, timeout=2.0)
                    # Если start() вернулся (например, ошибка авторизации) — упадём в except
                except asyncio.TimeoutError:
                    # start() не вернулся — клиент живой, идём дальше
                    pass
                except Exception as exc:
                    logger.warning("client.start() exited: %s", exc)
                    # статус авторизации мог не выставиться — пробуем отметить need_2fa
                    if "auth" in str(exc).lower() or "phone" in str(exc).lower() or "code" in str(exc).lower():
                        await _post_auth_state("need_2fa", error=str(exc))
                    else:
                        await _post_auth_state("unknown", error=str(exc))

            # 3) Подождём перед следующей итерацией
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=10.0)
                # stop_event выставлен — выходим
                break
            except asyncio.TimeoutError:
                continue

    finally:
        # shutdown
        if client_task is not None and not client_task.done():
            client_task.cancel()
            try:
                await client_task
            except (asyncio.CancelledError, Exception):
                pass
        if sender_task is not None and not sender_task.done():
            sender_task.cancel()
            try:
                await sender_task
            except (asyncio.CancelledError, Exception):
                pass
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass