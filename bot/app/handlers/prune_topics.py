"""Команда ``/prune_topics`` — закрытие stale-топиков.

Если MAX-чат пропал (``chat_topics.stale=1``), а его топик в Telegram
ещё существует — пользователь может:

* вызвать ``/prune_topics`` и получить список таких топиков с inline-кнопками
  «🗑 <название>» и «🧹 Закрыть все».
* нажать кнопку — ``prune_topic_callback`` вызовет ``closeForumTopic``
  и пометит ``stale=2`` в БД.

Сами топики не удаляются (Telegram Bot API этого не умеет) — только
закрываются (``is_closed=True``). Пользователь может потом переоткрыть
их вручную.
"""

from __future__ import annotations

import logging
from typing import List

from aiogram import types

from app.api_client import api
from app.handlers._common import _escape, _is_allowed, _reject
from app.keyboards import PruneTopicCallback
from shared import db as shared_db

logger = logging.getLogger(__name__)


def _build_prune_topics_keyboard(topics: list[dict]) -> types.InlineKeyboardMarkup:
    """Inline-клавиатура со списком stale-топиков и кнопкой «Закрыть все».

    Каждая кнопка — это ``PruneTopicCallback(action="close", max_chat_id=...)``.
    """
    rows: list[list[types.InlineKeyboardButton]] = []
    for t in topics[:8]:  # лимит на одну страницу
        title = (t.get("topic_name") or "").strip()
        cid = str(t.get("max_chat_id") or "?")
        label = title if title else f"(MAX: {cid})"
        # Укорачиваем, чтобы влезло в 64 байта callback_data.
        short = label[:40] + ("" if len(label) <= 40 else "…")
        rows.append([
            types.InlineKeyboardButton(
                text=f"🗑 {short}",
                callback_data=PruneTopicCallback(
                    action="close", max_chat_id=cid,
                ).pack(),
            )
        ])
    if topics:
        rows.append([
            types.InlineKeyboardButton(
                text="🧹 Закрыть все",
                callback_data=PruneTopicCallback(
                    action="close_all", max_chat_id="",
                ).pack(),
            )
        ])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


async def prune_topics_command(message: types.Message) -> None:
    """Команда ``/prune_topics`` — показать топики, у которых MAX-чат пропал.

    Не закрывает ничего автоматически: только показывает список и
    inline-кнопки «🗑 <название>» / «🧹 Закрыть все». Фактическое
    закрытие выполняет ``prune_topic_callback``.
    """
    if not _is_allowed(message.from_user.id):
        return await _reject(message)

    owner_uid = int(message.from_user.id)
    try:
        topics = await api.list_stale_topics(owner_uid)
    except Exception as exc:
        logger.warning("prune_topics_command: api.list_stale_topics failed: %s", exc)
        await message.answer(f"⚠️ Не удалось получить список: {exc}")
        return

    if not topics:
        await message.answer(
            "✅ Нет stale-топиков. Все топики в группе соответствуют "
            "актуальным MAX-чатам."
        )
        return

    lines = [
        f"⚠️ <b>{len(topics)}</b> топик(ов) ссылаются на MAX-чаты, "
        "которых больше нет в вашем списке.\n",
        "Это значит, что вы вышли из чата в MAX (или чат удалён), "
        "но топик в Telegram ещё существует.\n",
        "Я НЕ закрываю их автоматически. Выберите, что сделать:",
    ]
    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_build_prune_topics_keyboard(topics),
    )


async def prune_topic_callback(
        callback: types.CallbackQuery,
) -> None:
    """Закрыть один stale-топик (или все) через closeForumTopic.

    После успешного close'а помечает ``stale=2`` в БД через
    ``api.close_stale_topic``. Если close провалился — состояние в БД
    не меняем, топик останется в списке до следующей попытки.
    """
    if not callback.from_user or not _is_allowed(callback.from_user.id):
        return await callback.answer("⛔", show_alert=True)
    try:
        cb = PruneTopicCallback.unpack(callback.data or "")
    except Exception:
        logger.warning("prune_topic_callback: bad data %r", callback.data)
        return await callback.answer("⚠️ битый callback", show_alert=True)

    action = (cb.action or "").lower()
    if action not in ("close", "close_all"):
        return await callback.answer(f"⚠️ неизвестное действие: {action}", show_alert=True)

    owner_uid = int(callback.from_user.id)

    # Определяем список топиков для закрытия.
    try:
        if action == "close":
            topics = await api.list_stale_topics(owner_uid)
            wanted = [t for t in topics if str(t.get("max_chat_id") or "") == str(cb.max_chat_id)]
        else:
            topics = await api.list_stale_topics(owner_uid)
            wanted = topics
    except Exception as exc:
        logger.warning("prune_topic_callback: list_stale_topics failed: %s", exc)
        return await callback.answer(f"⚠️ API: {exc}", show_alert=True)

    if not wanted:
        await callback.answer("ℹ️ Уже закрыты", show_alert=True)
        return

    await callback.answer()  # снимаем «часики»

    closed = 0
    failed: list[tuple[str, str]] = []
    for t in wanted:
        cid = str(t.get("max_chat_id") or "")
        supergroup_chat_id = int(t.get("supergroup_chat_id") or 0)
        thread_id = int(t.get("thread_id") or 0)
        if not supergroup_chat_id or not thread_id:
            failed.append((cid, "missing supergroup/thread"))
            continue
        try:
            from app.topics import close_topic
            ok = await close_topic(
                bot=callback.bot,
                supergroup_chat_id=supergroup_chat_id,
                thread_id=thread_id,
            )
        except Exception as exc:
            logger.warning(
                "prune_topic_callback: close_topic raised for %s: %s", cid, exc,
            )
            ok = False
            failed.append((cid, str(exc)))
        if ok:
            try:
                await api.close_stale_topic(cid, owner_uid)
                closed += 1
            except Exception as exc:
                logger.warning(
                    "prune_topic_callback: close_stale_topic API failed for %s: %s",
                    cid, exc,
                )
                # closeForumTopic прошёл — пометим в БД локально как fallback.
                try:
                    shared_db.mark_topic_closed(cid)
                except Exception as exc2:
                    logger.warning(
                        "prune_topic_callback: local mark_topic_closed failed: %s", exc2,
                    )
                closed += 1
        else:
            failed.append((cid, "closeForumTopic failed"))

    # Формируем отчёт.
    parts = [f"✅ Закрыто топиков: <b>{closed}</b>"]
    if failed:
        parts.append(
            "\n⚠️ Не удалось закрыть:\n"
            + "\n".join(
                f"  • <code>{_escape(cid)}</code> — {_escape(err)}"
                for cid, err in failed[:10]
            )
        )
    await callback.message.answer("\n".join(parts), parse_mode="HTML")