"""Фоновая задача «read receipts» — пометка прочитанных сообщений в MAX.

Когда пользователь в TG-боте делает любое действие (``REPLY``, ``SHOWID``,
ввод текста через ``/reply`` и т.п.), бот вызывает ``POST /chats/{id}/read-up-to``
→ ``chat_read_state.last_read_at`` обновляется.

MAX-процесс (этот код) периодически:

1. Забирает ``GET /chats/pending-reads`` — список доставленных сообщений
   (``delivered_messages.delivered_at <= chat.last_read_at``).
2. Для каждого вызывает ``client.read_message(chat_id, message_id)``.
3. На успехе — ``POST /chats/{chat}/messages/{mid}/read``.

Адаптивный backoff: при последовательных ошибках увеличиваем интервал
опроса (3с → 30с → 600с), чтобы не спамить MAX-сервер proto.payload-ошибками
и не ловить rate-limit. Сброс счётчика при первом успехе.

``client_getter`` — callable, возвращающий текущий ``Client`` (или
``None``, если Client ещё не поднят). Это позволяет не пересоздавать
Client при рестарте: supervisor читает ``client`` из своей переменной
через замыкание.
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx

from app.supervisor._backoff import READ_RECEIPTS_INTERVAL

logger = logging.getLogger(__name__)


async def _claim_pending_reads() -> list:
    """Забрать из API список доставленных сообщений, прочитанных в TG."""
    api_base = os.getenv("API_BASE_URL", "http://localhost:8000")
    api_key = os.getenv("BRIDGE_API_KEY", "")
    try:
        async with httpx.AsyncClient(
            base_url=api_base,
            headers={"X-Api-Key": api_key},
            timeout=15.0,
        ) as c:
            r = await c.get("/chats/pending-reads")
            r.raise_for_status()
            data = r.json() if r.content else []
            return list(data or [])
    except Exception as exc:
        logger.warning("claim_pending_reads failed: %s", exc)
        return []


async def _mark_message_read(
    delivered_id: int, max_chat_id: str, max_message_id: str
) -> None:
    """После успешного ``client.read_message`` пометить запись как прочитанную."""
    api_base = os.getenv("API_BASE_URL", "http://localhost:8000")
    api_key = os.getenv("BRIDGE_API_KEY", "")
    try:
        async with httpx.AsyncClient(
            base_url=api_base,
            headers={"X-Api-Key": api_key},
            timeout=10.0,
        ) as c:
            r = await c.post(
                f"/chats/{max_chat_id}/messages/{max_message_id}/read",
                params={"delivered_id": str(delivered_id)},
            )
            if r.status_code >= 400:
                logger.warning(
                    "mark_message_read chat=%s msg=%s failed: %s %s",
                    max_chat_id, max_message_id, r.status_code, r.text[:200],
                )
    except Exception as exc:
        logger.warning(
            "mark_message_read chat=%s msg=%s exception: %s",
            max_chat_id, max_message_id, exc,
        )


async def _wait_or_stop(stop_event, timeout: float) -> bool:
    """Ждать ``stop_event`` или таймаут. Возвращает ``True``, если пришёл stop."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


async def read_receipts_loop(stop_event, client_getter) -> None:
    """Периодически опрашивает API, вызывает ``client.read_message``
    и помечает успех в API.
    """
    logger.info(
        "read_receipts_loop started (poll=%.1fs)", READ_RECEIPTS_INTERVAL,
    )
    consecutive_errors = 0
    while not stop_event.is_set():
        try:
            client = client_getter()
            if client is None:
                # Client ещё не поднят (auth_required / session_attached).
                await _wait_or_stop(stop_event, READ_RECEIPTS_INTERVAL)
                continue

            receipts = await _claim_pending_reads()
            if not receipts:
                consecutive_errors = 0
                await _wait_or_stop(stop_event, READ_RECEIPTS_INTERVAL)
                continue

            logger.info(
                "read_receipts_loop: %d pending receipts to mark", len(receipts),
            )
            had_error_in_batch = False
            for r in receipts:
                if stop_event.is_set():
                    break
                chat_id_str = (r.get("max_chat_id") or "").strip()
                msg_id_str = (r.get("max_message_id") or "").strip()
                delivered_id = int(r.get("id") or 0)
                if not chat_id_str or not msg_id_str:
                    logger.warning(
                        "read_receipts_loop: skip receipt with empty chat_id/msg_id: %r",
                        r,
                    )
                    continue
                try:
                    chat_id_int = int(chat_id_str)
                except ValueError:
                    logger.warning(
                        "read_receipts_loop: cannot convert chat_id=%r to int",
                        chat_id_str,
                    )
                    continue
                try:
                    msg_id_int = int(msg_id_str)
                except ValueError:
                    logger.warning(
                        "read_receipts_loop: cannot convert message_id=%r to int, "
                        "marking as read in API to avoid retry",
                        msg_id_str,
                    )
                    # Чтобы не зацикливаться на невалидном id — помечаем как прочитанное.
                    await _mark_message_read(
                        delivered_id=delivered_id,
                        max_chat_id=chat_id_str,
                        max_message_id=msg_id_str,
                    )
                    continue
                try:
                    # PyMax: client.read_message(message_id, chat_id) -> ReadState
                    # Важно: message_id — int (см. ошибку "Expected number at 42").
                    await client.read_message(
                        message_id=msg_id_int, chat_id=chat_id_int,
                    )
                    logger.info(
                        "read_message ok: chat=%s msg=%s",
                        chat_id_str, msg_id_str,
                    )
                    await _mark_message_read(
                        delivered_id=delivered_id,
                        max_chat_id=chat_id_str,
                        max_message_id=msg_id_str,
                    )
                except Exception as exc:
                    logger.warning(
                        "read_message FAILED chat=%s msg=%s: %s",
                        chat_id_str, msg_id_str, exc,
                    )
                    had_error_in_batch = True
                    # Не помечаем как прочитанное — попробуем в следующем тике.

            if had_error_in_batch:
                consecutive_errors += 1
            else:
                consecutive_errors = 0

            if consecutive_errors <= 3:
                current_interval = READ_RECEIPTS_INTERVAL  # 3s
            elif consecutive_errors <= 5:
                current_interval = 30.0
            else:
                current_interval = 600.0  # 10 минут
            if consecutive_errors in (4, 6):
                logger.warning(
                    "read_receipts_loop: %d consecutive errors, backing off to %.0fs",
                    consecutive_errors, current_interval,
                )
        except Exception as exc:
            logger.warning("read_receipts_loop tick error: %s", exc)
            consecutive_errors += 1
            if consecutive_errors <= 3:
                current_interval = READ_RECEIPTS_INTERVAL
            elif consecutive_errors <= 5:
                current_interval = 30.0
            else:
                current_interval = 600.0

        await _wait_or_stop(stop_event, current_interval)