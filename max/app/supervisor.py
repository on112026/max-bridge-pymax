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
import time
from pathlib import Path
from typing import Optional

import httpx
from pymax import Client
from pymax.config import ExtraConfig
from pymax.auth.sms import SmsAuthFlow

from app.auth import (
    QueuePasswordProvider,
    QueueSmsCodeProvider,
    notify_code_received,
)
from app.bridge import register_bridge
from app.sender import sender_loop
from shared import db as shared_db

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


async def _post_auth_state(
    status: str,
    error: Optional[str] = None,
    clear_error: bool = False,
) -> None:
    """Пробросить изменение auth_state в api.

    Если ``clear_error=True`` — в БД будет сброшен ``last_error`` (даже если
    error=None). Нужно, чтобы при status=ok и при ручном reauth прошлая
    ошибка в /status не висела вечно.
    """
    try:
        async with httpx.AsyncClient(base_url=API_BASE, timeout=10.0) as c:
            r = await c.post(
                "/auth/state",
                json={
                    "status": status,
                    "error": error,
                    "clear_error": clear_error,
                },
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
    """Создаёт новый Client с нашими auth-провайдерами (pymax 2.2 API)."""
    flow = SmsAuthFlow(
        code_provider=QueueSmsCodeProvider(),
        password_provider=QueuePasswordProvider(),
    )
    # extra_config не передаём — Client сам сгенерирует user_agent/device_id/mt_instance_id.
    # Если потребуется — сюда можно прокинуть ExtraConfig(proxy=..., log_level=...).
    client = Client(
        phone=phone,
        session_name="bridge",
        work_dir=cache_dir,
        auth_flow=flow,
    )
    register_bridge(client)
    return client


# Backoff'ы для уменьшения количества запросов к api.oneme.ru
# и ухода от error.limit.violate.
NORMAL_POLL_SECONDS = 30.0  # основной интервал между итерациями supervisor'а
AUTH_FAIL_BACKOFF = 60.0   # пауза после неудачного client.start() (auth/phone/code)
RATE_LIMIT_BACKOFF = 600.0 # пауза при rate-limit (error.limit.violate) — 10 минут
# Минимальный интервал между запросами нового SMS-кода в секундах.
SMS_RESEND_COOLDOWN = 900.0  # 15 минут между попытками завести новый Client


def _is_rate_limit_error(exc: BaseException) -> bool:
    """Распознаём «error.limit.violate» в тексте исключения."""
    msg = str(exc).lower()
    return "limit" in msg or "too many" in msg or "ratelimit" in msg or "429" in msg


def _is_auth_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(s in msg for s in ("auth", "phone", "code", "sms", "password"))


async def _sleep_with_stop(stop_event: asyncio.Event, seconds: float) -> None:
    """Спим `seconds`, но выходим раньше, если взведён stop_event."""
    if seconds <= 0:
        return
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return


# Интервал опроса system_state на предмет введённого кода/пароля.
# Должен быть <= POLL_INTERVAL в QueueSmsCodeProvider/QueuePasswordProvider
# (1.5s), иначе провайдер начнёт сам забирать код через /auth/2fa/peek
# раньше, чем notify_code_received успеет разбудить ev.wait().
CODE_DRAIN_INTERVAL = 0.5


async def _drain_2fa_codes_loop(stop_event: asyncio.Event) -> None:
    """Фоновая задача: следит за появлением 2fa_code:<rid> в ``system_state``
    и будит соответствующий ``asyncio.Event`` в ``app.auth._EVENTS`` через
    ``notify_code_received``. Сам код не забираем: после ``ev.wait()`` провайдер
    сам вызовет ``GET /auth/2fa/peek/{rid}`` и ``db.take_pending_2fa_code``
    корректно удалит строку.

    Без этого провайдеры висели на ``ev.wait()`` до 10-минутного таймаута,
    PyMax падал, а supervisor уходил в 15-минутный sms-cooldown. До 2FA-пароля
    очередь не доходила — мост был сломан.
    """
    logger.info("2fa drain loop started (poll=%.1fs)", CODE_DRAIN_INTERVAL)
    while not stop_event.is_set():
        try:
            keys = shared_db.list_2fa_code_keys()
            for rid in keys:
                if rid in _ALREADY_NOTIFIED:
                    continue
                _ALREADY_NOTIFIED.add(rid)
                logger.info("drain: detected 2fa code rid=%s, waking provider", rid)
                # value=None — провайдер сам заберёт код из БД через peek.
                notify_code_received(rid, None)
        except Exception as exc:
            logger.warning("2fa drain loop error: %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=CODE_DRAIN_INTERVAL)
        except asyncio.TimeoutError:
            continue


# Локальный set rid'ов, которые мы уже отдали в notify_code_received.
# Нужен, чтобы не делать notify повторно (значение в system_state уже
# забрано take_pending_2fa_code, а вот rid из auth_state может жить дольше).
_ALREADY_NOTIFIED: set[int] = set()


SESSION_WATCH_INTERVAL = 5.0


async def _watch_session_file(
    stop_event: asyncio.Event,
    cache_dir: str,
    phone: str,
) -> None:
    """Следит за появлением/обновлением session-файла PyMax на диске.

    PyMax сохраняет сессию в ``cache_dir/<session_name>.db`` (``bridge.db``
    по умолчанию в нашем Client'е). Как только файл создан или его mtime
    свежее момента старта Client — считаем, что мост авторизован, и
    переводим ``auth_state`` в ``ok`` с очисткой ``last_error``.

    Зачем это нужно: ``pymax.client.start()`` — fire-and-forget, но PyMax
    завершает корутину ``start()`` сразу после первого ``client started``.
    ``client_task.done() == True`` без исключения, и supervisor не
    понимает, что мост живой — он пытается пересоздать Client, упирается
    в ``sms-cooldown`` и сбрасывает сессию. Здесь мы сами выставляем
    ``status=ok`` по факту появления session.db на диске.
    """
    session_path = Path(cache_dir) / "bridge.db"
    logger.info(
        "session watcher started (path=%s, poll=%.1fs)",
        session_path, SESSION_WATCH_INTERVAL,
    )
    started_at = time.time()
    last_posted_ok = False
    while not stop_event.is_set():
        try:
            if session_path.is_file():
                mtime = session_path.stat().st_mtime
                # Файл создан ПОСЛЕ старта Client (с запасом 5с на лаги).
                # До этого либо файла не было (мы только создали Client),
                # либо mtime старый (прошлая сессия из cache_dir).
                if mtime >= started_at - 5.0 and not last_posted_ok:
                    logger.info(
                        "session file detected (mtime=%.0f, started_at=%.0f), "
                        "marking auth=ok",
                        mtime, started_at,
                    )
                    await _post_auth_state(
                        "ok", last_login=True, clear_error=True,
                    )
                    last_posted_ok = True
        except Exception as exc:
            logger.warning("session watcher error: %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=SESSION_WATCH_INTERVAL)
        except asyncio.TimeoutError:
            continue


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
    drain_task: Optional[asyncio.Task] = None
    session_task: Optional[asyncio.Task] = None
    last_reauth_signal: Optional[str] = None  # анти-дубль: реагируем только на СМЕНУ need_2fa
    last_sms_request_at: float = 0.0         # анти-спам: время последнего create_client для need_2fa
    last_start_failed: bool = False          # только что был неудачный start() — нужен backoff
    rate_limit_until: float = 0.0            # глобальный cooldown: до этого moment не дёргаем api

    # Запускаем drain system_state — без него мост мёртв (см. _drain_2fa_codes_loop).
    drain_task = asyncio.create_task(_drain_2fa_codes_loop(stop_event), name="2fa-drain")
    # Следим за session.db на диске — как только он появится/обновится,
    # переводим auth_state в ok (см. _watch_session_file).
    session_task = asyncio.create_task(
        _watch_session_file(stop_event, cache_dir, phone), name="session-watcher",
    )

    try:
        while not stop_event.is_set():
            # 0) Если активен глобальный rate-limit — ждём до его окончания.
            now = time.monotonic()
            if rate_limit_until > 0 and now < rate_limit_until:
                wait = rate_limit_until - now
                logger.info(
                    "global rate-limit cooldown active, waiting %.0fs before next iteration",
                    wait,
                )
                await _sleep_with_stop(stop_event, wait)
                if stop_event.is_set():
                    break
            elif rate_limit_until > 0 and now >= rate_limit_until:
                # cooldown истёк — сбрасываем флаг, чтобы бот узнал о возврате
                logger.info("rate-limit cooldown elapsed, resuming")
                rate_limit_until = 0.0
                # clear_error=True, чтобы прошлая rate-limit-ошибка не висела в /status
                await _post_auth_state("unknown", error=None, clear_error=True)

            # 1) Проверим, не сигналил ли бот reauth
            auth = await _get_auth_state()
            status = auth.get("status") or "unknown"
            need_reauth = (
                status == "need_2fa" and (auth.get("last_error") or "").startswith("reauth requested")
            )
            # Свежий «reauth requested» — сбрасываем кэш и сразу
            # пересоздаём Client в этом же цикле, не дожидаясь
            # NORMAL_POLL_SECONDS (30s) следующей итерации.
            sig = f"{status}|{auth.get('last_error') or ''}"
            if need_reauth and sig != last_reauth_signal:
                last_reauth_signal = sig
                # Забываем прошлые rid'ы: после wipe_cache и рестарта Client'а
                # провайдеры заведут новые, и старые ключи в system_state
                # (если они там случайно залипли) уже не должны будить Event'ы.
                _ALREADY_NOTIFIED.clear()
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
                client = None
                _wipe_cache(cache_dir)
                # Сменим sig, чтобы следующий запуск Client прошёл как «нормальный».
                # clear_error=True, чтобы маркер «reauth requested by owner»
                # не висел в /status после того, как supervisor отреагировал.
                await _post_auth_state("unknown", error=None, clear_error=True)
                status = "unknown"
                # Сбросим sms-cooldown и last_start_failed, чтобы не ждать
                # ещё 15 минут после явного reauth от владельца.
                last_sms_request_at = 0.0
                last_start_failed = False
                # НЕ возвращаемся в конец цикла — следующий блок (2) сам
                # подхватит client_task is None и создаст Client немедленно.

            # 2) Создаём Client, если его нет или он мёртв
            if client_task is None or client_task.done():
                # ВАЖНО: если мост уже авторизован (status=ok) и сессия на диске,
                # НЕ пересоздаём Client. PyMax после первой синхронизации MAX
                # закрывает соединение, и ``client.start()`` корректно возвращается
                # (``pymax.app: client started`` → ``closing connection`` →
                # ``start() return``). Это нормальное поведение, а не падение.
                # Без этой проверки supervisor пересоздаёт Client раз в 30 секунд,
                # MAX шлёт новый SMS, и мост зацикливается в reauth/sms-cooldown.
                _auth_now = await _get_auth_state()
                _status_now = _auth_now.get("status") or "unknown"
                _session_path = Path(cache_dir) / "bridge.db"
                if _status_now == "ok" and _session_path.is_file():
                    if client_task is not None and client_task.done() and client_task.exception() is None:
                        logger.debug(
                            "pymax exited cleanly after auth (status=ok, session present); "
                            "not recreating client",
                        )
                    else:
                        logger.debug(
                            "status=ok and session present; not recreating client",
                        )
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=NORMAL_POLL_SECONDS)
                        break
                    except asyncio.TimeoutError:
                        continue

                # Anti-spam: если мы только что провалили старт — подождём подольше
                if last_start_failed:
                    logger.info(
                        "backing off %.0fs before recreating pymax client (last start failed)",
                        AUTH_FAIL_BACKOFF,
                    )
                    await _sleep_with_stop(stop_event, AUTH_FAIL_BACKOFF)
                    if stop_event.is_set():
                        break
                    last_start_failed = False

                # Anti-spam: не создаём новый Client чаще, чем раз в SMS_RESEND_COOLDOWN,
                # чтобы не молотить api.oneme.ru SMS-запросами.
                now = time.monotonic()
                if now - last_sms_request_at < SMS_RESEND_COOLDOWN and last_sms_request_at > 0:
                    wait = SMS_RESEND_COOLDOWN - (now - last_sms_request_at)
                    logger.info(
                        "sms-cooldown active, waiting %.0fs before next pymax start",
                        max(0.0, wait),
                    )
                    await _sleep_with_stop(stop_event, max(0.0, wait))
                    if stop_event.is_set():
                        break

                logger.info("creating PyMax client (status=%s)", status)
                last_sms_request_at = time.monotonic()
                client = build_client(phone, cache_dir)
                client_task = asyncio.create_task(client.start(), name="pymax-client")
                sender_task = asyncio.create_task(sender_loop(client, stop_event), name="pymax-sender")
                # ВАЖНО: client.start() — fire-and-forget. Нельзя делать
                # ``await asyncio.wait_for(client_task, timeout=...)`` — это
                # **отменяет** таску по таймауту и убивает SmsAuthFlow.authenticate
                # посреди ``await code_provider.get_code(...)``. В итоге Client
                # закрывается раньше, чем MAX получит наш код, и до 2FA-фазы дело
                # не доходит. Вместо этого спим 2 секунды через stop_event
                # (без отмены client_task) и потом проверяем, не упала ли таска.
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=2.0)
                    # stop_event выставлен — выходим из run() через finally
                    break
                except asyncio.TimeoutError:
                    pass

                # Моментальный эксепшен (например, MAX сразу вернул
                # error.limit.violate из on_start) — обработаем как раньше.
                if client_task.done():
                    exc: Optional[BaseException] = None
                    try:
                        exc = client_task.exception()
                    except (asyncio.CancelledError, asyncio.InvalidStateError):
                        exc = None
                    if exc is not None:
                        last_start_failed = True
                        # rate-limit — особый случай: даём длинный бэкофф
                        # и ставим глобальный rate_limit_until, чтобы следующий
                        # цикл не дёрнул api.oneme.ru снова сразу после sleep'а.
                        if _is_rate_limit_error(exc):
                            rate_limit_until = time.monotonic() + RATE_LIMIT_BACKOFF
                            logger.warning(
                                "rate-limit hit, backing off %.0fs (err=%s)",
                                RATE_LIMIT_BACKOFF,
                                exc,
                            )
                            await _post_auth_state("rate_limited", error=str(exc))
                            await _sleep_with_stop(stop_event, RATE_LIMIT_BACKOFF)
                            if stop_event.is_set():
                                break
                        else:
                            logger.warning("client.start() exited: %s", exc)
                            # статус авторизации мог не выставиться — пробуем
                            # отметит need_2fa
                            if _is_auth_error(exc):
                                await _post_auth_state("need_2fa", error=str(exc))
                            else:
                                await _post_auth_state("unknown", error=str(exc))

            # 3) Подождём перед следующей итерацией
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=NORMAL_POLL_SECONDS)
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
        # Сигналим drain'у и session-watcher'у остановиться и дожидаемся их.
        stop_event.set()
        if drain_task is not None and not drain_task.done():
            drain_task.cancel()
            try:
                await drain_task
            except (asyncio.CancelledError, Exception):
                pass
        if session_task is not None and not session_task.done():
            session_task.cancel()
            try:
                await session_task
            except (asyncio.CancelledError, Exception):
                pass
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass
