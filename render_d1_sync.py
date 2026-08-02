"""render_d1_sync.py — фоновая синхронизация SQLite + сессии в Cloudflare D1.

Запускается как asyncio-задача внутри run_all.py, если заданы
CF_ACCOUNT_ID, CF_API_TOKEN, CF_D1_DATABASE_ID.

Что делает:
  - Каждые D1_SYNC_INTERVAL секунд (по умолчанию 5 мин):
      * online-бэкап bridge.db → D1 (key='db')
      * копия файла сессии PyMax → D1 (key='session')
  - При завершении (SIGTERM/SIGINT) — финальная синхронизация.
  - Не мешает работе приложения: sqlite3.backup() не блокирует транзакции.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

D1_SYNC_INTERVAL = int(os.environ.get("D1_SYNC_INTERVAL", "300"))  # 5 минут


def _sync_once() -> tuple[bool, bool]:
    """Синхронизировать БД и сессию в D1. Возвращает (ok_db, ok_session)."""
    from render_d1 import D1Store

    store = D1Store.from_env()
    db_path = Path(os.environ.get("DB_PATH", "/data/bridge.db"))
    cache_dir = Path(os.environ.get("CACHE_DIR", "/data/cache"))
    # PyMax сохраняет сессию как CACHE_DIR/bridge (без .db)
    session_path = next(
        (p for p in (cache_dir / "bridge", cache_dir / "bridge.db") if p.exists()),
        None,
    )

    ok_db = store.push_db(db_path) if db_path.exists() else False
    ok_session = store.push_blob("session", session_path) if session_path else False

    return ok_db, ok_session


async def d1_sync_loop(stop_event: asyncio.Event) -> None:
    """Фоновый цикл синхронизации. Вызывается из run_all.py."""
    logger.info("d1_sync: started (interval=%ds)", D1_SYNC_INTERVAL)

    # Ждём пока приложение полностью инициализируется
    await asyncio.sleep(60)

    while not stop_event.is_set():
        try:
            ok_db, ok_session = await asyncio.to_thread(_sync_once)
            logger.info(
                "d1_sync: db=%s session=%s",
                "✓" if ok_db else "✗",
                "✓" if ok_session else "✗",
            )
        except Exception as e:
            logger.warning("d1_sync: ошибка синхронизации: %s", e)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=D1_SYNC_INTERVAL)
        except asyncio.TimeoutError:
            pass

    # Финальная синхронизация при shutdown
    logger.info("d1_sync: shutdown — финальная синхронизация")
    try:
        ok_db, ok_session = await asyncio.to_thread(_sync_once)
        logger.info("d1_sync: final db=%s session=%s", "✓" if ok_db else "✗", "✓" if ok_session else "✗")
    except Exception as e:
        logger.error("d1_sync: финальная синхронизация упала: %s", e)

    logger.info("d1_sync: stopped")
