"""Общая загрузка конфигурации из окружения (этап 2, PyMax)."""

from __future__ import annotations

import os
import logging
from dataclasses import dataclass, field
from typing import List, Optional


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    val = os.getenv(name)
    if val is None or val == "":
        return default
    return val


def _env_int(name: str, default: int) -> int:
    val = _env(name)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    val = _env(name)
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        return default


def _env_list(name: str, default: List[str] | None = None) -> List[str]:
    val = _env(name)
    if not val:
        return list(default or [])
    return [item.strip() for item in val.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    # Telegram
    telegram_bot_token: str = ""
    allowed_tg_user_ids: List[int] = field(default_factory=list)

    # MAX (PyMax)
    max_phone: str = ""
    cache_dir: str = "/data/cache"  # PyMax session + sqlite cache

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    db_path: str = "/data/bridge.db"
    media_dir: str = "/data/media"
    bridge_api_key: str = ""

    # Misc
    log_level: str = "INFO"
    pymax_log_level: str = "INFO"
    pymax_reconnect_delay: float = 2.0


def load_settings() -> Settings:
    """Загружает настройки и применяет безопасные дефолты."""

    api_key = _env("BRIDGE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "BRIDGE_API_KEY is not set. Set it in your environment (e.g. "
            "`openssl rand -hex 32`) before starting the bridge."
        )
    # Подавляем варнинг у shutdown-логов: ключ провалидирован выше.
    _ = logging.getLogger(__name__).warning

    return Settings(
        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN", "") or "",
        allowed_tg_user_ids=[
            int(x) for x in _env_list("ALLOWED_TG_USER_IDS") if x.isdigit()
        ],
        max_phone=_env("MAX_PHONE", "") or "",
        cache_dir=_env("CACHE_DIR", "/data/cache") or "/data/cache",
        api_host=_env("API_HOST", "0.0.0.0") or "0.0.0.0",
        api_port=_env_int("API_PORT", 8000),
        db_path=_env("DB_PATH", "/data/bridge.db") or "/data/bridge.db",
        media_dir=_env("MEDIA_DIR", "/data/media") or "/data/media",
        bridge_api_key=api_key,
        log_level=_env("LOG_LEVEL", "INFO") or "INFO",
        pymax_log_level=_env("PYMAX_LOG_LEVEL", "INFO") or "INFO",
        pymax_reconnect_delay=_env_float("PYMAX_RECONNECT_DELAY", 2.0),
    )