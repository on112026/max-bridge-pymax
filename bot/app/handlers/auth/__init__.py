"""Пакет ``handlers/auth`` — логика авторизации MAX и «эхо» из топиков.

Структура (4 модуля по доменам):

* ``reauth``         — ``/reauth_sms``, ``/code`` (текстовые команды).
* ``event_callbacks`` — ``event_action_callback`` (inline-кнопки под
                       сообщениями из MAX: reply/showid/history).
* ``auth_action``    — ``auth_action_callback`` (inline-кнопки выбора
                       способа авторизации: sms/session/upload/cancel).
* ``topic_echo``     — ``topic_message_to_max`` (эхо из топика супергруппы
                       в MAX без команды ``/reply``).

``registration.py`` импортирует всё явно:

* ``from app.handlers.auth.reauth import reauth_sms_command, code_command``
* ``from app.handlers.auth.event_callbacks import event_action_callback``
* ``from app.handlers.auth.auth_action import auth_action_callback``
* ``from app.handlers.auth.topic_echo import topic_message_to_max``
"""

from app.handlers.auth.auth_action import auth_action_callback  # noqa: F401
from app.handlers.auth.event_callbacks import event_action_callback  # noqa: F401
from app.handlers.auth.reauth import code_command, reauth_sms_command  # noqa: F401
from app.handlers.auth.topic_echo import topic_message_to_max  # noqa: F401