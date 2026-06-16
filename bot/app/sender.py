"""Отправка сообщений в Telegram: медиа + текст."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Dict, Optional

from aiogram import Bot
from aiogram.types import FSInputFile, Message

from app.config import settings

logger = logging.getLogger(__name__)


# В Telegram Bot API лимит загрузки — 50 МБ.
MAX_TG_FILE_SIZE = 49 * 1024 * 1024


def _abs_media_path(media_path: str) -> str:
    if os.path.isabs(media_path):
        return media_path
    return os.path.join(settings.media_dir, media_path)


def _escape(text: str) -> str:
    if not text:
        return ""
    return (
        text.replace("&", "&")
        .replace("<", "<")
        .replace(">", ">")
    )


def _format_header(event: Dict[str, Any]) -> str:
    title = event.get("chat_title") or event.get("max_chat_id") or "?"
    sender = event.get("sender") or "—"
    ts = event.get("timestamp")
    ts_str = ""
    if ts:
        try:
            ts_str = " · " + datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%d.%m %H:%M")
        except Exception:
            pass
    outgoing = "↗️ Вы" if event.get("is_outgoing") else "↘️ " + sender
    return f"💬 <b>{_escape(title)}</b>\n{outgoing}{ts_str}"


def _caption(event: Dict[str, Any], header: str) -> str:
    text = event.get("text") or ""
    parts = [header]
    if text:
        parts.append("")
        parts.append(_escape(text[:3500]))
    return "\n".join(parts)[:4096]


async def forward_event(bot: Bot, target_chat_id: int, event: Dict[str, Any]) -> Optional[Message]:
    kind = (event.get("kind") or "text").lower()
    media_path = event.get("media_path")
    header = _format_header(event)

    if not media_path:
        return await bot.send_message(
            chat_id=target_chat_id,
            text=_caption(event, header),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    abs_path = _abs_media_path(media_path)
    if not os.path.exists(abs_path):
        return await bot.send_message(
            chat_id=target_chat_id,
            text=_caption(event, header) + "\n\n<i>(медиафайл не найден)</i>",
            parse_mode="HTML",
        )

    size = os.path.getsize(abs_path)
    cap = _caption(event, header)
    filename = event.get("media_filename") or os.path.basename(abs_path)
    doc = FSInputFile(abs_path, filename=filename)

    if size > MAX_TG_FILE_SIZE:
        await bot.send_message(
            chat_id=target_chat_id,
            text=_caption(event, header) + f"\n\n<i>Файл больше 50 МБ ({size // 1024 // 1024} МБ) — в MAX</i>",
            parse_mode="HTML",
        )
        return None

    if kind == "photo":
        return await bot.send_photo(chat_id=target_chat_id, photo=doc, caption=cap[:1024], parse_mode="HTML")
    if kind == "video":
        return await bot.send_video(chat_id=target_chat_id, video=doc, caption=cap[:1024], parse_mode="HTML")
    if kind == "voice":
        return await bot.send_voice(chat_id=target_chat_id, voice=doc, caption=cap[:1024], parse_mode="HTML")
    if kind == "video_note":
        try:
            return await bot.send_video_note(chat_id=target_chat_id, video_note=doc)
        except Exception:
            return await bot.send_document(chat_id=target_chat_id, document=doc, caption=cap[:1024], parse_mode="HTML")
    if kind in ("audio", "sticker", "document", "other"):
        return await bot.send_document(chat_id=target_chat_id, document=doc, caption=cap[:1024], parse_mode="HTML")

    return await bot.send_document(chat_id=target_chat_id, document=doc, caption=cap[:1024], parse_mode="HTML")