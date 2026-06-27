"""Фоновая задача «drain 2FA-кодов» из ``system_state``.

Зачем нужен drain: PyMax SmsAuthFlow через ``QueueSmsCodeProvider`` /
``QueuePasswordProvider`` блокируется на ``asyncio.Event`` до тех пор,
пока владелец не введёт код/пароль через бота (``/code``). Бот кладёт
значение в ``system_state`` через ``POST /auth/2fa`` (``db.put_2fa_code``).

Проблема: провайдер не «знает», что код уже лежит в БД — он ждёт
локального ``notify_code_received``. Поэтому supervisor запускает эту
фоновую задачу, которая раз в ``CODE_DRAIN_INTERVAL`` секунд:

1. Берёт список ``request_id`` из ``system_state`` (``list_2fa_code_keys``).
2. Для каждого нового ``rid`` вызывает ``notify_code_received(rid, None)``,
   что будит ``asyncio.Event`` в ``app.auth._EVENTS``.
3. Сам код провайдер забирает сам через ``GET /auth/2fa/peek/{rid}``
   и ``db.take_pending_2fa_code``.

Без drain'а провайдер висел бы на ``ev.wait()`` до 10-минутного таймаута,
PyMax падал бы, supervisor уходил в 15-минутный sms-cooldown, и до 2FA-пароля
очередь не доходила — мост был бы сломан.
"""

from __future__ import annotations

import logging

from app.auth import notify_code_received
from app.supervisor._backoff import CODE_DRAIN_INTERVAL, sleep_with_stop
from shared import db as shared_db

logger = logging.getLogger(__name__)


# Локальный set rid'ов, которые мы уже отдали в notify_code_received.
# Нужен, чтобы не делать notify повторно (значение в system_state уже
# забрано take_pending_2fa_code, а вот rid из auth_state может жить дольше).
_ALREADY_NOTIFIED: set[int] = set()


async def drain_2fa_codes_loop(stop_event) -> None:
    """Фоновая задача: следит за появлением 2fa_code:<rid> в ``system_state``."""
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
        await sleep_with_stop(stop_event, CODE_DRAIN_INTERVAL)