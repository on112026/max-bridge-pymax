"""Pydantic-модели событий и команд, общие для сервисов."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class MediaKind(str, Enum):
    TEXT = "text"
    PHOTO = "photo"
    VIDEO = "video"
    VOICE = "voice"
    VIDEO_NOTE = "video_note"
    DOCUMENT = "document"
    STICKER = "sticker"
    AUDIO = "audio"
    OTHER = "other"


class NewMessage(BaseModel):
    """Событие нового сообщения из MAX."""

    max_chat_id: str
    max_message_id: str
    chat_title: Optional[str] = None
    sender: Optional[str] = None
    sender_id: Optional[str] = None
    text: Optional[str] = None
    kind: MediaKind = MediaKind.TEXT
    media_path: Optional[str] = None  # относительный путь в MEDIA_DIR
    media_mime: Optional[str] = None
    media_filename: Optional[str] = None
    media_size: Optional[int] = None
    timestamp: Optional[datetime] = None
    is_outgoing: bool = False
    raw: Optional[Dict[str, Any]] = None


class ChatInfo(BaseModel):
    max_chat_id: str
    title: Optional[str] = None
    type: Optional[str] = None
    last_message_preview: Optional[str] = None
    last_message_at: Optional[datetime] = None
    unread: Optional[int] = None


class SendCommand(BaseModel):
    """Команда на отправку сообщения в MAX (из Telegram)."""

    id: Optional[int] = None
    kind: MediaKind = MediaKind.TEXT
    target_chat_id: str
    text: Optional[str] = None
    media_path: Optional[str] = None
    media_mime: Optional[str] = None
    media_filename: Optional[str] = None
    created_by: Optional[int] = None
    status: str = "pending"
    error: Optional[str] = None
    created_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class Auth2faRequest(BaseModel):
    code: str = Field(min_length=4, max_length=12)