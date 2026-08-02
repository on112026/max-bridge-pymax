"""Единая точка входа max-bridge-pymax: api + bot + max в одном процессе.

Раньше (этап 2) три процесса — ``api`` (FastAPI/uvicorn), ``bot``
(aiogram) и ``max`` (PyMax-клиент) — крутились через ``supervisord``
как отдельные Python-процессы. Это утраивало базовый memory-footprint
(каждый процесс отдельно грузил SQLAlchemy/pydantic/httpx) и требовало
HTTP-round-trip'ов между процессами даже для внутренних вызовов.

Этот файл запускает все три как conftask'и одного asyncio event loop'а
в одном интерпретаторе. Каждая обёрнута в ``_supervise()`` — аналог
``autorestart=true`` из supervisord: если задача падает с исключением,
она перезапускается через ``RESTART_DELAY`` секунд, а не роняет весь
процесс.

Контракты между api/bot/max (HTTP на localhost, общая SQLite через
``shared.db``) НЕ менялись — это тот же код, что и раньше, просто
исполняется в одном процессе вместо трёх. ``api/run.py``, ``bot/run.py``,
``max/run.py`` по-прежнему рабочие entrypoint'ы и остаются на месте —
их можно запускать по отдельности для локальной отладки одного сервиса
(см. ``docs/deployment.md``).

Важно про namespace-коллизию: у ``bot`` и ``max`` были одинаковые
top-level пакеты ``app`` — в одном интерпретаторе (один ``sys.modules``)
это несовместимо. Поэтому пакет ``max/app`` переименован в
``max/maxcore`` (см. историю изменений); ``bot/app`` остался как есть.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Тот же набор путей, что раньше выставлял supervisord по отдельности
# для каждого процесса (PYTHONPATH=/app:/app/shared:/app/<proc>:/app/vendor),
# теперь всё сразу в одном интерпретаторе.
for _sub in ("shared", "api", "bot", "max", "vendor"):
    sys.path.insert(0, str(ROOT / _sub))

from shared.config import load_settings  # noqa: E402
from shared.log_setup import configure_logging  # noqa: E402
from shared import db as shared_db  # noqa: E402

settings = load_settings()
configure_logging(settings.log_level)
shared_db.init_engine(settings.db_path)

logger = logging.getLogger("run_all")

RESTART_DELAY = 5.0


async def _supervise(name: str, coro_factory, restart_delay: float = RESTART_DELAY) -> None:
    """Перезапускает ``coro_factory()`` при падении — аналог supervisord autorestart.

    ``CancelledError`` пробрасывается наружу без перезапуска — это
    сигнал штатного shutdown'а (см. ``main()``), а не сбой.
    """
    while True:
        try:
            logger.info("[%s] starting", name)
            await coro_factory()
            logger.warning(
                "[%s] exited cleanly (unexpected for a long-running service), "
                "restarting in %.1fs", name, restart_delay,
            )
        except asyncio.CancelledError:
            logger.info("[%s] stopping (shutdown)", name)
            raise
        except Exception:
            logger.exception("[%s] crashed, restarting in %.1fs", name, restart_delay)
        await asyncio.sleep(restart_delay)


async def _run_api() -> None:
    """FastAPI/uvicorn — та же ``api.main:app``, что и раньше в отдельном процессе."""
    import uvicorn
    from api.main import app as fastapi_app

    config = uvicorn.Config(
        fastapi_app,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    server = uvicorn.Server(config)
    # По умолчанию uvicorn сам ставит обработчики SIGINT/SIGTERM на весь
    # процесс — конфликтует с нашим собственным shutdown'ом в main().
    # Отключаем, шатдауном рулит main() через отмену задачи.
    server.install_signal_handlers = lambda: None
    await server.serve()


async def _run_bot() -> None:
    """Telegram-бот (aiogram) — переиспользует ``bot/run.py::main()`` как есть."""
    from bot.run import main as bot_main

    await bot_main()


async def _run_max() -> None:
    """PyMax-клиент + sender/chat_ops циклы — переиспользует ``max/run.py::main()``."""
    from max.run import main as max_main

    await max_main()


async def main() -> None:
    logger.info("bridge starting: api + bot + max in one process")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # pragma: no cover - не Linux/Docker
            pass

    tasks = [
        asyncio.create_task(_supervise("api", _run_api), name="svc-api"),
        asyncio.create_task(_supervise("bot", _run_bot), name="svc-bot"),
        asyncio.create_task(_supervise("max", _run_max), name="svc-max"),
    ]

    # Render.com: периодическая синхронизация SQLite + сессии → Cloudflare D1
    # Активируется если заданы CF_ACCOUNT_ID, CF_API_TOKEN, CF_D1_DATABASE_ID
    import os as _os
    if all(_os.environ.get(k) for k in ("CF_ACCOUNT_ID", "CF_API_TOKEN", "CF_D1_DATABASE_ID")):
        try:
            sys.path.insert(0, str(ROOT))
            from render_d1_sync import d1_sync_loop
            tasks.append(
                asyncio.create_task(d1_sync_loop(stop_event), name="svc-d1-sync")
            )
            logger.info("render d1-sync task started")
        except ImportError:
            logger.warning("render_d1_sync not found, d1 sync disabled")

    stopper = asyncio.create_task(stop_event.wait(), name="stop-waiter")

    done, _pending = await asyncio.wait(
        [*tasks, stopper], return_when=asyncio.FIRST_COMPLETED
    )

    if stopper in done:
        logger.info("shutdown signal received, stopping api/bot/max")
    else:
        # Один из supervised task'ов сам завершился без исключения —
        # такого быть не должно (они бесконечные циклы), но на всякий
        # случай тоже гасим остальные, а не оставляем процесс в подвисшем
        # состоянии с одним живым сервисом из трёх.
        logger.warning("one of api/bot/max tasks exited unexpectedly, shutting down")

    stopper.cancel()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, stopper, return_exceptions=True)
    logger.info("bridge stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
