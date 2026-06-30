"""Inline-кнопка «🔄 Реакции» под сообщением из MAX в группе/канале.

Хэндлер кладёт ``to_max`` задачу с ``op="fetch_summary"`` — MAX-процесс
делает свежий ``client.get_reactions`` и возвращает результат как
``to_tg_summary`` задачу. ``ReactionsMaxPoller`` обновит сводку.

По сути это «форс-refresh» для сводки «👍×N 🔥×M · итого K» под
сообщением в топике (MAX-процесс и так автоматически обновляет её
на каждый ``on_reaction_update``, но иногда удобно перезапросить
вручную).
"""

from __future__ import annotations

import logging

from aiogram import Bot, types

from app.api_client import api
from app.handlers._common import _is_allowed
from app.keyboards import ReactionSummaryCallback

logger = logging.getLogger(__name__)


async def reaction_summary_callback(
    callback: types.CallbackQuery,
) -> None:
    """Inline-кнопка «🔄 Реакции»: запросить сводку реакций из MAX.

    Фильтр по владельцу и нашей супергруппе — на уровне ``register_handlers``.
    """
    try:
        if not callback.from_user or not _is_allowed(callback.from_user.id):
            return await callback.answer("⛔", show_alert=True)
        try:
            cb = ReactionSummaryCallback.unpack(callback.data)
        except Exception as exc:
            logger.error(
                "reaction_summary_callback: bad callback_data=%r: %s",
                callback.data, exc,
            )
            return await callback.answer("⚠️ битый callback", show_alert=True)

        max_chat_id = str(cb.max_chat_id)
        max_message_id = str(cb.max_message_id)

        # Кладём ``to_max`` задачу ``fetch_summary`` — MAX-процесс
        # дёрнет ``get_reactions`` и положит ``to_tg_summary``,
        # которую ReactionsMaxPoller обновит в топике.
        try:
            await api.enqueue_reaction_op_to_max(
                op="fetch_summary",
                max_chat_id=max_chat_id,
                max_message_id=max_message_id,
                emoji="",  # не используется для fetch_summary
            )
        except Exception as exc:
            logger.warning(
                "reaction_summary_callback: enqueue failed chat=%s msg=%s: %s",
                max_chat_id, max_message_id, exc,
            )
            return await callback.answer(
                f"⚠️ Не удалось запросить: {exc}",
                show_alert=True,
            )

        await callback.answer("🔄 Обновляю реакции…", show_alert=False)
        logger.info(
            "reaction_summary_callback: enqueued fetch_summary for chat=%s msg=%s",
            max_chat_id, max_message_id,
        )
    except Exception as exc:
        logger.exception("reaction_summary_callback failed: %s", exc)
        try:
            await callback.answer("⚠️ ошибка", show_alert=True)
        except Exception:
            pass