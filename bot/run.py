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
from shared.log_setup import configure_logging

logger = logging.getLogger(__name__)


async def main() -> None:
    configure_logging(settings.log_level)
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

    poller = EventPoller(
        bot=bot,
        target_chat_id=settings.allowed_tg_user_ids[0] if settings.allowed_tg_user_ids else 0,
        poll_interval=2.0,
    )

    auth_watcher = AuthWatcher(bot=bot)

    try:
        if poller.target_chat_id:
            await poller.start()
        auth_watcher.start()
        await dp.start_polling(bot)
    finally:
        await poller.stop()
        await auth_watcher.stop()
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