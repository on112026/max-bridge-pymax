"""Polling send_queue → отправляет в MAX через PyMax Client.

Берёт задачу из api (GET /send/next), отправляет через pymax.send_message
с Photo/File/Video, помечает finished (ok/error) через POST /send/{id}/finish.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

from pymax.files import File as MaxFile
from pymax.files import Photo as MaxPhoto
from pymax.files import Video as MaxVideo

# ``shared.db`` используется для создания ``DeliveredMessage``-записи
# после успешной отправки в MAX: это связывает новый ``max_message_id``
# с исходным ``tg_message_id`` из TG-топика, чтобы мост MAX→TG-реакций
# (``bridge._on_reaction_update``) мог найти запись через
# ``get_delivered_by_max_message`` и поставить реакцию в нужный
# TG-топик/сообщение.
from shared import db as shared_db

logger = logging.getLogger(__name__)


API_BASE = os.getenv("API_BASE_URL", "http://localhost:8000")
API_KEY = os.getenv("BRIDGE_API_KEY", "")
MEDIA_DIR = os.getenv("MEDIA_DIR", "/data/media")
POLL_INTERVAL = 2.0  # секунд между опросами


def _headers() -> dict:
    return {"X-Api-Key": API_KEY}


def _abs_media_path(media_path: str) -> str:
    if os.path.isabs(media_path):
        return media_path
    return os.path.join(MEDIA_DIR, media_path)


def _build_attachment(item: dict) -> Optional[Any]:
    kind = (item.get("kind") or "text").lower()
    media_path = item.get("media_path")
    if not media_path:
        return None
    abs_path = _abs_media_path(media_path)
    if not os.path.exists(abs_path):
        logger.warning("media file missing: %s", abs_path)
        return None
    if kind == "photo":
        return MaxPhoto(path=abs_path)
    if kind == "video":
        return MaxVideo(path=abs_path)
    if kind in ("document", "file", "audio", "sticker", "voice", "video_note", "other"):
        return MaxFile(path=abs_path, name=item.get("media_filename") or os.path.basename(abs_path))
    return None


async def _claim_next() -> Optional[dict]:
    try:
        async with httpx.AsyncClient(base_url=API_BASE, timeout=15.0) as c:
            r = await c.get("/send/next", headers=_headers())
            r.raise_for_status()
            if r.status_code == 200 and r.content:
                return r.json()
    except Exception as exc:
        logger.warning("claim_next failed: %s", exc)
    return None


def _log_thread_id(item_id: int, target_chat_id: str, thread_id: Optional[int]) -> None:
    """Логируем ``thread_id`` (для отладки и будущей синхронизации с TG-топиками)."""
    if thread_id:
        logger.info(
            "send item id=%s thread_id=%s chat=%s (ответ из TG-топика)",
            item_id, thread_id, target_chat_id,
        )
    else:
        logger.info("send item id=%s chat=%s", item_id, target_chat_id)


async def _finish(item_id: int, ok: bool, error: Optional[str] = None) -> None:
    try:
        async with httpx.AsyncClient(base_url=API_BASE, timeout=10.0) as c:
            r = await c.post(
                f"/send/{item_id}/finish",
                params={"ok": str(ok).lower(), "error": error or ""},
                headers=_headers(),
            )
            r.raise_for_status()
    except Exception as exc:
        logger.warning("finish_send id=%s failed: %s", item_id, exc)


async def _send_one(client, item: dict) -> tuple[bool, str | None, Optional[str]]:
    """Отправить одно сообщение в MAX.

    Возвращает ``(ok, error, max_message_id)``:
    * ``max_message_id`` — id только что отправленного сообщения в MAX
      (``str(msg.id)``), либо ``None`` при неуспехе. Нужен для создания
      ``DeliveredMessage``-записи в :func:`shared.db.record_delivered_with_tg`.
    """
    chat_id = item.get("target_chat_id")
    if not chat_id:
        return False, "target_chat_id missing", None
    try:
        chat_id_int = int(chat_id)
    except ValueError:
        return False, f"invalid target_chat_id: {chat_id}", None

    text = item.get("text") or ""
    attachment = _build_attachment(item)
    attachments = [attachment] if attachment is not None else None

    try:
        msg = await client.send_message(
            chat_id=chat_id_int,
            text=text,
            attachments=attachments,
            notify=True,
        )
        if msg is None:
            return False, "send_message returned None", None
        # ``msg.id`` в PyMax — ``str`` вида ``"mid.123…`` (см. ``pymax.types``).
        max_message_id = str(getattr(msg, "id", "") or "")
        if not max_message_id:
            return False, "send_message returned message without id", None
        return True, None, max_message_id
    except Exception as exc:
        return False, str(exc), None


def _maybe_record_delivered(item: dict, max_message_id: str) -> None:
    """Создать ``DeliveredMessage``-запись для исходящего TG→MAX сообщения.

    Заполняется, если ``topic_echo`` (бот) передал ``tg_chat_id`` и
    ``tg_message_id`` в payload — это означает, что сообщение пришло из
    конкретного TG-топика и должно быть зеркально отражено в обратную
    сторону (MAX→TG-реакции).

    ``thread_id`` хранится отдельно (``tg_thread_id``) для будущей
    синхронизации ``chat.read_at`` с TG-топиком.
    """
    tg_chat_id = item.get("tg_chat_id")
    tg_message_id = item.get("tg_message_id")
    if tg_chat_id is None or tg_message_id is None:
        # Исходящее сообщение без привязки к TG-топику (например, ручной
        # POST /send без bridge-контекста) — не создаём запись, чтобы не
        # засорять ``delivered_messages`` мусорными строками.
        return
    target_chat_id = str(item.get("target_chat_id") or "")
    if not target_chat_id:
        return
    try:
        thread_id = item.get("thread_id")
        tg_thread_id = int(thread_id) if thread_id is not None else None
        shared_db.record_delivered_with_tg(
            max_chat_id=target_chat_id,
            max_message_id=max_message_id,
            tg_chat_id=int(tg_chat_id),
            tg_thread_id=tg_thread_id,
            tg_message_id=int(tg_message_id),
        )
        logger.info(
            "delivered_mapping max_chat=%s max_msg=%s -> tg_chat=%s tg_msg=%s thread=%s",
            target_chat_id,
            max_message_id,
            tg_chat_id,
            tg_message_id,
            tg_thread_id,
        )
    except Exception as exc:
        # Не критично: основная отправка уже успешно завершена. Если
        # запись не создастся, мост MAX→TG-реакций просто пропустит
        # зеркалирование для этого сообщения, как и раньше.
        logger.warning(
            "record_delivered_with_tg failed max_msg=%s tg_msg=%s: %s",
            max_message_id, tg_message_id, exc,
        )


async def sender_loop(client, stop_event) -> None:
    """Основной цикл: забирает задачи и шлёт их в MAX."""
    logger.info("sender_loop started (poll=%.1fs)", POLL_INTERVAL)
    while not stop_event.is_set():
        item = await _claim_next()
        if item is None:
            # api не вернул задачу — подождём
            try:
                await asyncio_wait(stop_event, POLL_INTERVAL)
            except Exception:
                break
            continue
        item_id = item.get("id")
        if not item_id:
            continue
        # ``thread_id`` (если есть) пришёл из TG-топика — логируем для
        # трассировки и будущей синхронизации ``chat.read_at`` с TG.
        _log_thread_id(
            item_id=item_id,
            target_chat_id=item.get("target_chat_id", ""),
            thread_id=item.get("thread_id"),
        )
        ok, err, max_message_id = await _send_one(client, item)
        if ok and max_message_id:
            # Связываем новое MAX-сообщение с исходным TG-сообщением из
            # топика, чтобы мост MAX→TG-реакций мог найти запись через
            # ``get_delivered_by_max_message`` и поставить реакцию
            # обратно в нужный TG-топик.
            _maybe_record_delivered(item, max_message_id)
        await _finish(item_id, ok=ok, error=err)
        logger.info(
            "send item id=%s ok=%s err=%s max_msg=%s",
            item_id, ok, err, max_message_id,
        )


async def asyncio_wait(stop_event, timeout: float) -> None:
    """Локальный helper: ждать stop_event с таймаутом."""
    import asyncio
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass