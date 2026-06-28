"""Отправка сообщений в Telegram: медиа + текст."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Dict, Optional

from aiogram import Bot
from aiogram.types import FSInputFile, InlineKeyboardMarkup, Message

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


async def forward_event(
    bot: Bot,
    target_chat_id: int,
    event: Dict[str, Any],
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    message_thread_id: Optional[int] = None,
    header_override: Optional[str] = None,
) -> Optional[Message]:
    """Переслать событие из MAX в Telegram.

    Если передан ``reply_markup``, inline-клавиатура прикрепляется
    прямо к сообщению с самим событием (текст или медиа). Раньше
    клавиатуру приходилось слать отдельным сообщением-заглушкой «—»,
    что разрывало связь кнопок с контекстом и через 48ч Telegram
    запрещал нажимать на них.

    ``message_thread_id`` (опционально) — id топика в супергруппе
    (см. ``app/topics.py``). Если задан, сообщение уходит в топик.

    ``header_override`` (опционально) — если передано ``""``, шапка
    «💬 чат / ↘️ автор · время» не добавляется к тексту/caption
    (используется в compact-режиме для топиков, см. ``COMPACT_TOPIC_MESSAGES``).
    По умолчанию (``None``) — шапка формируется через ``_format_header(event)``.
    """
    kind = (event.get("kind") or "text").lower()
    media_path = event.get("media_path")
    if header_override is None:
        header = _format_header(event)
    else:
        header = header_override

    if not media_path:
        return await bot.send_message(
            chat_id=target_chat_id,
            text=_caption(event, header),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=reply_markup,
            message_thread_id=message_thread_id,
        )

    abs_path = _abs_media_path(media_path)
    if not os.path.exists(abs_path):
        return await bot.send_message(
            chat_id=target_chat_id,
            text=_caption(event, header) + "\n\n<i>(медиафайл не найден)</i>",
            parse_mode="HTML",
            reply_markup=reply_markup,
            message_thread_id=message_thread_id,
        )

    size = os.path.getsize(abs_path)
    cap = _caption(event, header)
    filename = event.get("media_filename") or os.path.basename(abs_path)
    doc = FSInputFile(abs_path, filename=filename)

    if size > MAX_TG_FILE_SIZE:
        msg = await bot.send_message(
            chat_id=target_chat_id,
            text=_caption(event, header) + f"\n\n<i>Файл больше 50 МБ ({size // 1024 // 1024} МБ) — в MAX</i>",
            parse_mode="HTML",
            reply_markup=reply_markup,
            message_thread_id=message_thread_id,
        )
        return msg

    if kind == "photo":
        return await bot.send_photo(
            chat_id=target_chat_id, photo=doc, caption=cap[:1024],
            parse_mode="HTML", reply_markup=reply_markup,
            message_thread_id=message_thread_id,
        )
    if kind == "video":
        return await bot.send_video(
            chat_id=target_chat_id, video=doc, caption=cap[:1024],
            parse_mode="HTML", reply_markup=reply_markup,
            message_thread_id=message_thread_id,
        )
    if kind == "voice":
        return await bot.send_voice(
            chat_id=target_chat_id, voice=doc, caption=cap[:1024],
            parse_mode="HTML", reply_markup=reply_markup,
            message_thread_id=message_thread_id,
        )
    if kind == "video_note":
        try:
            return await bot.send_video_note(
                chat_id=target_chat_id, video_note=doc,
                message_thread_id=message_thread_id,
            )
        except Exception:
            return await bot.send_document(
                chat_id=target_chat_id, document=doc, caption=cap[:1024],
                parse_mode="HTML", reply_markup=reply_markup,
                message_thread_id=message_thread_id,
            )
    if kind in ("audio", "sticker", "document", "other"):
        return await bot.send_document(
            chat_id=target_chat_id, document=doc, caption=cap[:1024],
            parse_mode="HTML", reply_markup=reply_markup,
            message_thread_id=message_thread_id,
        )

    return await bot.send_document(
        chat_id=target_chat_id, document=doc, caption=cap[:1024],
        parse_mode="HTML", reply_markup=reply_markup,
        message_thread_id=message_thread_id,
    )
