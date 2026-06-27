"""Логика ``on_start`` колбэка PyMax Client.

Вызывается при успешном старте Client'а:

1. Ставит ``auth_state.status=ok`` через ``POST /auth/state``.
2. Синхронизирует список чатов через ``fetch_chats``.
3. Обогащает ``title`` для чатов без имени (``enrich_chat_titles``).
4. Поэлементно кладёт каждый чат в ``POST /chats``.
5. Дёргает ``POST /internal/sync_topics`` — API сравнит свежий список
   с уже существующими ``ChatTopic``, пометит пропавшие как ``stale=1``
   и поставит в очередь ``create``/``rename``-джобы для бота.

Если первый ``fetch_chats`` вернул пустой список (например, MAX ещё не
успел прогреть кеш после login/sync) — пробуем ещё раз через 5 секунд.
Без этого на свежей сессии можно получить «0 чатов» и не создать ни
одного топика.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.bridge.chats import (
    chat_to_dict,
    display_name_of,
    enrich_chat_titles,
)
from shared.config import load_settings

logger = logging.getLogger(__name__)


# Константы окружения — те же, что в ``bridge.py`` (старом).
API_BASE = "http://localhost:8000"
API_KEY = ""
MEDIA_DIR = "/data/media"

# Подгружаем реальные значения из ``shared.config``.
_settings = load_settings()
API_BASE = f"http://localhost:{_settings.api_port}"
API_KEY = _settings.bridge_api_key
MEDIA_DIR = _settings.media_dir


def _headers() -> dict:
    return {"X-Api-Key": API_KEY}


async def _post(path: str, json: dict = None) -> None:
    """POST в API без проверки ответа (best-effort)."""
    try:
        async with httpx.AsyncClient(base_url=API_BASE, timeout=30.0) as c:
            r = await c.post(path, json=json or {}, headers=_headers())
            r.raise_for_status()
    except Exception as exc:
        logger.warning("api POST %s failed: %s", path, exc)


async def _fetch_chats(client) -> list:
    """Синхронизация списка чатов MAX.

    Используем ``client.fetch_chats(marker=None)`` — это гарантированный
    способ получить полный список чатов пользователя с сервера MAX
    (Opcode.CHATS_LIST) с пагинацией по marker (мс). ``client.chats``
    (кеш) может быть пустым на свежей сессии, поэтому опираемся именно
    на серверный ответ.
    """
    chats_list: list = []
    try:
        if hasattr(client, "fetch_chats"):
            try:
                chats_list = await client.fetch_chats(marker=None) or []
            except TypeError:
                chats_list = await client.fetch_chats() or []
            logger.info("fetch_chats on start returned %d chats", len(chats_list))
        else:
            logger.warning(
                "on_start: client.fetch_chats not available; "
                "falling back to client.chats cache"
            )
            chats_list = list(getattr(client, "chats", None) or [])
    except Exception as exc:
        logger.warning("on_start: fetch_chats failed: %s", exc)
        chats_list = list(getattr(client, "chats", None) or [])

    # Если первый fetch вернул пустой список (например, MAX ещё не успел
    # прогреть кеш после login/sync) — пробуем ещё раз через 5 секунд,
    # один раз. Без этого на свежей сессии можно получить «0 чатов»
    # и не создать ни одного топика.
    if not chats_list and hasattr(client, "fetch_chats"):
        try:
            await asyncio.sleep(5.0)
            try:
                chats_list = await client.fetch_chats(marker=None) or []
            except TypeError:
                chats_list = await client.fetch_chats() or []
            logger.info(
                "fetch_chats retry on start returned %d chats", len(chats_list),
            )
        except Exception as exc:
            logger.warning("on_start: fetch_chats retry failed: %s", exc)

    return chats_list


async def on_start_actions(client) -> None:
    """Главная функция ``on_start``: auth=ok + sync чатов + sync топиков."""
    logger.info("PyMax client started, marking auth=ok")
    # ВАЖНО: передаём ``clear_error=True``, чтобы прошлая ошибка
    # (например, ``error.limit.violate`` от прошлой неудачной попытки)
    # не висела в /status после успешной авторизации. Без этого
    # AuthWatcher в боте не увидит переход need_2fa → ok и не пришлёт
    # сообщение «✅ MAX: вход выполнен успешно».
    try:
        async with httpx.AsyncClient(base_url=API_BASE, timeout=10.0) as c:
            r = await c.post(
                "/auth/state",
                json={
                    "status": "ok",
                    "last_login": True,
                    "clear_error": True,
                },
                headers=_headers(),
            )
            r.raise_for_status()
    except Exception as exc:
        logger.warning("on_start: post auth_state ok failed: %s", exc)

    chats_list = await _fetch_chats(client)

    # 0) Обогащаем ``title`` для чатов, у которых MAX его не вернул.
    await enrich_chat_titles(client, chats_list)

    # 1) Поэлементно синхронизируем каждую запись чата в БД.
    if chats_list:
        synced = 0
        for chat in chats_list:
            try:
                await _post("/chats", chat_to_dict(chat))
                synced += 1
            except Exception as exc:
                logger.warning(
                    "chat upsert on start failed for %s: %s",
                    getattr(chat, "id", "?"), exc,
                )
        logger.info("synced %d chats on start", synced)

        # 2) Дёргаем ``/internal/sync_topics``: API сравнит свежий
        # список MAX с уже существующими ``ChatTopic``, пометит пропавшие
        # как ``stale=1`` и поставит в очередь ``create``/``rename``-джобы.
        try:
            sync_payload = {
                "trigger": "auth_ok",
                "chats": [
                    {
                        "max_chat_id": str(getattr(c, "id", "")),
                        "title": display_name_of(c) or "",
                        "type": str(getattr(c, "type", "") or ""),
                    }
                    for c in chats_list
                    if getattr(c, "id", None) is not None
                ],
            }
            async with httpx.AsyncClient(
                base_url=API_BASE, timeout=15.0
            ) as _c:
                _r = await _c.post(
                    "/internal/sync_topics",
                    json=sync_payload,
                    headers=_headers(),
                )
                if _r.status_code >= 400:
                    logger.warning(
                        "on_start: /internal/sync_topics returned %s: %s",
                        _r.status_code, _r.text[:200],
                    )
                else:
                    body = _r.json() if _r.content else {}
                    logger.info(
                        "on_start: sync_topics result: %s",
                        body,
                    )
        except Exception as exc:
            logger.warning("on_start: post /internal/sync_topics failed: %s", exc)