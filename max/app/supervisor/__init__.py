"""Пакет supervisor'а max-процесса.

Структура:

* ``_backoff``      — backoff-параметры (``NORMAL_POLL_SECONDS``,
                      ``RATE_LIMIT_BACKOFF``, ``SMS_RESEND_COOLDOWN`` и т.д.),
                      распознавание ошибок (``is_rate_limit_error``,
                      ``is_auth_error``), хелпер ``sleep_with_stop``.
* ``client_runtime`` — ``build_client`` (создаёт Client с нашими auth-flow)
                      и ``_long_running_start`` (обёртка вокруг ``client.start()``,
                      держит Client живым между чистыми возвратами).
* ``cache``         — ``wipe_cache`` (стирает PyMax session),
                      ``find_session_files`` (ищет кандидатов),
                      ``watch_session_file`` (фоновая задача).
* ``twofa_drain``   — ``drain_2fa_codes_loop`` (будит asyncio.Event
                      провайдеров, когда бот кладёт код через ``/code``).
* ``read_receipts`` — ``read_receipts_loop`` (помечает прочитанные
                      сообщения в MAX через ``client.read_message``).

``run()`` — главный цикл supervisor'а, определён прямо здесь
(в ``__init__.py`` пакета), чтобы работал импорт ``from app.supervisor
import run`` из ``max/run.py`` — Python при этом импортирует именно
пакет, и функция ``run`` доступна как атрибут модуля.

Логика «только по команде»:

- На старте НЕ создаём PyMax Client. Выставляем
  ``auth_state.status = auth_required`` и ждём явной команды
  от владельца (через бота).
- Supervisor крутится в цикле, опрашивая ``auth_state.pending_action``:
    * ``"sms"``     — стираем cache и поднимаем Client с ``SmsAuthFlow``.
    * ``"session"`` — не трогаем cache и поднимаем Client (PyMax прочитает
                     bridge-файл).
    * ``"cancel"``  — отмена текущего действия, возврат в ``auth_required``.
- При ошибке Client (особенно ``error.limit.violate``) — возвращаемся
  в ``auth_required`` с ``notify_message``, не зацикливаемся.
- При ``status=ok`` Client продолжает работать; supervisor не пересоздаёт
  его без причины.

Команды от владельца бот кладёт через ``/auth/action``
(см. ``shared.db.set_pending_action`` / ``consume_pending_action``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Optional

from pymax import Client

from app import chat_ops
from app import reactions_loop as reactions
from app.sender import sender_loop
from app.supervisor._backoff import (
    AUTH_FAIL_BACKOFF,
    NORMAL_POLL_SECONDS,
    RATE_LIMIT_BACKOFF,
    SMS_RESEND_COOLDOWN,
    is_rate_limit_error,
    sleep_with_stop,
)
from app.supervisor.cache import (
    find_session_files,
    watch_session_file,
    wipe_cache,
)
from app.supervisor.client_runtime import _long_running_start, build_client
from app.supervisor.read_receipts import read_receipts_loop
from app.supervisor.twofa_drain import drain_2fa_codes_loop
from shared import db as shared_db

logger = logging.getLogger(__name__)


async def run() -> None:
    """Главный цикл supervisor'а: ждёт команды от владельца и поднимает Client.

    В отличие от старой логики, Client НЕ создаётся автоматически на старте.
    Сначала выставляем ``status=auth_required`` и ждём ``pending_action`` от
    бота (``"sms"`` / ``"session"`` / ``"cancel"``). На каждой итерации:
      0) Если активен rate-limit cooldown — ждём до конца.
      1) Читаем текущее auth_state.
      2) Если Client жив и status=ok — ждём (мост работает).
      3) Если Client жив и status не ok — ждём (идёт авторизация / 2FA).
      4) Если Client умер — закрываем и обрабатываем ошибку.
      5) Если status=ok, но Client'а нет — не пересоздаём автоматически.
      6) Читаем pending_action. Если нет — ждём.
      7) Обрабатываем action: ``cancel`` / ``sms`` / ``session``.
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
    # Фоновая задача chat-операций (join/invite/заявки). Хранит
    # ссылку на живой Client через ``chat_ops.set_client``.
    chat_ops_task: Optional[asyncio.Task] = None
    rate_limit_until: float = 0.0
    last_sms_request_at: float = 0.0

    def _client_getter() -> Optional[Client]:
        """Замыкание: возвращает текущий Client для ``_read_receipts_loop``."""
        return client

    # === Стартовая инициализация: режим «только по команде» ===
    initial = shared_db.get_auth_state()
    initial_status = initial.get("status") or "unknown"
    initial_session_candidates = find_session_files(cache_dir)
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

    # === Защита от "fake ok" ===
    current_after_init = shared_db.get_auth_state()
    if current_after_init.get("status") == "ok":
        logger.warning(
            "stale status=ok detected at startup (Client not yet running); "
            "forcing auth_required to allow processing pending_action"
        )
        shared_db.set_auth_state("auth_required", error=None, clear_error=True)
        shared_db.set_notify_message(
            "🆕 MAX-мост перезапущен. Подтвердите подключение: "
            "«📂 Подключиться по сессии» или «🔐 SMS-авторизация»."
        )
        if current_after_init.get("pending_action"):
            logger.info(
                "startup: pending_action=%r preserved, will be processed below",
                current_after_init.get("pending_action"),
            )

    # Запускаем фоновые задачи.
    drain_task = asyncio.create_task(drain_2fa_codes_loop(stop_event), name="2fa-drain")
    session_task = asyncio.create_task(
        watch_session_file(stop_event, cache_dir, phone), name="session-watcher",
    )
    read_receipts_task = asyncio.create_task(
        read_receipts_loop(stop_event, _client_getter),
        name="read-receipts",
    )
    # Chat-ops — отдельная фоновая задача. Работает даже когда Client
    # не поднят (``auth_required``): задачи из ``chat_ops_queue`` просто
    # копятся в БД, и ``chat_ops.is_ready()`` сразу даст сигнал,
    # когда Client появится.
    chat_ops_task = asyncio.create_task(
        chat_ops.chat_ops_loop(stop_event), name="chat-ops",
    )
    # Реакции: polling ``reaction_ops_queue`` с direction=to_max.
    # Воркер сам фильтрует (API возвращает только ``to_max``). Ошибки
    # внутри не валят supervisor — они логируются и задача помечается
    # ``failed`` (см. ``reactions_loop``).
    reactions_task = asyncio.create_task(
        reactions.reactions_loop(stop_event), name="reactions",
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
                await sleep_with_stop(stop_event, wait)
                if stop_event.is_set():
                    break
                continue
            elif rate_limit_until > 0 and now >= rate_limit_until:
                logger.info("rate-limit cooldown elapsed, resuming")
                rate_limit_until = 0.0
                shared_db.set_auth_state("auth_required", error=None, clear_error=True)

            # 1) Текущее состояние
            auth = shared_db.get_auth_state()
            status = auth.get("status") or "unknown"
            session_candidates = find_session_files(cache_dir)
            has_session_file = bool(session_candidates)
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

            # 3) Client жив, но статус ещё не ok
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
                # Публикуем факт смерти Client'а в ``chat_ops``,
                # чтобы ``chat_ops_loop`` приостановил обработку.
                chat_ops.clear_client()

                if exc is not None:
                    logger.warning("client_task exited with error: %s", exc)
                    if is_rate_limit_error(exc):
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
                        await sleep_with_stop(stop_event, RATE_LIMIT_BACKOFF)
                        if stop_event.is_set():
                            break
                        continue
                    else:
                        shared_db.set_auth_state(
                            "auth_required", error=str(exc), clear_error=False,
                        )
                        shared_db.set_notify_message(
                            f"❌ Ошибка клиента MAX: {exc}. "
                            f"Мост возвращён в режим ожидания команды."
                        )
                        await sleep_with_stop(stop_event, AUTH_FAIL_BACKOFF)
                        if stop_event.is_set():
                            break
                        continue
                else:
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

            # 5) Client'а нет, status=ok — не пересоздаём автоматически
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
                    await sleep_with_stop(stop_event, max(0.0, wait))
                    if stop_event.is_set():
                        break
                    continue
                last_sms_request_at = time.monotonic()

                logger.info("owner requested SMS auth; wiping cache and starting client")
                wipe_cache(cache_dir)
                shared_db.set_auth_state("unknown", error=None, clear_error=True)
                shared_db.set_notify_message("🔐 Запрашиваю SMS-код у MAX…")
                client = build_client(phone, cache_dir)
                chat_ops.set_client(client)
                reactions.set_client(client)
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
                chat_ops.set_client(client)
                reactions.set_client(client)
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

            logger.warning("unknown pending_action=%r, ignoring", action)
            continue

    finally:
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
        if read_receipts_task is not None and not read_receipts_task.done():
            read_receipts_task.cancel()
            try:
                await read_receipts_task
            except (asyncio.CancelledError, Exception):
                pass
        if chat_ops_task is not None and not chat_ops_task.done():
            chat_ops_task.cancel()
            try:
                await chat_ops_task
            except (asyncio.CancelledError, Exception):
                pass
        if reactions_task is not None and not reactions_task.done():
            reactions_task.cancel()
            try:
                await reactions_task
            except (asyncio.CancelledError, Exception):
                pass
        chat_ops.clear_client()
        reactions.clear_client()
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass
