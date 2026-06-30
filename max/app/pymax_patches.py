"""Monkey-patches для PyMax 2.2.0, которые не вошли в upstream.

Зачем этот модуль
------------------

PyMax 2.2.0 (вендор, см. ``vendor/pymax/``) содержит ряд багов в
обработке реакций. Все они обходятся через monkey-patches в этом
файле. Патчи применяются **до** старта ``Client.start()``, чтобы
перехватить и входящий, и исходящий поток данных.

Список активных патчей:

* **Patch 1**: ``add_reaction`` / ``remove_reaction`` шлют ``messageId``
  как ``int`` (MAX-сервер ожидает int64).
* **Patch 2**: ``EVENT_MAP[NOTIF_MSG_YOU_REACTED]`` → ``REACTION_UPDATE``
  (без этого dispatcher не видит событие «вы поставили реакцию»).
* **Patch 3**: ``ReactionUpdateEvent.model_validate`` coerce
  ``messageId`` int → str (MAX-сервер шлёт int в payload).
 * **Patch 4**: ``App.on_event`` не валит long-poll при ошибке парсинга
   payload (один битый фрейм больше не рвёт соединение).
 * **Patch 5**: ``get_reactions`` шлёт ``messageIds`` как ``list[int]``
   (MAX-сервер ожидает int, а вендорный ``GetReactionsPayload``
   объявляет ``list[str]``).
 * **Patch 6**: ``Dispatcher.dispatch`` для ``Opcode.NOTIF_CHAT`` (135)
   дополнительно синтетически эмитит ``REACTION_UPDATE`` из
   ``chat.lastReactedMessageId`` / ``lastReaction`` /
   ``chat.lastMessage.reactionInfo`` — без этого MAX-сервер
   уведомляет об обновлении реакции через ``NOTIF_CHAT``, который
   в вендорном ``EVENT_MAP`` маппится в ``CHAT_UPDATE`` (а не
   ``REACTION_UPDATE``), и ``@client.on_reaction_update()`` никогда
   не срабатывает.

После того, как upstream починит соответствующие баги, все шесть
патчей можно удалить — наш мост продолжит работать корректно.

Применение
----------

:func:`apply` вызывается из :func:`max.app.bridge.register_bridge`
**до** того, как ``Client.start()`` начнёт слушать long-poll.

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
    _patch_get_reactions_message_ids_int()
    _patch_event_map_you_reacted()
    _patch_reaction_event_message_id_coerce()
    _patch_dispatcher_chat_reaction()
    _patch_app_on_event_safe()
    _APPLIED = True
    logger.info(
        "pymax_patches.apply: applied "
        "(out msgId→int, in EVENT_MAP[YOU_REACTED], "
        "in ReactionUpdateEvent.messageId coerce int→str, "
        "in Dispatcher NOTIF_CHAT→synthetic REACTION_UPDATE, "
        "App.on_event safe, get_reactions messageIds→int)",
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
    падает с ValidationError и событие реакции теряется.

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


# ---------------------------------------------------------------------------
# Patch 6: Dispatcher.dispatch — NOTIF_CHAT (135) → синтетический REACTION_UPDATE
# ---------------------------------------------------------------------------


def _patch_dispatcher_chat_reaction() -> None:
    """PyMax 2.2.0: MAX-сервер шлёт обновления реакции через ``NOTIF_CHAT``.

    Вендорный ``mapping.EVENT_MAP`` маппит ``Opcode.NOTIF_CHAT`` (135) на
    :func:`resolve_chat` → :attr:`EventType.CHAT_UPDATE`, а **не** на
    ``REACTION_UPDATE``. Соответственно обработчик
    ``@client.on_reaction_update()`` в :mod:`app.bridge` никогда не
    вызывается, и мост MAX → TG не зеркалит реакции.

    В логах (``MAX_BRIDGE_RAW_LOG=all``) подтверждено: при лайке в MAX
    приходит **только** ``opcode=135`` с ``chat.lastReactedMessageId`` /
    ``chat.lastReaction`` (а иногда и ``chat.lastMessage.reactionInfo``).
    ``NOTIF_MSG_REACTIONS_CHANGED`` (155) и ``NOTIF_MSG_YOU_REACTED`` (156)
    не приходят вообще.

    Workaround: подменяем :meth:`Dispatcher.dispatch` так, чтобы:

    1. До вызова ``_orig_dispatch`` проверить ``frame.opcode == NOTIF_CHAT``
       и ``frame.cmd == Command.REQUEST`` (т.е. это именно событие,
       а не ответ/ERR).
    2. Из ``frame.payload["chat"]`` достать ``lastReactedMessageId`` /
       ``lastReaction`` (и, если есть, ``lastMessage.reactionInfo``).
    3. Сконструировать синтетический :class:`ReactionUpdateEvent` через
       ``model_validate`` (это пропустит нас через Patch 3 — coerce
       ``messageId`` int → str).
    4. Вызвать ``dispatcher._dispatch_to_router(dispatcher.root_router,
       EventType.REACTION_UPDATE, evt)`` — это пропустит ``mapper.map``
       (нам не нужно заново парсить chat в ``Chat``), но запустит уже
       зарегистрированный ``@client.on_reaction_update()``.
    5. Продолжить ``_orig_dispatch(frame)`` — обычная обработка
       ``CHAT_UPDATE`` (``@client.on_chat_update()``) не ломается.
    6. Всё в ``try/except`` — один битый синтетический event не должен
       валить long-poll (Patch 4 глушит ошибки ``App.on_event`` сверху,
       здесь дублируем защиту: при сбое просто пропускаем синтетический
       REACTION_UPDATE и идём в ``_orig_dispatch``).

    Идемпотентен по флагу на методе. После фикса upstream
    (добавление ``NOTIF_CHAT`` → ``REACTION_UPDATE`` либо отдельный
    opcode для реакции) этот патч можно удалить.
    """
    from pymax.dispatch import dispatcher as dispatch_dispatcher
    from pymax.dispatch.enums import EventType
    from pymax.protocol import Opcode
    from pymax.protocol.enums import Command
    from pymax.types.events.reaction import ReactionUpdateEvent

    target = dispatch_dispatcher.Dispatcher.dispatch
    if getattr(target, "_pymax_patched_chat_reaction", False):
        logger.debug(
            "pymax_patches: Dispatcher.dispatch already patched "
            "(chat→synthetic REACTION_UPDATE), skipping",
        )
        return

    notif_chat = getattr(Opcode, "NOTIF_CHAT", None)
    reaction_evt = getattr(EventType, "REACTION_UPDATE", None)
    if notif_chat is None or reaction_evt is None:
        logger.warning(
            "pymax_patches: Opcode.NOTIF_CHAT=%s or "
            "EventType.REACTION_UPDATE=%s missing, skipping Patch 6",
            notif_chat, reaction_evt,
        )
        return

    _orig_dispatch = target

    async def _patched_dispatch(self, frame: Any) -> None:
        # 1) Пре-условие: только для NOTIF_CHAT-REQUEST пытаемся синтезировать
        #    REACTION_UPDATE. Для остальных opcodes — идём в оригинал.
        opcode = getattr(frame, "opcode", None)
        cmd = getattr(frame, "cmd", None)
        if (
            opcode is not None
            and int(opcode) == int(notif_chat)
            and cmd == Command.REQUEST
        ):
            try:
                synth_evt = _build_synthetic_reaction_event(frame)
            except Exception as exc:
                logger.debug(
                    "pymax_patches: build_synthetic_reaction_event failed "
                    "(skipping synthetic REACTION_UPDATE): %s",
                    exc,
                )
                synth_evt = None
            if synth_evt is not None:
                try:
                    # Зовём приватный диспатчер вендора напрямую — обходим
                    # ``EventResolver``/``EventMapper``: событие уже готово.
                    await self._dispatch_to_router(
                        self.root_router,
                        reaction_evt,
                        synth_evt,
                    )
                    logger.info(
                        "pymax_patches: dispatched synthetic REACTION_UPDATE "
                        "chat=%s msg=%s counters=%s total=%d",
                        synth_evt.chat_id,
                        synth_evt.message_id,
                        [getattr(c, "reaction", "?")
                         for c in (synth_evt.counters or [])],
                        int(getattr(synth_evt, "total_count", 0) or 0),
                    )
                except Exception as exc:
                    # Не валим long-poll: на ошибке — пропускаем синтетику
                    # и идём в ``_orig_dispatch`` (CHAT_UPDATE отработает).
                    logger.warning(
                        "pymax_patches: synthetic REACTION_UPDATE dispatch "
                        "failed (skipping, going to original): %s: %s",
                        type(exc).__name__, exc,
                    )
        # 2) Всегда — обычный dispatch (CHAT_UPDATE и on_raw).
        await _orig_dispatch(self, frame)

    _patched_dispatch._pymax_patched_chat_reaction = True  # type: ignore[attr-defined]
    dispatch_dispatcher.Dispatcher.dispatch = _patched_dispatch
    logger.info(
        "pymax_patches: Dispatcher.dispatch → NOTIF_CHAT "
        "synthesises REACTION_UPDATE from chat.lastReactedMessageId / "
        "lastReaction / lastMessage.reactionInfo (registered)",
    )


def _build_synthetic_reaction_event(frame: Any) -> Optional[Any]:
    """Построить :class:`ReactionUpdateEvent` из NOTIF_CHAT payload.

    Извлекаем:

    * ``chat_id`` — ``chat.chatId`` / ``chat.chat_id`` / ``chat.id``.
    * ``message_id`` — ``chat.lastReactedMessageId`` /
      ``chat.last_reacted_message_id`` (int64).
    * ``last_reaction`` — ``chat.lastReaction`` / ``chat.last_reaction``
      (emoji-строка). Может быть пустой строкой при снятии реакции —
      в этом случае counters=[] и total=0, событие не синтезируем.
    * Если в ``chat.lastMessage.reactionInfo`` есть счётчики — берём их.
      ``your_reaction`` из этого источника обычно не приходит
      (MAX-сервер его не кладёт в chat.lastMessage), но обработчик
      ``@client.on_reaction_update()`` всё равно дотянет правду через
      :meth:`Client.get_reactions` (см. Patch 5).
    """
    from pymax.types.events.reaction import ReactionUpdateEvent

    payload = getattr(frame, "payload", None) or {}
    if not isinstance(payload, dict):
        return None
    chat = payload.get("chat") or {}
    if not isinstance(chat, dict):
        return None

    chat_id = (
        chat.get("chatId")
        or chat.get("chat_id")
        or chat.get("id")
    )
    if chat_id is None:
        return None
    try:
        chat_id_int = int(chat_id)
    except (TypeError, ValueError):
        return None

    message_id = (
        chat.get("lastReactedMessageId")
        or chat.get("last_reacted_message_id")
    )
    if message_id is None:
        return None

    last_reaction = (
        chat.get("lastReaction")
        or chat.get("last_reaction")
    )
    last_reaction = str(last_reaction) if last_reaction else ""

    last_message = (
        chat.get("lastMessage")
        or chat.get("last_message")
        or {}
    )
    if not isinstance(last_message, dict):
        last_message = {}
    reaction_info = (
        last_message.get("reactionInfo")
        or last_message.get("reaction_info")
        or {}
    )
    if not isinstance(reaction_info, dict):
        reaction_info = {}

    counters_raw = reaction_info.get("counters") or []
    total_raw = reaction_info.get("totalCount")
    if total_raw is None:
        total_raw = reaction_info.get("total_count")

    counters: list[dict[str, Any]] = []
    if isinstance(counters_raw, list):
        for c in counters_raw:
            if not isinstance(c, dict):
                continue
            reaction = c.get("reaction") or c.get("id")
            count = c.get("count")
            if reaction is None or count is None:
                continue
            try:
                counters.append({
                    "reaction": str(reaction),
                    "count": int(count),
                })
            except (TypeError, ValueError):
                continue

    if counters:
        try:
            total = int(total_raw) if total_raw is not None else sum(
                c["count"] for c in counters
            )
        except (TypeError, ValueError):
            total = sum(c["count"] for c in counters)
    elif last_reaction:
        # Нет полного reactionInfo, но ``lastReaction`` есть (лайк) —
        # синтезируем минимальный счётчик из 1.
        counters = [{"reaction": last_reaction, "count": 1}]
        total = 1
    else:
        # Ничего полезного: сняли реакцию и сервер не прислал счётчики.
        # Чтобы ``on_reaction_update`` корректно снял зеркальную реакцию
        # в TG, всё равно шлём пустое событие: обработчик сам разберётся
        # (``your_reaction`` через ``get_reactions`` даст None → no-op).
        counters = []
        total = 0

    evt_obj = {
        "messageId": message_id,  # int — Patch 3 приведёт к str
        "chatId": chat_id_int,
        "counters": counters,
        "totalCount": total,
    }
    # ``model_validate`` (пропатченный Patch 3) coerce-ит int→str для
    # messageId, остальные поля Pydantic валидирует сам.
    return ReactionUpdateEvent.model_validate(evt_obj)


# ---------------------------------------------------------------------------
# Patch 4: App.on_event — не валить long-poll при ошибке парсинга
# ---------------------------------------------------------------------------


def _patch_app_on_event_safe() -> None:
    """PyMax 2.2.0: ``App.on_event`` роняет ``RuntimeError`` при любой
    ошибке dispatcher'а и **рвёт long-poll**.

    Симптом в логах::

        RuntimeError: Failed to dispatch inbound frame: ...

    Один битый фрейм (например, новая схема события реакции с
    дополнительным полем, или ``User.gender: int`` вместо ``str`` —
    сервер уже отдаёт такие payload-ы) валит ВСЕ последующие события,
    пока long-poll не переподключится. После реконнекта сервер может
    снова прислать тот же битый payload — и цикл повторяется.

    Workaround: подменяем ``App.on_event`` так, чтобы при ошибке
    логировать её и **пропускать фрейм**, но **не raise** — long-poll
    продолжает работать. Ошибка попадает в ``logger.warning``, чтобы
    её можно было увидеть в Railway-логах.

    Идемпотентен по флагу на методе. После того, как upstream
    сделает dispatcher более устойчивым, патч можно удалить.
    """
    from pymax.app import App

    if getattr(App.on_event, "_pymax_patched_safe", False):
        logger.debug(
            "pymax_patches: App.on_event already patched (safe), skipping",
        )
        return

    _orig_on_event = App.on_event

    async def _safe_on_event(self, frame: Any) -> None:
        opcode = getattr(frame, "opcode", "?")
        cmd = getattr(frame, "cmd", "?")
        seq = getattr(frame, "seq", "?")
        logger.debug(
            "pymax_patches: on_event opcode=%s cmd=%s seq=%s (safe wrapper)",
            opcode, cmd, seq,
        )
        try:
            await _orig_on_event(self, frame)
        except Exception as exc:
            # Ошибка парсинга payload (часто — mismatch типов в модели vs.
            # реальный JSON от сервера). Не валим long-poll, просто
            # логируем — следующие фреймы продолжат обрабатываться.
            try:
                payload_repr = repr(getattr(frame, "payload", None))[:500]
            except Exception:
                payload_repr = "<unreprable>"
            logger.warning(
                "pymax_patches: App.on_event swallowed exception "
                "opcode=%s cmd=%s seq=%s: %s: %s | payload=%s",
                opcode, cmd, seq, type(exc).__name__, exc, payload_repr,
            )

    _safe_on_event._pymax_patched_safe = True  # type: ignore[attr-defined]
    App.on_event = _safe_on_event
    logger.info(
        "pymax_patches: App.on_event → safe wrapper "
        "(no raise on ValidationError, long-poll survives) (registered)",
    )


# ---------------------------------------------------------------------------
# Patch 5: get_reactions → messageIds as list[int]
# ---------------------------------------------------------------------------


def _patch_get_reactions_message_ids_int() -> None:
    """PyMax 2.2.0: ``GetReactionsPayload.message_ids: list[str]``,
    но MAX-сервер ожидает ``list[int]`` (так же, как и в Patch 1).

    Симптом в логах::

        ERROR pymax.app: api error opcode=180 seq=N error=proto.payload
          title=Ошибка валидации message=Expected number at 26

    И затем мост не знает, поставил ли владелец свою реакцию —
    ``client.get_reactions`` возвращает ошибку → ``your_reaction`` = None →
    зеркальная реакция MAX → TG для сообщения оппонента не ставится.

    Workaround: подменяем ``MessageService.get_reactions`` так, чтобы
    шлить payload как dict с ``messageIds`` = ``[int(msg_id), ...]``,
    минуя Pydantic-валидацию ``GetReactionsPayload``.

    После фикса upstream (``message_ids: list[int]``) этот патч можно
    удалить — он идемпотентен.
    """
    from pymax.api.messages import service as msgs_service
    from pymax.protocol import Opcode

    _orig_get = msgs_service.MessageService.get_reactions
    if getattr(_orig_get, "_pymax_patched_msgids", False):
        logger.debug(
            "pymax_patches: MessageService.get_reactions already patched, "
            "skipping",
        )
        return

    async def _patched_get_reactions(
        self,
        chat_id: Any,
        message_ids: Any,
    ) -> Optional[Any]:
        """Замена :meth:`MessageService.get_reactions` с int message_ids."""
        # Шлём payload как dict, минуя GetReactionsPayload (там list[str]).
        try:
            ids_int = [int(m) for m in message_ids]
        except Exception:
            ids_int = list(message_ids or [])
        payload = {
            "chatId": int(chat_id),
            "messageIds": ids_int,  # ← list[int], а не list[str]
        }
        try:
            response = await self.app.invoke(Opcode.MSG_GET_REACTIONS, payload)
        except Exception as exc:
            logger.warning(
                "pymax_patches: get_reactions failed chat=%s ids=%s: %s",
                chat_id, message_ids, exc,
            )
            raise
        # Разбор ответа — повторяем логику оригинального метода:
        # ``messagesReactions`` (dict[message_id → reaction_data]).
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
            messages_reactions = payload_item(
                response, MessagePayloadKey.MESSAGES_REACTIONS,
            )
            if not isinstance(messages_reactions, dict):
                return None
            return {
                str(mid): bind_api_model(
                    self.app,
                    require_payload_model(r_data, ReactionInfo),
                )
                for mid, r_data in messages_reactions.items()
            }
        except Exception as exc:
            logger.debug(
                "pymax_patches: parse ReactionInfo map failed (non-critical): %s",
                exc,
            )
            return None

    _patched_get_reactions._pymax_patched_msgids = True  # type: ignore[attr-defined]
    msgs_service.MessageService.get_reactions = _patched_get_reactions
    logger.info(
        "pymax_patches: MessageService.get_reactions → "
        "messageIds as list[int] (registered)",
    )