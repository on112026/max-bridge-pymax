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
    _patch_reaction_event_message_id_coerce()
    _APPLIED = True
    logger.info(
        "pymax_patches.apply: applied (out msgId→int, in EVENT_MAP[YOU_REACTED], "
        "in ReactionUpdateEvent.messageId coerce int→str)",
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


# ---------------------------------------------------------------------------
# Patch 3: ReactionUpdateEvent.model_validate → coerce messageId int→str
# ---------------------------------------------------------------------------


def _patch_reaction_event_message_id_coerce() -> None:
    """PyMax 2.2.0: ``ReactionUpdateEvent.message_id: str`` валит фрейм.

    MAX-сервер шлёт в JSON ``"messageId": <int64>`` (число), а Pydantic-модель
    :class:`ReactionUpdateEvent` объявляет поле как ``str``. Pydantic 2.x
    со строгим режимом **не** coerce-ит int → str, поэтому dispatcher
    падает с::

        pydantic_core._pydantic_core.ValidationError: 1 validation error
        for ReactionUpdateEvent
        messageId
          Input should be a valid string [type=string_type,
          input_value=116838091054923435, input_type=int]

    Это симметричный баг к Patch 1: на **исходящей** стороне PyMax ждёт
    int (Patch 1 чинит), на **входящей** — ждёт str (Patch 3 чинит).
    Без Patch 3 dispatcher роняет фрейм в ``RuntimeError("Failed to
    dispatch inbound frame")``, событие реакции теряется и мост не
    зеркалит реакции MAX → TG.

    Workaround: подменяем ``ReactionUpdateEvent.model_validate`` так,
    чтобы перед валидацией привести ``messageId`` из int в str (если
    он int). Другие поля не трогаем — Pydantic сам coerce-ит chat_id,
    counters и total_count корректно.

    После фикса upstream (``message_id: Union[int, str]``) этот патч
    можно удалить — он идемпотентен.
    """
    from pymax.types.events.reaction import ReactionUpdateEvent

    if getattr(ReactionUpdateEvent.model_validate, "_pymax_patched_coerce", False):
        logger.debug(
            "pymax_patches: ReactionUpdateEvent.model_validate already patched, "
            "skipping",
        )
        return

    _orig_validate = ReactionUpdateEvent.model_validate

    @classmethod  # type: ignore[no-redef]
    def _patched_validate(cls, obj, *args, **kwargs):
        # ``obj`` обычно dict (или уже модель). Меняем только если есть
        # ``messageId`` и он int — иначе оставляем всё как есть.
        try:
            if isinstance(obj, dict) and "messageId" in obj:
                mid = obj["messageId"]
                if isinstance(mid, int) and not isinstance(mid, bool):
                    obj = dict(obj)
                    obj["messageId"] = str(mid)
        except Exception as exc:
            logger.debug(
                "pymax_patches: pre-validate messageId coerce failed: %s",
                exc,
            )
        return _orig_validate.__func__(cls, obj, *args, **kwargs)

    _patched_validate._pymax_patched_coerce = True  # type: ignore[attr-defined]
    ReactionUpdateEvent.model_validate = _patched_validate
    logger.info(
        "pymax_patches: ReactionUpdateEvent.model_validate → "
        "coerce messageId int→str (registered)",
    )
