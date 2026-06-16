"""Entrypoint max-процесса (PyMax client + sender) для supervisord."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

# Подключаем /app/shared, /app/max
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "vendor"))
sys.path.insert(0, str(ROOT / "max"))

# Экспортируем ключевые переменные для auth.py и sender.py
os.environ.setdefault("API_BASE_URL", "http://localhost:8000")
os.environ.setdefault("MEDIA_DIR", "/data/media")
os.environ.setdefault("CACHE_DIR", "/app/cache")

from shared.config import load_settings  # noqa: E402
from shared.log_setup import configure_logging  # noqa: E402

settings = load_settings()
# Дублируем настройки в ENV, чтобы подпроцессы/модули видели
os.environ["MAX_PHONE"] = settings.max_phone
os.environ["BRIDGE_API_KEY"] = settings.bridge_api_key
os.environ["MEDIA_DIR"] = settings.media_dir
os.environ["CACHE_DIR"] = settings.cache_dir
os.environ["API_BASE_URL"] = f"http://localhost:{settings.api_port}"

configure_logging(settings.log_level)

from app import supervisor  # noqa: E402

logger = logging.getLogger(__name__)


async def main() -> None:
    logger.info("max process starting (phone=%s, cache=%s)", settings.max_phone, settings.cache_dir)
    await supervisor.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("max process stopped")
    except Exception:
        logger.exception("max process crashed")
        sys.exit(1)