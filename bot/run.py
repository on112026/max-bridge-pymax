"""Entrypoint Telegram-бота (этап 2, PyMax)."""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import suppress
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "bot"))

from aiogram import Bot, Dispatcher

from app.api_client import api
from app.config import settings
from app.forwarder import EventPoller
from app.handlers import AuthWatcher, register_handlers
from app.topic_worker import TopicSyncWorker
from shared import db as shared_db
from shared.log_setup import configure_logging

logger = logging.getLogger(__name__)


async def main() -> None:
    configure_logging(settings.log_level)
    # Инициализируем DB engine в bot-процессе (та же БД ``settings.db_path``,
    # которую использует api-процесс и max-процесс). Без этого
    # ``shared_db.*`` в ``handlers.py`` (``get_supergroup_for_owner``,
    # ``create_supergroup``, ``update_supergroup_invite_link``) и в
    # ``forwarder.py`` (``get_supergroup_for_owner``) падают с
    # ``RuntimeError: DB engine не инициализирован``.
    # ``init_engine`` идемпотентна (``if _engine is not None: return``).
    shared_db.init_engine(settings.db_path)
    token = settings.telegram_bot_token
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is empty")
    if not settings.allowed_tg_user_ids:
        logger.warning(
            "ALLOWED_TG_USER_IDS пуст — бот не будет отвечать никому (fail-closed)."
        )

    bot = Bot(token=token)
    dp = Dispatcher()
    register_handlers(dp)

    # Владелец — первый (и пока единственный) user_id из ALLOWED_TG_USER_IDS.
    # EventPoller использует его для lookup supergroup (создаётся через /setup)
    # и для создания/поиска топика для каждого MAX-чата.
    owner_uid = (
        settings.allowed_tg_user_ids[0] if settings.allowed_tg_user_ids else 0
    )

    poller = EventPoller(
        bot=bot,
        owner_user_id=owner_uid,
        poll_interval=2.0,
    )

    auth_watcher = AuthWatcher(bot=bot)

    # Воркер синхронизации топиков (создание/переименование после auth=ok).
    # Всегда запускаем — если владелец ещё не сделал /setgroup, воркер
    # просто будет возвращать джобы в pending (см. ``TopicSyncWorker``).
    topic_worker = TopicSyncWorker(bot=bot)

    try:
        if owner_uid:
            await poller.start()
        auth_watcher.start()
        topic_worker.start()
        await dp.start_polling(bot)
    finally:
        await poller.stop()
        await auth_watcher.stop()
        await topic_worker.stop()
        with suppress(Exception):
            await api.close()
        with suppress(Exception):
            await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
    except Exception:
        logger.exception("Bot crashed")
        sys.exit(1)