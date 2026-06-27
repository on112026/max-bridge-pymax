"""Логика привязки приватной Telegram-supergroup к владельцу моста.

Telegram Bot API **не позволяет ботам создавать supergroups** напрямую —
это делает владелец вручную (TDesktop, Telegram для Android/iOS, любой
клиент). После того как supergroup создана и бот добавлен в неё как
админ с правом «Manage Topics», владелец может:

* вызвать ``/autosetup`` **внутри** группы — бот сам определит
  ``chat_id``, проверит все требования и привяжет группу к мосту;
* или вызвать ``/setgroup <chat_id>`` в личке — то же самое, но
  ``chat_id`` нужно ввести вручную (например, узнав через @RawDataBot).

Оба пути идут через единый helper ``_attach_supergroup_for_owner``,
который прогоняет чеклист:

1. ``bot.get_chat(chat_id)`` — бот вообще видит этот чат.
2. Тип чата — ``supergroup`` / ``group``.
3. ``bot.get_chat_member`` — бот ``administrator`` или ``creator``.
4. ``ensure_forum_enabled`` — топики включены (``chat.is_forum``).
5. ``export_invite_link`` — получаем/создаём invite-ссылку.
6. ``shared_db.create_supergroup(...)`` — финальная запись в БД.

При успехе ``AuthWatcher._supergroup_prompted_for`` сбрасывается —
иначе бот продолжит напоминать «сделай /setup» даже после привязки.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict

from aiogram import Bot

from app.handlers._common import _escape
from app.handlers.auth_watcher import AuthWatcher
from app.topics import ensure_forum_enabled, export_invite_link
from shared import db as shared_db

logger = logging.getLogger(__name__)


@dataclass
class AttachResult:
    """Результат ``_attach_supergroup_for_owner``.

    ``ok=True`` означает, что все проверки пройдены и запись в БД создана.
    В этом случае ``details`` содержит ``title``/``invite_link``/``forum_ok``.

    Если ``ok=False``, ``short_error`` — короткое описание причины отказа
    (одна строка, для отправки в группу), а ``details`` — словарь с
    подробностями по каждой проверке (для детального сообщения в личку).
    """

    ok: bool
    short_error: str = ""
    details: Dict[str, Any] = None

    def __post_init__(self) -> None:
        if self.details is None:
            self.details = {}


async def _attach_supergroup_for_owner(
    bot: Bot,
    chat_id: int,
    owner_uid: int,
) -> AttachResult:
    """Проверить требования к supergroup и привязать её к ``owner_uid``.

    Шаги (выполняются последовательно, на первой же ошибке — отказ):

    1. ``bot.get_chat(chat_id)`` — бот вообще видит этот чат.
    2. Тип чата — ``supergroup`` (или ``group``, что эквивалентно в Bot API).
    3. ``bot.get_chat_member(chat_id, bot.id)`` — бот ``administrator`` или
       ``creator`` (нужны права на создание топиков).
    4. ``ensure_forum_enabled`` — топики включены (``chat.is_forum``).
    5. ``export_invite_link`` — получаем/создаём invite-ссылку.
    6. ``shared_db.create_supergroup(...)`` — финальная запись в БД.

    При успехе возвращает ``AttachResult(ok=True, details=...)``.
    При отказе — ``AttachResult(ok=False, short_error="...", details=...)``
    и **не пишет в БД**.
    """
    details: Dict[str, Any] = {"chat_id": chat_id}

    # 1) Бот должен видеть чат (быть добавленным).
    try:
        chat = await bot.get_chat(chat_id)
    except Exception as exc:
        logger.warning(
            "_attach_supergroup_for_owner: get_chat(%s) failed: %s", chat_id, exc
        )
        return AttachResult(
            ok=False,
            short_error="бот не состоит в этой группе или чат недоступен",
            details={**details, "stage": "get_chat", "error": str(exc)},
        )

    chat_type = str(getattr(chat, "type", "") or "")
    details["chat_type"] = chat_type
    details["title"] = getattr(chat, "title", None) or ""
    if "group" not in chat_type.lower():
        return AttachResult(
            ok=False,
            short_error=f"чат имеет тип «{chat_type}», нужен supergroup",
            details={**details, "stage": "chat_type"},
        )

    # 2) Бот должен быть админом.
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id, me.id)
        status = str(getattr(member, "status", "") or "")
    except Exception as exc:
        logger.warning(
            "_attach_supergroup_for_owner: get_chat_member(%s) failed: %s",
            chat_id, exc,
        )
        return AttachResult(
            ok=False,
            short_error="не удалось проверить права бота в группе",
            details={**details, "stage": "get_chat_member", "error": str(exc)},
        )
    details["bot_status"] = status
    if status not in ("ChatMemberStatus.ADMINISTRATOR", "administrator", "creator"):
        return AttachResult(
            ok=False,
            short_error="бот не админ в группе",
            details={**details, "stage": "bot_not_admin"},
        )

    # 3) Топики должны быть включены.
    forum_ok = await ensure_forum_enabled(bot, chat_id)
    details["forum_ok"] = forum_ok
    if not forum_ok:
        return AttachResult(
            ok=False,
            short_error="топики (forum) не включены в группе",
            details={**details, "stage": "forum_disabled"},
        )

    # 4) Invite-ссылка.
    invite_link = await export_invite_link(bot, chat_id)
    details["invite_link"] = invite_link

    # 5) Финальная запись в БД.
    title = getattr(chat, "title", None) or "MAX Bridge 🔒"
    try:
        shared_db.create_supergroup(
            owner_user_id=owner_uid,
            supergroup_chat_id=chat_id,
            title=title,
            invite_link=invite_link,
            is_forum_enabled=forum_ok,
        )
    except Exception as exc:
        logger.exception(
            "_attach_supergroup_for_owner: shared_db.create_supergroup failed for %s",
            chat_id,
        )
        return AttachResult(
            ok=False,
            short_error="ошибка записи в БД",
            details={**details, "stage": "db_create", "error": str(exc)},
        )

    # 6) Сбросить флаг «уже подсказали про /setup» в AuthWatcher.
    w = AuthWatcher.get_active()
    if w is not None:
        try:
            w._supergroup_prompted_for.discard(owner_uid)
        except Exception as exc:
            logger.debug("attach: reset prompt flag failed: %s", exc)

    details["title"] = title
    details["invite_link"] = invite_link
    details["forum_ok"] = forum_ok
    return AttachResult(ok=True, details=details)


def _format_attach_failure_for_owner(result: AttachResult) -> str:
    """Подробное сообщение для владельца (в личку) при отказе привязки.

    Перечисляет, какие проверки прошли, какие — нет, и что доделать
    вручную.
    """
    d = result.details or {}
    lines: list[str] = [
        f"⚠️ Не удалось подключить группу <code>{d.get('chat_id')}</code> автоматически.",
        "",
    ]
    chat_type_ok = "group" in str(d.get("chat_type", "")).lower()
    lines.append(
        f"• Тип чата: <b>{_escape(str(d.get('chat_type') or '—'))}</b> "
        f"{'✅' if chat_type_ok else '❌ (нужен supergroup)'}"
    )
    bot_status = str(d.get("bot_status") or "—")
    bot_status_ok = bot_status in (
        "ChatMemberStatus.ADMINISTRATOR", "administrator", "creator",
    )
    lines.append(
        f"• Бот — админ: <b>{_escape(bot_status)}</b> "
        f"{'✅' if bot_status_ok else '❌'}"
    )
    if "forum_ok" in d:
        lines.append(
            f"• Топики (forum): {'включены ✅' if d.get('forum_ok') else '❌ выключены'}"
        )

    stage = str(d.get("stage") or "")
    if stage == "get_chat":
        lines.append("")
        lines.append(
            "Бот не состоит в этой группе или чат недоступен. "
            "Сначала добавьте бота в группу."
        )
    elif stage == "bot_not_admin":
        lines.append("")
        lines.append(
            "Что сделать:\n"
            "1. Откройте группу → Управление → Администраторы.\n"
            "2. Добавьте бота с правом <b>«Manage Topics»</b>.\n"
            "3. Повторите <code>/autosetup</code> в группе."
        )
    elif stage == "forum_disabled":
        lines.append("")
        lines.append(
            "Что сделать:\n"
            "1. Откройте группу → Настройки группы → <b>Топики</b>.\n"
            "2. Включите «Топики».\n"
            "3. Повторите <code>/autosetup</code> в группе."
        )
    elif stage == "db_create":
        lines.append("")
        lines.append(
            f"Ошибка записи в БД: <code>{_escape(str(d.get('error') or '—'))}</code>"
        )
    elif d.get("error"):
        lines.append("")
        lines.append(
            f"Техническая ошибка: <code>{_escape(str(d.get('error') or '—'))}</code>"
        )

    return "\n".join(lines)