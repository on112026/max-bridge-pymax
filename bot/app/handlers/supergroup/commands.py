"""Команды бота для привязки приватной Telegram-supergroup.

* ``/setup`` — инструкция по подключению supergroup (или ссылка на
  уже привязанную).
* ``/setgroup <chat_id>`` — ручное подключение по ``chat_id`` (владелец
  узнаёт через @RawDataBot).
* ``/autosetup`` — автоматическое подключение группы, **вызывается
  внутри самой группы**. ``chat_id`` берётся прямо из ``message.chat.id``.
* ``/getlink`` — получить/пересоздать invite-ссылку на привязанную группу.

Все три команды привязки (``/setgroup``, ``/autosetup``) вызывают
общий ``_attach_supergroup_for_owner`` из ``attach.py`` — он прогоняет
чеклист бота-админа/forum/invite-link и пишет в БД только при успехе.
"""

from __future__ import annotations

import logging

from aiogram import types

from app.api_client import api
from app.handlers._common import _escape, _is_allowed, _reject
from app.handlers.supergroup.attach import (
    _attach_supergroup_for_owner,
    _format_attach_failure_for_owner,
)
from app.topics import export_invite_link
from shared import db as shared_db

logger = logging.getLogger(__name__)


# ---------- Команды ----------


async def setup_command(message: types.Message) -> None:
    """``/setup`` — инструкция по подключению supergroup.

    Поведение:
    * Если supergroup уже привязана — присылает её текущий ``invite_link``
      и ``chat_id`` (для справки).
    * Если нет — пошаговая инструкция по созданию вручную.
    """
    if not _is_allowed(message.from_user.id):
        return await _reject(message)

    owner_uid = message.from_user.id
    existing = shared_db.get_supergroup_for_owner(owner_uid)
    if existing:
        link = existing.invite_link or "(см. /getlink)"
        await message.answer(
            "ℹ️ Supergroup уже подключена.\n\n"
            f"Название: <b>{_escape(existing.title or '—')}</b>\n"
            f"chat_id: <code>{existing.supergroup_chat_id}</code>\n"
            f"Топики (forum): {'включены ✅' if existing.is_forum_enabled else 'включите вручную ⚠️'}\n"
            f"🔗 Ссылка: {link}\n\n"
            "Если хотите сменить группу — выполните /setgroup <chat_id> ещё раз."
        )
        return

    await message.answer(
        "🛠 <b>Настройка supergroup для моста MAX ↔ Telegram</b>\n\n"
        "Telegram Bot API не умеет создавать supergroups напрямую, "
        "поэтому нужно создать группу вручную (в любом клиенте Telegram).\n\n"
        "<b>Шаги:</b>\n"
        "1. В Telegram создайте новую группу (меню «Создать группу»).\n"
        "2. <b>Включите топики</b>: Настройки группы → «Топики» (Topics).\n"
        "3. Добавьте этого бота в группу и сделайте <b>админом</b> "
        "(с правом «Управление топиками»).\n"
        "4. <b>Внутри созданной группы</b> выполните:\n"
        "   <code>/autosetup</code>\n"
        "   Бот сам определит <code>chat_id</code> группы, проверит все "
        "требования и привяжет её к мосту. Если что-то не так — пришлёт "
        "в личку подробный чеклист, что доделать вручную.\n\n"
        "<i>Альтернатива (ручной режим):</i> узнайте <code>chat_id</code> "
        "через @RawDataBot и выполните в личке <code>/setgroup <chat_id></code>.\n\n"
        "После успешного подключения бот начнёт пересылать события из MAX "
        "в эту группу (каждый MAX-чат получит свой топик)."
    )


async def setgroup_command(message: types.Message) -> None:
    """``/setgroup <chat_id>`` — ручная привязка supergroup по её id.

    Использование: <code>/setgroup <chat_id></code>, где ``chat_id``
    имеет формат <code>-100xxxxxxxxxx</code>. Бот проверит, что он
    находится в группе и может создавать топики, после чего сохранит
    привязку в БД. ``EventPoller`` сразу подхватит накопившиеся события.
    """
    if not _is_allowed(message.from_user.id):
        return await _reject(message)

    args = (message.text or "").split()
    if len(args) < 2:
        await message.answer(
            "Использование: <code>/setgroup <chat_id></code>\n"
            "Формат <code>chat_id</code>: <code>-100xxxxxxxxxx</code> "
            "(можно узнать через @RawDataBot).\n\n"
            "<i>Совет:</i> проще вызвать <code>/autosetup</code> прямо "
            "внутри группы — бот сам определит <code>chat_id</code>."
        )
        return

    try:
        chat_id = int(args[1].strip())
    except ValueError:
        await message.answer("⚠️ chat_id должен быть числом (формат -100xxxxxxxxxx).")
        return

    result = await _attach_supergroup_for_owner(
        bot=message.bot,
        chat_id=chat_id,
        owner_uid=message.from_user.id,
    )

    if not result.ok:
        logger.info(
            "setgroup_command: REFUSED chat_id=%s reason=%s",
            chat_id, result.short_error,
        )
        await message.answer(
            f"⚠️ Не удалось привязать группу: {result.short_error}."
        )
        await message.answer(
            _format_attach_failure_for_owner(result),
            parse_mode="HTML",
        )
        return

    # Успех.
    details = result.details or {}
    title = str(details.get("title") or "MAX Bridge 🔒")
    forum_ok = bool(details.get("forum_ok"))
    invite_link = details.get("invite_link")
    await message.answer(
        f"✅ Supergroup подключена!\n\n"
        f"Название: <b>{_escape(title)}</b>\n"
        f"chat_id: <code>{chat_id}</code>\n"
        f"Топики (forum): {'включены ✅' if forum_ok else '⚠️ включите вручную'}\n"
        f"🔗 Invite: {invite_link or '(сгенерируйте вручную через /getlink)'}\n\n"
        "Бот начнёт пересылать события из MAX в эту группу (каждый MAX-чат "
        "получит свой топик). Уже накопленные события будут доставлены "
        "автоматически в течение пары секунд."
    )

    # Если были события «undelivered» — форсим один tick EventPoller'а
    # нельзя (поллер сам крутится), но владельцу полезно увидеть мгновенный
    # фидбек. Поэтому после /setgroup сразу дёрнем api.status и сообщим.
    try:
        s = await api.status()
        undelivered = s.get("undelivered", 0)
        if undelivered:
            await message.answer(
                f"📬 Сейчас в очереди <b>{undelivered}</b> непрочитанных событий — "
                "через пару секунд они появятся в группе в своих топиках."
            )
    except Exception as exc:
        logger.debug("setgroup: status check failed: %s", exc)


async def autosetup_command(message: types.Message) -> None:
    """``/autosetup`` — автоматическая привязка группы к владельцу.

    Вызывается **внутри** той группы, которую нужно подключить.
    ``chat_id`` берётся прямо из ``message.chat.id`` — самый надёжный
    способ (никаких форвардов, никаких внешних утилит).

    Поведение:
    * Если команда вызвана **вне группы** (в личке) — просим вызвать
      команду прямо в группе.
    * Если команда вызвана **в группе**, но это **не supergroup** —
      отказываем с понятным сообщением.
    * Если бот — **не админ** или топики **выключены** — в группу шлём
      короткое сообщение с причиной отказа, в личку владельцу — детальный
      чеклист (через ``_format_attach_failure_for_owner``).
    * Если всё ОК — пишем в БД, шлём подтверждение и в группу, и в личку.
    """
    if not message.from_user or not _is_allowed(message.from_user.id):
        return await _reject(message)

    chat = message.chat
    chat_type = str(getattr(chat, "type", "") or "")
    if chat_type not in ("supergroup", "group"):
        await message.answer(
            "ℹ️ Команду <code>/autosetup</code> нужно вызвать "
            "<b>внутри группы</b>, которую хотите подключить к мосту.\n"
            "Откройте нужную группу → в поле сообщения наберите "
            "<code>/autosetup</code> и отправьте.",
            parse_mode="HTML",
        )
        return

    chat_id = int(chat.id)
    owner_uid = int(message.from_user.id)
    logger.info(
        "autosetup_command: uid=%s chat_id=%s chat_type=%s",
        owner_uid, chat_id, chat_type,
    )

    result = await _attach_supergroup_for_owner(
        bot=message.bot,
        chat_id=chat_id,
        owner_uid=owner_uid,
    )

    if not result.ok:
        logger.info(
            "autosetup_command: REFUSED chat_id=%s reason=%s",
            chat_id, result.short_error,
        )
        # В группу — короткое сообщение с причиной отказа.
        try:
            await message.answer(
                f"⚠️ Не удалось подключить эту группу автоматически: "
                f"{result.short_error}.\n"
                "Детали и чеклист прислал вам в личку."
            )
        except Exception as exc:
            logger.debug("autosetup: failed to reply in group: %s", exc)
        # В личку владельцу — подробный разбор.
        try:
            await message.bot.send_message(
                owner_uid,
                _format_attach_failure_for_owner(result),
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.warning("autosetup: failed to DM owner: %s", exc)
        return

    # Успех.
    details = result.details or {}
    title = str(details.get("title") or "MAX Bridge 🔒")
    invite_link = details.get("invite_link")
    logger.info(
        "autosetup_command: SUCCESS chat_id=%s title=%r",
        chat_id, title,
    )
    # В группу — короткое подтверждение.
    try:
        await message.answer(
            f"✅ Supergroup «<b>{_escape(title)}</b>» подключена к мосту MAX↔Telegram!\n"
            "События MAX будут приходить в топики этой группы "
            "(каждый MAX-чат — свой топик).",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.debug("autosetup: failed to reply in group: %s", exc)
    # В личку владельцу — развёрнуто.
    try:
        await message.bot.send_message(
            owner_uid,
            f"✅ Supergroup подключена автоматически!\n\n"
            f"Название: <b>{_escape(title)}</b>\n"
            f"chat_id: <code>{chat_id}</code>\n"
            f"Топики (forum): включены ✅\n"
            f"🔗 Invite: {invite_link or '(сгенерируйте вручную через /getlink)'}\n\n"
            "Бот начнёт пересылать события из MAX в топики этой группы. "
            "Уже накопленные события будут доставлены автоматически в течение "
            "пары секунд.",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.warning("autosetup: failed to DM owner: %s", exc)

    # Сообщим о накопленных undelivered (как в setgroup_command).
    try:
        s = await api.status()
        undelivered = s.get("undelivered", 0)
        if undelivered:
            await message.bot.send_message(
                owner_uid,
                f"📬 Сейчас в очереди <b>{undelivered}</b> непрочитанных событий — "
                "через пару секунд они появятся в группе в своих топиках.",
                parse_mode="HTML",
            )
    except Exception as exc:
        logger.debug("autosetup: status check failed: %s", exc)


async def getlink_command(message: types.Message) -> None:
    """``/getlink`` — получить/пересоздать invite-ссылку на приватную группу."""
    if not _is_allowed(message.from_user.id):
        return await _reject(message)
    sg = shared_db.get_supergroup_for_owner(message.from_user.id)
    if not sg:
        await message.answer(
            "ℹ️ Группа ещё не создана. Используйте /setup."
        )
        return
    new_link = await export_invite_link(message.bot, sg.supergroup_chat_id)
    if new_link:
        shared_db.update_supergroup_invite_link(message.from_user.id, new_link)
    await message.answer(
        f"🔗 Ссылка на группу «{_escape(sg.title or '—')}»:\n"
        f"{new_link or sg.invite_link or '(не удалось получить)'}"
    )