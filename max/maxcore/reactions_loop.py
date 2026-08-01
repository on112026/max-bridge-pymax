"""Polling-цикл MAX-процесса для реакций (направление ``to_max``).

Зеркалит реакции владельца моста из Telegram в MAX:

* ``to_max`` — задача ``add_reaction`` / ``remove_reaction``, поставленная
  ботом после ``MessageReactionUpdated``. MAX-процесс берёт её через
  :func:`shared.db.claim_next_reaction_op("to_max")`, выполняет
  ``client.add_reaction`` / ``client.remove_reaction`` и помечает
  ``done``/``failed``.

Направления ``to_tg`` и ``to_tg_summary`` MAX-процесс не обрабатывает —
ими занимается бот (см. ``bot/app/handlers/reactions_max.py``).

Регистрируется в ``max/run.py`` через supervisor (``max/app/supervisor/__init__.py``).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


POLL_INTERVAL = 2.0  # секунд между опросами
READY_CHECK_INTERVAL = 2.0
API_BASE = os.getenv("API_BASE_URL", "http://localhost:8000")
API_KEY = os.getenv("BRIDGE_API_KEY", "")


_client_holder: dict = {"client": None}


def set_client(client) -> None:
    """Зарегистрировать живой ``Client`` (вызывается из supervisor)."""
    _client_holder["client"] = client


def clear_client() -> None:
    """Сбросить ссылку на Client (при рестарте / остановке)."""
    _client_holder["client"] = None


def _get_client():
    return _client_holder["client"]


def _headers() -> dict:
    return {"X-Api-Key": API_KEY}


async def _claim_next() -> Optional[dict]:
    try:
        async with httpx.AsyncClient(base_url=API_BASE, timeout=10.0) as c:
            r = await c.get(
                "/reaction_ops/next",
                params={"direction": "to_max"},
                headers=_headers(),
            )
            if r.status_code == 200 and r.content:
                return r.json()
    except Exception as exc:
        logger.warning("reactions_loop._claim_next failed: %s", exc)
    return None


async def _finish(item_id: int, ok: bool, error: Optional[str] = None) -> None:
    try:
        async with httpx.AsyncClient(base_url=API_BASE, timeout=10.0) as c:
            r = await c.post(
                f"/reaction_ops/{item_id}/finish",
                json={"ok": ok, "error": error},
                headers=_headers(),
            )
            if r.status_code >= 400:
                logger.warning(
                    "reactions_loop._finish id=%s failed: %s %s",
                    item_id, r.status_code, r.text[:200],
                )
    except Exception as exc:
        logger.warning("reactions_loop._finish id=%s exception: %s", item_id, exc)


async def _apply(client, item: dict) -> None:
    """Применить одну задачу ``to_max`` через ``pymax.Client``."""
    op = (item.get("op") or "").lower()
    chat_id_raw = item.get("max_chat_id")
    msg_id_raw = item.get("max_message_id")
    emoji = item.get("emoji")
    if not chat_id_raw or not msg_id_raw:
        raise ValueError("max_chat_id / max_message_id missing")
    try:
        chat_id = int(chat_id_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid max_chat_id: {chat_id_raw!r}") from exc
    try:
        message_id = str(msg_id_raw)  # PyMax add_reaction ждёт str (см. :C в message.py)
    except Exception as exc:
        raise ValueError(f"invalid max_message_id: {msg_id_raw!r}") from exc

    if op == "add":
        if not emoji:
            raise ValueError("emoji is required for op=add")
        await client.add_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=str(emoji),
        )
        logger.info(
            "reactions_loop: added %s on chat=%s msg=%s",
            emoji, chat_id_raw, msg_id_raw,
        )
        return
    if op == "remove":
        await client.remove_reaction(
            chat_id=chat_id,
            message_id=message_id,
        )
        logger.info(
            "reactions_loop: removed reaction on chat=%s msg=%s",
            chat_id_raw, msg_id_raw,
        )
        return
    if op == "fetch_summary":
        # Пользователь нажал «🔄 Реакции» в топике. Делаем свежий
        # ``get_reactions`` в MAX и кладём ``to_tg_summary`` — воркер
        # ``ReactionsMaxPoller`` обновит сводку под сообщением.
        reactions_map = await client.get_reactions(
            chat_id=chat_id,
            message_ids=[message_id],
        )
        info = (reactions_map or {}).get(message_id)
        counters = (
            [
                {
                    "reaction": getattr(c, "reaction", "?"),
                    "count": int(getattr(c, "count", 0)),
                }
                for c in (getattr(info, "counters", None) or [])
            ]
            if info is not None
            else []
        )
        total = int(getattr(info, "total_count", 0) or 0) if info else 0
        await _post_summary_update(
            max_chat_id=chat_id_raw,
            max_message_id=msg_id_raw,
            counters=counters,
            total_count=total,
        )
        logger.info(
            "reactions_loop: fetch_summary for chat=%s msg=%s → %d emoji, total=%d",
            chat_id_raw, msg_id_raw, len(counters), total,
        )
        return
    raise ValueError(f"unsupported op for to_max: {op!r}")


async def _post_summary_update(
    max_chat_id: str,
    max_message_id: str,
    counters: list,
    total_count: int,
) -> None:
    """Положить ``to_tg_summary`` задачу после свежего ``get_reactions``."""
    import json
    payload = {
        "direction": "to_tg_summary",
        "op": "summary_update",
        "max_chat_id": max_chat_id,
        "max_message_id": max_message_id,
        "counters_json": json.dumps(counters, ensure_ascii=False),
        "total_count": int(total_count),
    }
    try:
        async with httpx.AsyncClient(base_url=API_BASE, timeout=10.0) as c:
            r = await c.post(
                "/reaction_ops",
                json=payload,
                headers=_headers(),
            )
            if r.status_code >= 400:
                logger.warning(
                    "reactions_loop._post_summary_update failed: %s %s",
                    r.status_code, r.text[:200],
                )
    except Exception as exc:
        logger.warning(
            "reactions_loop._post_summary_update exception: %s", exc,
        )


async def _wait_or_stop(stop_event: asyncio.Event, timeout: float) -> bool:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


async def reactions_loop(stop_event: asyncio.Event) -> None:
    """Основной polling-цикл MAX-процесса для направления ``to_max``.

    Задачи ``to_tg`` / ``to_tg_summary`` сюда не приходят (фильтр на
    стороне API через ``?direction=to_max``) и обрабатываются ботом.
    """
    logger.info("reactions_loop started (poll=%.1fs)", POLL_INTERVAL)
    while not stop_event.is_set():
        try:
            client = _get_client()
            if client is None:
                # Client ещё не поднят (``auth_required`` / перезапуск).
                await _wait_or_stop(stop_event, READY_CHECK_INTERVAL)
                continue

            item = await _claim_next()
            if item is None:
                await _wait_or_stop(stop_event, POLL_INTERVAL)
                continue

            item_id = item.get("id")
            if not item_id:
                logger.warning("reactions_loop: malformed item=%r, skipping", item)
                await _wait_or_stop(stop_event, POLL_INTERVAL)
                continue

            logger.info(
                "reactions_loop: claimed id=%s op=%s chat=%s msg=%s emoji=%s",
                item_id, item.get("op"), item.get("max_chat_id"),
                item.get("max_message_id"), item.get("emoji"),
            )
            try:
                await _apply(client, item)
                await _finish(item_id, ok=True)
            except Exception as exc:
                logger.warning(
                    "reactions_loop: id=%s op=%s FAILED: %s",
                    item_id, item.get("op"), exc,
                )
                await _finish(item_id, ok=False, error=str(exc))

        except Exception as exc:
            logger.warning("reactions_loop tick error: %s", exc)

        await _wait_or_stop(stop_event, POLL_INTERVAL)