"""Пакет хэндлеров Telegram-бота (этап 2, PyMax).

Структура (по доменам):

* ``_common`` — общие хелперы (``_is_allowed``, ``_reject``, ``_escape``,
  ``_format_chat``, константы лимитов).
* ``basic`` — ``/start``, ``/help``, ``/status``, ``/chats``, ``/history``
  + reply-кнопки.
* ``_supergroup`` — ``/setup``, ``/setgroup``, ``/autosetup``, ``/getlink``
  + ``AttachResult``, ``_attach_supergroup_for_owner``.
* ``reply`` — ``/reply``, ``/cancel``, FSM ``reply_text`` / ``reply_media``.
* ``sessions`` — ``/upload_session``, ``/sessions``, FSM загрузки файла
  + inline-кнопки выбора session.
* ``auth`` — ``/reauth_sms``, ``/code``, ``event_action_callback``,
  ``auth_action_callback``, ``topic_message_to_max``.
* ``prune_topics`` — ``/prune_topics``, ``prune_topic_callback``.
* ``auth_watcher`` — фоновый ``AuthWatcher`` (поллер ``/status`` → уведомления).
* ``registration`` — единственная публичная ``register_handlers(dp)``.

Импорт через ``from app.handlers import register_handlers, AuthWatcher``
сохраняет обратную совместимость со старым ``handlers.py``.
"""

from app.handlers.auth_watcher import AuthWatcher  # noqa: F401
from app.handlers.registration import register_handlers  # noqa: F401