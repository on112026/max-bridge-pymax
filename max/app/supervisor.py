"""Supervisor max-процесса.

Логика «только по команде»:
- На старте НЕ создаём PyMax Client. Выставляем auth_state.status = "auth_required"
  и ждём явной команды от владельца (через бота).
- Supervisor крутится в цикле, опрашивая auth_state.pending_action:
    * "sms"     — стираем cache и поднимаем Client с SmsAuthFlow
    * "session" — не трогаем cache и поднимаем Client (PyMax прочитает bridge-файл)
    * "cancel"  — отмена текущего действия, возврат в auth_required
- При ошибке Client (особенно error.limit.violate) — возвращаемся в auth_required
  с notify_message, не зацикливаемся.
- При status=ok Client продолжает работать; supervisor не пересоздаёт его без причины.

Прокси-команды от владельца бот кладёт через /auth/action
(см. shared.db.set_pending_action / consume_pending_action).
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


def _find_session_files(cache_dir: str) -> list[Path]:
    """Ищет файлы-кандидаты на PyMax session в ``cache_dir``.

    Учитывает:
      * ``bridge`` (без расширения) — PyMax при ``session_name="bridge"``
        пишет сюда;
      * ``bridge.db`` — используется в API-эндпоинтах;
      * любые другие ``*.db`` — владелец мог положить session с
        произвольным именем.

    Возвращает список существующих файлов (исключая ``-shm``/``-wal``
    sidecar'ы), отсортированный по mtime (свежие первыми).
    """
    p = Path(cache_dir)
    if not p.is_dir():
        return []
    candidates: list[Path] = []
    seen: set[Path] = set()
    for name in ("bridge", "bridge.db"):
        f = p / name
        if f.is_file() and f not in seen:
            candidates.append(f)
            seen.add(f)
    for cand in p.glob("*.db"):
        if cand.name.endswith(("-shm", "-wal", "-journal")):
            continue
        if cand in seen:
            continue
        if cand.is_file():
            candidates.append(cand)
            seen.add(cand)
    candidates.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return candidates


async def _long_running_start(client: Client, stop_event: asyncio.Event) -> None:
    """Обёртка вокруг ``client.start()``, которая держит Client живым.

    Проблема: ``pymax.base.BaseClient.start()`` после первой успешной
    синхронизации с MAX (``client started``) и закрытия long-poll соединения
    (``closing connection``) возвращается чисто через ``else: return``. Для
    supervisor'а это выглядит как «Client умер» — он пытается пересоздать
    Client, и если ``status`` ещё ``ok``, мы упираемся в гард и не
    пересоздаём. В итоге мост живёт, но новые события из MAX не
    обрабатываются, потому что ``start()`` уже завершился.

    Решение: после того, как ``client.start()`` вернулся без исключения,
    переинициализируем runtime и снова вызываем ``start()``. При обрыве с
    ``ConnectionError``/``EOFError``/``OSError``/``TimeoutError`` PyMax
    делает reconnect сам (через свой внутренний ``while True`` и ``if not
    extra_config.reconnect: raise``), но после чистого ``wait_closed()``
    он всё равно возвращается — и тут уже мы перехватываем.
    """
    while not stop_event.is_set():
        try:
            await client.start()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "client.start() crashed: %s; reconnecting in %.1fs",
                exc, client.extra_config.reconnect_delay,
            )
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=client.extra_config.reconnect_delay,
                )
                # stop_event выставлен — выходим
                return
            except asyncio.TimeoutError:
                pass
        else:
            # Чистое завершение (PyMax закрыл long-poll соединение после sync,
            # ``else: return`` в pymax/base.py). Переинициализируем runtime
            # и снова запускаем start(). Сессия на диске жива, поэтому
            # повторный start() пройдёт через ``saved session loaded`` без
            # SMS/2FA.
            logger.info(
                "client.start() returned cleanly; resetting runtime and reconnecting in %.1fs",
                client.extra_config.reconnect_delay,
            )
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=client.extra_config.reconnect_delay,
                )
                return
            except asyncio.TimeoutError:
                pass
            try:
                client._reset_runtime()
            except Exception as exc:
                logger.warning("client._reset_runtime failed: %s", exc)


def build_client(phone: str, cache_dir: str) -> Client:
    """Создаёт новый Client с нашими auth-провайдерами (pymax 2.2 API)."""
    flow = SmsAuthFlow(
        code_provider=QueueSmsCodeProvider(),
        password_provider=QueuePasswordProvider(),
    )
    # ``extra_config.reconnect=True`` — PyMax внутри ``start()`` сам делает
    # reconnect при ``ConnectionError``/``EOFError``/``OSError``/
    # ``TimeoutError``. Но после чистого ``wait_closed()`` (MAX сам закрыл
    # long-poll соединение после sync) PyMax возвращается через ``else:
    # return``. Это мы обрабатываем в ``_long_running_start`` wrapper'е.
    extra = ExtraConfig(
        reconnect=True,
        reconnect_delay=2.0,
    )
    client = Client(
        phone=phone,
        session_name="bridge",
        work_dir=cache_dir,
        auth_flow=flow,
        extra_config=extra,
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


# Интервал опроса API для пометки прочитанных сообщений в MAX.
READ_RECEIPTS_INTERVAL = 3.0


async def _claim_pending_reads(stop_event: asyncio.Event) -> list[dict]:
    """Забирает из API список доставленных сообщений, которые пользователь
    уже прочитал в TG (т.е. ``delivered_at <= chat.last_read_at``).

    Возвращает список ``[{"id", "max_chat_id", "max_message_id", "delivered_at"}, ...]``.
    При ошибке возвращает ``[]`` (не падаем).
    """
    import os as _os

    api_base = _os.getenv("API_BASE_URL", "http://localhost:8000")
    api_key = _os.getenv("BRIDGE_API_KEY", "")
    try:
        async with httpx.AsyncClient(
            base_url=api_base,
            headers={"X-Api-Key": api_key},
            timeout=15.0,
        ) as c:
            r = await c.get("/chats/pending-reads")
            r.raise_for_status()
            data = r.json() if r.content else []
            return list(data or [])
    except Exception as exc:
        logger.warning("claim_pending_reads failed: %s", exc)
        return []


async def _mark_message_read(delivered_id: int, max_chat_id: str, max_message_id: str) -> None:
    """После успешного ``client.read_message`` помечает запись как прочитанную."""
    import os as _os

    api_base = _os.getenv("API_BASE_URL", "http://localhost:8000")
    api_key = _os.getenv("BRIDGE_API_KEY", "")
    try:
        async with httpx.AsyncClient(
            base_url=api_base,
            headers={"X-Api-Key": api_key},
            timeout=10.0,
        ) as c:
            r = await c.post(
                f"/chats/{max_chat_id}/messages/{max_message_id}/read",
                params={"delivered_id": str(delivered_id)},
            )
            if r.status_code >= 400:
                logger.warning(
                    "mark_message_read chat=%s msg=%s failed: %s %s",
                    max_chat_id, max_message_id, r.status_code, r.text[:200],
                )
    except Exception as exc:
        logger.warning(
            "mark_message_read chat=%s msg=%s exception: %s",
            max_chat_id, max_message_id, exc,
        )


async def _read_receipts_loop(
    stop_event: asyncio.Event,
    client_getter,
) -> None:
    """Периодически опрашивает API, вызывает ``client.read_message`` и помечает
    успех в API.

    ``client_getter`` — callable, возвращающий текущий ``Client`` (или ``None``,
    если Client ещё не поднят). Это позволяет не пересоздавать Client при
    рестарте: supervisor читает ``client`` из своей переменной через замыкание.
    """
    logger.info(
        "read_receipts_loop started (poll=%.1fs)", READ_RECEIPTS_INTERVAL,
    )
    while not stop_event.is_set():
        try:
            client = client_getter()
            if client is None:
                # Client ещё не поднят (auth_required / session_attached).
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=READ_RECEIPTS_INTERVAL
                    )
                except asyncio.TimeoutError:
                    continue
                continue

            receipts = await _claim_pending_reads(stop_event)
            if not receipts:
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=READ_RECEIPTS_INTERVAL
                    )
                except asyncio.TimeoutError:
                    continue
                continue

            logger.info(
                "read_receipts_loop: %d pending receipts to mark", len(receipts),
            )
            for r in receipts:
                if stop_event.is_set():
                    break
                chat_id_str = (r.get("max_chat_id") or "").strip()
                msg_id_str = (r.get("max_message_id") or "").strip()
                delivered_id = int(r.get("id") or 0)
                if not chat_id_str or not msg_id_str:
                    logger.warning(
                        "read_receipts_loop: skip receipt with empty chat_id/msg_id: %r",
                        r,
                    )
                    continue
                try:
                    chat_id_int = int(chat_id_str)
                except ValueError:
                    logger.warning(
                        "read_receipts_loop: cannot convert chat_id=%r to int",
                        chat_id_str,
                    )
                    continue
                try:
                    # PyMax: client.read_message(message_id, chat_id) -> ReadState
                    await client.read_message(
                        message_id=msg_id_str, chat_id=chat_id_int,
                    )
                    logger.info(
                        "read_message ok: chat=%s msg=%s",
                        chat_id_str, msg_id_str,
                    )
                    await _mark_message_read(
                        delivered_id=delivered_id,
                        max_chat_id=chat_id_str,
                        max_message_id=msg_id_str,
                    )
                except Exception as exc:
                    logger.warning(
                        "read_message FAILED chat=%s msg=%s: %s",
                        chat_id_str, msg_id_str, exc,
                    )
                    # Не помечаем как прочитанное — попробуем в следующем тике.
        except Exception as exc:
            logger.warning("read_receipts_loop tick error: %s", exc)

        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=READ_RECEIPTS_INTERVAL
            )
        except asyncio.TimeoutError:
            continue


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

    В режиме «только по команде» watcher НЕ должен ставить ``ok`` если
    владелец ещё не дал команду (``auth_required``) или идёт процесс
    SMS/2FA (``need_2fa``) или MAX под rate-limit (``rate_limited``).
    Иначе мы заспойлим владельцу ложное «✅ MAX: вход выполнен успешно»
    пока он только положил файл сессии в кэш и ещё не подтвердил.

    Дополнительно: если на диске нашёлся ``.db`` с произвольным именем
    (владелец положил руками), и в ``auth_state.session_file_path`` пусто —
    выставляем ``session_attached`` и прописываем путь, чтобы бот
    AuthWatcher прислал inline-меню «📂 Подключиться по сессии».
    """
    logger.info(
        "session watcher started (cache=%s, poll=%.1fs)",
        cache_dir, SESSION_WATCH_INTERVAL,
    )
    started_at = time.time()
    last_posted_ok = False
    last_announced_path: Optional[str] = None
    while not stop_event.is_set():
        try:
            candidates = _find_session_files(cache_dir)
            if candidates:
                # Берём самый свежий (первый после сортировки).
                session_path = candidates[0]
                mtime = session_path.stat().st_mtime
                current = shared_db.get_auth_state()
                current_status = current.get("status") or "unknown"
                current_sf = current.get("session_file_path")

                # Если в БД путь не зафиксирован — прописываем и
                # переводим в session_attached (даже если status уже
                # auth_required), чтобы владелец увидел кнопку «📂 Подключиться».
                if current_sf != str(session_path):
                    if current_status in ("auth_required", "unknown", None):
                        logger.info(
                            "session watcher: detected %s, switching to session_attached",
                            session_path,
                        )
                        shared_db.set_session_file_path(str(session_path))
                        shared_db.set_auth_state(
                            "session_attached", error=None, clear_error=True
                        )
                        shared_db.set_notify_message(
                            f"📥 На сервере обнаружен session-файл: "
                            f"<code>{session_path.name}</code>. "
                            f"Нажмите «📂 Подключиться по сессии» в меню авторизации."
                        )
                        last_posted_ok = False
                        last_announced_path = str(session_path)
                        continue

                if mtime < started_at - 5.0:
                    # Файл старый (например, лежал до старта supervisor'а).
                    # НЕ ставим ok автоматически — пусть решает владелец.
                    last_posted_ok = False
                else:
                    # Файл свежий — может ставить ok, но только если мост
                    # реально в процессе авторизации, а не ждёт команды.
                    if current_status in ("auth_required", "need_2fa", "rate_limited"):
                        # Владелец ещё не подтвердил / идёт 2FA / rate-limit.
                        # Watcher не должен вмешиваться.
                        last_posted_ok = False
                    else:
                        if not last_posted_ok:
                            logger.info(
                                "session file detected (mtime=%.0f, started_at=%.0f, status=%s), "
                                "marking auth=ok",
                                mtime, started_at, current_status,
                            )
                            shared_db.set_auth_state(
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
    """Главный цикл supervisor'а: ждёт команды от владельца и поднимает Client.

    В отличие от старой логики, Client НЕ создаётся автоматически на старте.
    Сначала выставляем ``status=auth_required`` и ждём ``pending_action`` от
    бота (``"sms"`` / ``"session"`` / ``"cancel"``). На каждой итерации:
      0) Если активен rate-limit cooldown — ждём до конца.
      1) Читаем текущее auth_state.
      2) Если Client жив и status=ok — ждём (мост работает).
      3) Если Client жив и status не ok — ждём (идёт авторизация / 2FA).
      4) Если Client умер — закрываем и обрабатываем ошибку (rate-limit → в
         auth_required с notify_message, прочие auth-ошибки → то же).
      5) Если status=ok, но Client'а нет — не пересоздаём автоматически,
         ждём (такого в норме быть не должно).
      6) Читаем pending_action. Если нет — ждём.
      7) Обрабатываем action:
           * "cancel"  → status=auth_required
           * "sms"     → wipe_cache, status=unknown, build_client
           * "session" → проверяем файл, status=session_attached, build_client
    """
    phone = os.getenv("MAX_PHONE", "")
    cache_dir = os.getenv("CACHE_DIR", "/data/cache")
    if not phone:
        logger.error("MAX_PHONE не задан, supervisor не может работать")
        shared_db.set_auth_state("unknown", error="MAX_PHONE env is empty")
        return

    stop_event = asyncio.Event()
    client: Optional[Client] = None
    client_task: Optional[asyncio.Task] = None
    sender_task: Optional[asyncio.Task] = None
    drain_task: Optional[asyncio.Task] = None
    session_task: Optional[asyncio.Task] = None
    read_receipts_task: Optional[asyncio.Task] = None
    rate_limit_until: float = 0.0
    last_sms_request_at: float = 0.0

    def _client_getter() -> Optional[Client]:
        """Возвращает текущий Client для ``_read_receipts_loop``.

        Замыкание над ``client`` позволяет loop'у читать актуальное
        значение переменной без явной передачи параметров.
        """
        return client

    # === Стартовая инициализация: режим «только по команде» ===
    initial = shared_db.get_auth_state()
    initial_status = initial.get("status") or "unknown"
    initial_session_candidates = _find_session_files(cache_dir)
    initial_session_present = bool(initial.get("session_file_path")) or bool(
        initial_session_candidates
    )

    if initial_status == "ok" and initial_session_present:
        # Тёплый запуск с валидной сессией. Всё равно требуем явного
        # подтверждения — выставляем auth_required, чтобы бот показал
        # inline-меню «Подключиться по сессии / SMS». Сессию на диске
        # НЕ трогаем.
        if initial_session_candidates and not initial.get("session_file_path"):
            shared_db.set_session_file_path(str(initial_session_candidates[0]))
        shared_db.set_auth_state("auth_required", error=None, clear_error=True)
        shared_db.set_notify_message(
            "🔁 Найдена сохранённая сессия MAX. Подтвердите подключение: "
            "«📂 Подключиться по сессии» или начните заново через «🔐 SMS-авторизация»."
        )
        logger.info(
            "warm start: session present and status=ok → auth_required, waiting for owner"
        )
    elif initial_status in ("unknown", "rate_limited", "ok"):
        # Холодный запуск или восстановление после rate-limit/ok-without-session.
        if initial_status != "auth_required":
            shared_db.set_auth_state("auth_required", error=None, clear_error=True)
        # Если на диске есть session-файл с произвольным именем (владелец
        # положил руками) — прописываем его в auth_state, чтобы бот увидел
        # кнопку «📂 Подключиться по сессии».
        if initial_session_candidates and not initial.get("session_file_path"):
            shared_db.set_session_file_path(str(initial_session_candidates[0]))
            shared_db.set_auth_state("session_attached", error=None, clear_error=True)
            shared_db.set_notify_message(
                f"📥 На сервере обнаружен session-файл: "
                f"<code>{initial_session_candidates[0].name}</code>. "
                f"Нажмите «📂 Подключиться по сессии» в меню авторизации."
            )
        else:
            shared_db.set_notify_message(
                "🆕 MAX-мост запущен. Выберите способ авторизации: «🔐 SMS-авторизация» "
                "или «📂 Подключиться по сессии» (если она уже загружена)."
            )
        logger.info(
            "cold start: status=%s → auth_required, waiting for owner", initial_status
        )
    elif initial_status == "need_2fa":
        # Рестарт во время ожидания кода. Сбрасываем, чтобы владелец
        # начал авторизацию заново.
        logger.info("restart during need_2fa: clearing state, returning to auth_required")
        shared_db.set_auth_state("auth_required", error=None, clear_error=True)
        shared_db.set_notify_message(
            "⚠️ MAX-мост перезапущен во время ожидания кода. "
            "Выберите действие заново: «🔐 SMS-авторизация» или «📂 Подключиться по сессии»."
        )
    elif initial_status == "session_attached":
        # Владелец загрузил сессию, но Client ещё не запускался.
        if initial_session_candidates and not initial.get("session_file_path"):
            shared_db.set_session_file_path(str(initial_session_candidates[0]))
        logger.info("session_attached on start: waiting for owner confirm")
        shared_db.set_notify_message(
            "📂 Сессия MAX загружена в кэш. Подтвердите подключение через бота."
        )
    # auth_required — оставляем, владелец уже в процессе выбора.

    # Запускаем drain system_state — без него мост мёртв (см. _drain_2fa_codes_loop).
    drain_task = asyncio.create_task(_drain_2fa_codes_loop(stop_event), name="2fa-drain")
    # Следим за session.db на диске — fallback для _on_start (см. _watch_session_file).
    session_task = asyncio.create_task(
        _watch_session_file(stop_event, cache_dir, phone), name="session-watcher",
    )
    # Помечаем сообщения из MAX как прочитанные, когда пользователь
    # проявляет активность в TG-боте (см. _read_receipts_loop).
    read_receipts_task = asyncio.create_task(
        _read_receipts_loop(stop_event, _client_getter),
        name="read-receipts",
    )

    try:
        while not stop_event.is_set():
            # 0) Глобальный rate-limit cooldown
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
                continue
            elif rate_limit_until > 0 and now >= rate_limit_until:
                logger.info("rate-limit cooldown elapsed, resuming")
                rate_limit_until = 0.0
                # clear_error=True, чтобы прошлая rate-limit-ошибка не висела в /status
                shared_db.set_auth_state("auth_required", error=None, clear_error=True)

            # 1) Текущее состояние
            auth = shared_db.get_auth_state()
            status = auth.get("status") or "unknown"
            session_candidates = _find_session_files(cache_dir)
            has_session_file = bool(session_candidates)
            # Путь к «актуальному» файлу: сначала то, что в БД, иначе —
            # самый свежий кандидат на диске.
            session_path_db = auth.get("session_file_path")
            if session_path_db:
                session_path = Path(session_path_db)
            elif session_candidates:
                session_path = session_candidates[0]
            else:
                session_path = Path(cache_dir) / "bridge"

            # 2) Client жив и status=ok — мост работает, ничего не делаем
            if client_task is not None and not client_task.done() and status == "ok":
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=NORMAL_POLL_SECONDS)
                    break
                except asyncio.TimeoutError:
                    continue

            # 3) Client жив, но статус ещё не ok (need_2fa / unknown / session_attached)
            #    — идёт авторизация, не дёргаем api.oneme.ru
            if client_task is not None and not client_task.done():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=NORMAL_POLL_SECONDS)
                    break
                except asyncio.TimeoutError:
                    continue

            # 4) Client'а нет или он умер — разбираемся
            if client_task is not None and client_task.done():
                exc: Optional[BaseException] = None
                try:
                    exc = client_task.exception()
                except (asyncio.CancelledError, asyncio.InvalidStateError):
                    exc = None
                # Закрываем sender и client
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
                client_task = None

                if exc is not None:
                    logger.warning("client_task exited with error: %s", exc)
                    if _is_rate_limit_error(exc):
                        # Возвращаемся в auth_required с notify_message и длинным cooldown
                        rate_limit_until = time.monotonic() + RATE_LIMIT_BACKOFF
                        shared_db.set_auth_state(
                            "auth_required", error=str(exc), clear_error=False,
                        )
                        shared_db.set_notify_message(
                            f"⚠️ MAX ограничил авторизацию (error.limit.violate). "
                            f"Мост возвращён в режим ожидания команды. "
                            f"Повторная попытка возможна через {RATE_LIMIT_BACKOFF/60:.0f} мин."
                        )
                        logger.warning(
                            "rate-limit hit, returning to auth_required; cooldown=%.0fs",
                            RATE_LIMIT_BACKOFF,
                        )
                        await _sleep_with_stop(stop_event, RATE_LIMIT_BACKOFF)
                        if stop_event.is_set():
                            break
                        continue
                    else:
                        # Любая другая ошибка — тоже возвращаемся в auth_required
                        shared_db.set_auth_state(
                            "auth_required", error=str(exc), clear_error=False,
                        )
                        shared_db.set_notify_message(
                            f"❌ Ошибка клиента MAX: {exc}. "
                            f"Мост возвращён в режим ожидания команды."
                        )
                        await _sleep_with_stop(stop_event, AUTH_FAIL_BACKOFF)
                        if stop_event.is_set():
                            break
                        continue
                else:
                    # exc is None — _long_running_start сам вышел (странно,
                    # в норме она крутится вечно). Не пересоздаём Client
                    # автоматически — ждём команду.
                    logger.warning(
                        "client_task exited cleanly without exception; "
                        "returning to auth_required and waiting for owner"
                    )
                    shared_db.set_auth_state(
                        "auth_required",
                        error="client exited unexpectedly",
                        clear_error=True,
                    )
                    shared_db.set_notify_message(
                        "⚠️ Клиент MAX неожиданно завершился. Выберите действие: "
                        "«🔐 SMS-авторизация» или «📂 Подключиться по сессии»."
                    )
                    continue

            # 5) Client'а нет, status=ok (был, но Client умер; в норме такого
            #    быть не должно, т.к. _long_running_start крутится вечно).
            #    Не пересоздаём автоматически, ждём команду.
            if status == "ok":
                logger.debug(
                    "status=ok but client_task is None; not recreating (waiting for owner)"
                )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=NORMAL_POLL_SECONDS)
                    break
                except asyncio.TimeoutError:
                    continue

            # 6) Client'а нет, status не ok — читаем pending_action
            action = shared_db.consume_pending_action()
            if not action:
                # Нет команды — supervisor бездействует, ждём
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=NORMAL_POLL_SECONDS)
                    break
                except asyncio.TimeoutError:
                    continue

            # 7) Обрабатываем команду
            if action == "cancel":
                logger.info("owner requested cancel; returning to auth_required")
                shared_db.set_auth_state("auth_required", error=None, clear_error=True)
                shared_db.set_notify_message(
                    "⛔ Действие отменено владельцем. Мост ожидает новую команду."
                )
                continue

            if action == "sms":
                # Anti-spam: не дёргаем MAX чаще, чем раз in SMS_RESEND_COOLDOWN
                now = time.monotonic()
                if last_sms_request_at > 0 and now - last_sms_request_at < SMS_RESEND_COOLDOWN:
                    wait = SMS_RESEND_COOLDOWN - (now - last_sms_request_at)
                    logger.info(
                        "sms-cooldown active, waiting %.0fs before next pymax start",
                        max(0.0, wait),
                    )
                    shared_db.set_notify_message(
                        f"⏳ Слишком частые SMS-запросы к MAX. "
                        f"Повторная попытка через {max(0.0, wait)/60:.1f} мин."
                    )
                    await _sleep_with_stop(stop_event, max(0.0, wait))
                    if stop_event.is_set():
                        break
                    continue
                last_sms_request_at = time.monotonic()

                logger.info("owner requested SMS auth; wiping cache and starting client")
                _wipe_cache(cache_dir)
                shared_db.set_auth_state("unknown", error=None, clear_error=True)
                shared_db.set_notify_message("🔐 Запрашиваю SMS-код у MAX…")
                client = build_client(phone, cache_dir)
                client_task = asyncio.create_task(
                    _long_running_start(client, stop_event), name="pymax-client",
                )
                sender_task = asyncio.create_task(
                    sender_loop(client, stop_event), name="pymax-sender",
                )
                # Дать Client'у 2с на мгновенный краш (например, error.limit.violate
                # сразу из on_start). Обработаем на следующей итерации.
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=2.0)
                    break
                except asyncio.TimeoutError:
                    pass
                continue

            if action == "session":
                logger.info("owner requested attach by saved session")
                if not has_session_file:
                    shared_db.set_auth_state("auth_required", error=None, clear_error=True)
                    shared_db.set_notify_message(
                        "❌ Файл сессии не найден в кэше. Сначала загрузите его "
                        "через бота (кнопка «📂 Загрузить сессию MAX»)."
                    )
                    continue
                # PyMax ищет сессию строго в ``cache_dir/bridge``. Если
                # выбранный файл — не ``bridge``/``bridge.db``, копируем
                # его в ``bridge`` и подчищаем sidecar-файлы.
                if session_path.name not in ("bridge", "bridge.db"):
                    target = Path(cache_dir) / "bridge"
                    try:
                        for sidecar in (
                            Path(cache_dir) / "bridge.db-shm",
                            Path(cache_dir) / "bridge.db-wal",
                        ):
                            if sidecar.exists():
                                sidecar.unlink()
                        shutil.copy2(session_path, target)
                        logger.info(
                            "copied session %s → %s for attach",
                            session_path, target,
                        )
                    except Exception as exc:
                        logger.warning(
                            "failed to copy session %s → bridge: %s",
                            session_path, exc,
                        )
                        shared_db.set_auth_state(
                            "auth_required", error=None, clear_error=True
                        )
                        shared_db.set_notify_message(
                            f"❌ Не удалось подготовить session-файл: {exc}. "
                            "Попробуйте загрузить его через бота."
                        )
                        continue
                shared_db.set_session_file_path(str(Path(cache_dir) / "bridge.db"))
                shared_db.set_auth_state("session_attached", error=None, clear_error=True)
                shared_db.set_notify_message("📂 Подключаюсь по сохранённой сессии…")
                client = build_client(phone, cache_dir)
                client_task = asyncio.create_task(
                    _long_running_start(client, stop_event), name="pymax-client",
                )
                sender_task = asyncio.create_task(
                    sender_loop(client, stop_event), name="pymax-sender",
                )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=2.0)
                    break
                except asyncio.TimeoutError:
                    pass
                continue

            # Неизвестный action — игнорируем
            logger.warning("unknown pending_action=%r, ignoring", action)
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
        # Останавливаем read_receipts_loop.
        if read_receipts_task is not None and not read_receipts_task.done():
            read_receipts_task.cancel()
            try:
                await read_receipts_task
            except (asyncio.CancelledError, Exception):
                pass
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass
