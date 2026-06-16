"""Точка входа api-процесса для supervisord."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Подключаем /app и /app/shared как путь импорта
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "api"))

# Сначала настраиваем логирование, затем импортируем app
from shared.log_setup import configure_logging  # noqa: E402
from shared.config import load_settings  # noqa: E402

settings = load_settings()
configure_logging(settings.log_level)

import uvicorn  # noqa: E402

# main.py создаёт FastAPI app при импорте
from api.main import app  # noqa: E402,F401  (re-export for clarity)

logger = logging.getLogger(__name__)


def main() -> None:
    uvicorn.run(
        "api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()