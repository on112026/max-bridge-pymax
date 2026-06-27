"""Backoff-параметры и хелперы для supervisor'а MAX-процесса.

Главная цель backoff'ов — снизить нагрузку на ``api.oneme.ru`` и уйти
от ``error.limit.violate``. Каждый параметр задокументирован, потому что
«правильные» числа подбирались эмпирически по логам.
"""

from __future__ import annotations

import asyncio
from typing import Optional


# ---------- Backoff-параметры ----------

# Основной интервал между итерациями supervisor'а (в нормальном режиме).
NORMAL_POLL_SECONDS = 30.0

# Пауза после неудачного client.start() (auth/phone/code).
AUTH_FAIL_BACKOFF = 60.0

# Пауза при rate-limit (error.limit.violate) — 10 минут.
RATE_LIMIT_BACKOFF = 600.0

# Минимальный интервал между запросами нового SMS-кода в секундах.
# Каждый новый Client = новый запрос SMS к api.oneme.ru → rate-limit.
SMS_RESEND_COOLDOWN = 900.0  # 15 минут

# Интервал опроса system_state на предмет введённого кода/пароля.
# Должен быть <= POLL_INTERVAL в QueueSmsCodeProvider/QueuePasswordProvider
# (1.5s), иначе провайдер начнёт сам забирать код через /auth/2fa/peek
# раньше, чем notify_code_received успеет разбудить ev.wait().
CODE_DRAIN_INTERVAL = 0.5

# Интервал опроса session-файла на диске.
SESSION_WATCH_INTERVAL = 5.0

# Интервал опроса API для пометки прочитанных сообщений в MAX.
READ_RECEIPTS_INTERVAL = 3.0


# ---------- Распознавание ошибок ----------


def is_rate_limit_error(exc: BaseException) -> bool:
    """Распознаём «error.limit.violate» в тексте исключения."""
    msg = str(exc).lower()
    return "limit" in msg or "too many" in msg or "ratelimit" in msg or "429" in msg


def is_auth_error(exc: BaseException) -> bool:
    """Распознаём auth-ошибки (phone/code/sms/password) в тексте исключения."""
    msg = str(exc).lower()
    return any(s in msg for s in ("auth", "phone", "code", "sms", "password"))


# ---------- Утилиты ----------

async def sleep_with_stop(stop_event: asyncio.Event, seconds: float) -> None:
    """Спим ``seconds``, но выходим раньше, если взведён ``stop_event``.

    Используется во всех фоновых задачах supervisor'а, чтобы они корректно
    завершались при ``stop_event.set()``.
    """
    if seconds <= 0:
        return
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return