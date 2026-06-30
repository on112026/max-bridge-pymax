"""Monkey-patches для PyMax 2.2.0, которые не вошли в upstream.

Зачем этот модуль
------------------

PyMax 2.2.0 (вендор, см. ``vendor/pymax/``) содержит баг: методы
:meth:`pymax.api.messages.service.MessageService.add_reaction` и
:meth:`remove_reaction` сериализуют поле ``message_id`` как **строку**
(в Pydantic-модели ``AddReactionPayload.message_id: str``), а
MAX-сервер по протоколу ``proto.payload`` ожидает **число** (int64).

Симптом: каждый ``client.add_reaction(...)`` возвращает ошибку:

    ERROR pymax.app: api error opcode=178 seq=8 error=proto.payload
      title=Ошибка валидации message=Expected number at 24

После чего MAX-сервер принудительно разрывает long-poll соединение,
PyMax реконнектится — и так по кругу, пока в ``reaction_ops_queue``
висят непрочитанные реакции.

Решение
-------

Мы **не правим вендор** напрямую (``vendor/pymax/``) — иначе патч
молча слетит при любом обновлении PyMax. Вместо этого на уровне
сервисного слоя PyMax мы подменяем реализацию ``add_reaction`` /
``remove_reaction`` так, чтобы они слали payload напрямую с
``messageId`` как int64, минуя pydantic-валидацию ``AddReactionPayload``
(которая упорно требует str).

После того, как upstream починит баг (или мы обновимся до версии,
где ``message_id`` уже int в payload), этот модуль можно просто
удалить — наш ``reactions_loop`` продолжит работать корректно.

Применение
----------

:func:`apply` вызывается из :func:`max.app.bridge.register_bridge`
**до** того, как ``Client.start()`` начнёт слушать long-poll. Это
гарантирует, что любые вызовы ``client.add_reaction`` /
``client.remove_reaction`` (включая встроенный ``Message.react``)
идут через пропатченные функции.

Задокументировано в ``max-bridge-pymax/vendor/PYMAX_PATCHES.md``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


_APPLIED = False


def apply() -> None:
    """Наложить все известные monkey-patches на PyMax.

    Идемпотентна: повторный вызов no-op.
    """
    global _APPLIED
    if _APPLIED:
        logger.debug("pymax_patches.apply: already applied, skipping")
        return
    _patch_reaction_message_id_int()
    _patch_event_map_you_reacted()
    _APPLIED = True
    logger.info(
        "pymax_patches.apply: applied (reaction message_id → int, "
        "EVENT_MAP[NOTIF_MSG_YOU_REACTED] → REACTION_UPDATE)",
    )


# ---------------------------------------------------------------------------
# Patch 1: add_reaction / remove_reaction → message_id as int
# ---------------------------------------------------------------------------


def _patch_reaction_message_id_int() -> None:
    """PyMax 2.2.0: ``messageId`` в payload реакции должен быть int.

    Обходим Pydantic-модель ``AddReactionPayload`` (где ``message_id: str``)
    и шлём payload напрямую через ``app.invoke`` с правильными типами.
    """
    from pymax.api.messages import service as msgs_service
    from pymax.protocol import Opcode

    _orig_add = msgs_service.MessageService.add_reaction
    _orig_remove = msgs_service.MessageService.remove_reaction

    async def _patched_add_reaction(
        self,
        chat_id: Any,
        message_id: Any,
        reaction: str,
    ) -> Optional[Any]:
        """Замена :meth:`MessageService.add_reaction` с int message_id."""
        # Шлём payload как dict, минуя AddReactionPayload (там str).
        payload = {
            "chatId": int(chat_id),
            "messageId": int(message_id),  # ← int, а не str
            "reaction": {
                "reactionType": "EMOJI",
                "id": str(reaction),
            },
        }
        try:
            response = await self.app.invoke(Opcode.MSG_REACTION, payload)
        except Exception as exc:
            logger.warning(
                "pymax_patches: add_reaction failed chat=%s msg=%s emoji=%s: %s",
                chat_id, message_id, reaction, exc,
            )
            raise
        # Разбор ответа — повторяем логику оригинального метода.
        return _parse_reaction_info(self.app, response)

    async def _patched_remove_reaction(
        self,
        chat_id: Any,
        message_id: Any,
    ) -> Optional[Any]:
        """Замена :meth:`MessageService.remove_reaction` с int message_id."""
        payload = {
            "chatId": int(chat_id),
            "messageId": int(message_id),  # ← int, а не str
        }
        try:
            await self.app.invoke(Opcode.MSG_CANCEL_REACTION, payload)
        except Exception as exc:
            logger.warning(
                "pymax_patches: remove_reaction failed chat=%s msg=%s: %s",
                chat_id, message_id, exc,
            )
            raise
        return None

    msgs_service.MessageService.add_reaction = _patched_add_reaction
    msgs_service.MessageService.remove_reaction = _patched_remove_reaction

    # log: оригиналы сохранены для отладки/тестов
    _orig_add.__name__ = "_orig_add_reaction"
    _orig_remove.__name__ = "_orig_remove_reaction"


def _parse_reaction_info(app, response: Any) -> Optional[Any]:
    """Разбор ``ReactionInfo`` из ответа сервера (повторяет логику PyMax)."""
    if response is None:
        return None
    try:
        from pymax.api.messages.service import (
            payload_item,
            MessagePayloadKey,
            bind_api_model,
            require_payload_model,
        )
        from pymax.types.domain.message import ReactionInfo
        reaction_info = payload_item(response, MessagePayloadKey.REACTION_INFO)
        if reaction_info is None:
            return None
        return bind_api_model(
            app,
            require_payload_model(reaction_info, ReactionInfo),
        )
    except Exception as exc:
        logger.debug(
            "pymax_patches: parse ReactionInfo failed (non-critical): %s",
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Patch 2: dispatch EVENT_MAP → NOTIF_MSG_YOU_REACTED → REACTION_UPDATE
# ---------------------------------------------------------------------------


def _patch_event_map_you_reacted() -> None:
    """PyMax 2.2.0: ``Opcode.NOTIF_MSG_YOU_REACTED`` отсутствует в ``EVENT_MAP``.

    Вендорный ``mapping.EVENT_MAP`` содержит только:
        Opcode.NOTIF_MSG_REACTIONS_CHANGED → resolve_reaction_update

    Но MAX-сервер на собственную реакцию пользователя (владельца моста)
    шлёт **отдельное** событие с opcode=156 (``NOTIF_MSG_YOU_REACTED``),
    а не ``NOTIF_MSG_REACTIONS_CHANGED``. Без этого патча dispatcher
    не может зарезолвить фрейм и сбрасывает его в ``on_raw`` —
    обработчик ``on_reaction_update`` никогда не вызывается, и
    мост не зеркалит реакции владельца в Telegram.

    Решение: добавляем ``NOTIF_MSG_YOU_REACTED`` → ``resolve_reaction_update``
    в вендорный ``EVENT_MAP``. После фикса upstream этот патч можно
    просто удалить.
    """
    from pymax.dispatch import mapping as dispatch_mapping
    from pymax.protocol import Opcode

    opcode = getattr(Opcode, "NOTIF_MSG_YOU_REACTED", None)
    if opcode is None:
        logger.warning(
            "pymax_patches: Opcode.NOTIF_MSG_YOU_REACTED not found, skipping",
        )
        return

    event_map = dispatch_mapping.EVENT_MAP
    if opcode in event_map:
        logger.debug(
            "pymax_patches: EVENT_MAP already has NOTIF_MSG_YOU_REACTED, "
            "skipping",
        )
        return

    event_map[opcode] = dispatch_mapping.resolve_reaction_update
    logger.info(
        "pymax_patches: EVENT_MAP[NOTIF_MSG_YOU_REACTED] → "
        "resolve_reaction_update (registered)",
    )
