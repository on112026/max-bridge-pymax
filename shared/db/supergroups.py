"""Функции для работы с таблицей ``super_groups`` — привязанные Telegram supergroups.

Один владелец (Telegram user id) → одна supergroup (1-per-owner). Создаётся
через ``/setup`` или ``/autosetup``, валидируется ``AttachResult`` (см.
``bot/app/handlers/_supergroup.py``).

``invite_link`` обновляется через ``update_supergroup_invite_link`` при
``/getlink``.
"""

from __future__ import annotations

from typing import Optional

from shared.db._engine import session_scope
from shared.db._models import SuperGroup


def get_supergroup_for_owner(owner_user_id: int) -> Optional[SuperGroup]:
    """Возвращает supergroup владельца или ``None`` если ещё не создал."""
    with session_scope() as s:
        row = (
            s.query(SuperGroup)
            .filter(SuperGroup.owner_user_id == int(owner_user_id))
            .first()
        )
        if not row:
            return None
        s.expunge(row)
        return row


def create_supergroup(
    owner_user_id: int,
    supergroup_chat_id: int,
    title: str,
    invite_link: Optional[str] = None,
    is_forum_enabled: bool = True,
) -> None:
    """Создать запись о приватной supergroup для пользователя.

    Если запись уже существует — обновляет ``supergroup_chat_id`` / ``invite_link``.
    """
    with session_scope() as s:
        existing = (
            s.query(SuperGroup)
            .filter(SuperGroup.owner_user_id == int(owner_user_id))
            .first()
        )
        if existing:
            existing.supergroup_chat_id = int(supergroup_chat_id)
            existing.title = title
            existing.invite_link = invite_link
            existing.is_forum_enabled = bool(is_forum_enabled)
            return
        s.add(SuperGroup(
            owner_user_id=int(owner_user_id),
            supergroup_chat_id=int(supergroup_chat_id),
            title=title,
            invite_link=invite_link,
            is_forum_enabled=bool(is_forum_enabled),
        ))


def update_supergroup_invite_link(
    owner_user_id: int, invite_link: str
) -> None:
    """Обновить ``invite_link`` (например, после ``export_chat_invite_link``)."""
    with session_scope() as s:
        row = (
            s.query(SuperGroup)
            .filter(SuperGroup.owner_user_id == int(owner_user_id))
            .first()
        )
        if row:
            row.invite_link = invite_link