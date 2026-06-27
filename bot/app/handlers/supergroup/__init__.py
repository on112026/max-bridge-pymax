"""Пакет ``handlers/supergroup`` — привязка приватной Telegram-supergroup.

Структура (2 модуля):

* ``attach``   — ``AttachResult``, ``_attach_supergroup_for_owner``
               (общий чеклист привязки: get_chat → админ → forum →
               invite-link → запись в БД), ``_format_attach_failure_for_owner``.
* ``commands`` — ``/setup``, ``/setgroup``, ``/autosetup``, ``/getlink``.

``registration.py`` импортирует команды из ``commands``:

* ``from app.handlers.supergroup.commands import setup_command, setgroup_command, autosetup_command, getlink_command``

``attach.py`` — внутренний helper для команд.
"""

from app.handlers.supergroup.attach import (  # noqa: F401
    AttachResult,
    _attach_supergroup_for_owner,
    _format_attach_failure_for_owner,
)
from app.handlers.supergroup.commands import (  # noqa: F401
    autosetup_command,
    getlink_command,
    setgroup_command,
    setup_command,
)