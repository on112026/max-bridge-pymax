"""Telegram-бот: команды chat-операций MAX (join / invite / заявки / поиск).

Структура пакета (по поддоменам, чтобы при добавлении новых команд
— например поиска по имени группы/канала, приглашений по нику —
каждая фича жила в своём файле):

* ``_common``       — общие хелперы: парсинг user_id, форматирование результатов.
* ``join``          — ``/join`` / ``/resolve`` (вступление + превью + кнопки).
* ``invite``        — ``/invite`` / ``/search_user`` (приглашение + поиск по тел.).
* ``join_requests`` — ``/pending`` / ``/approve`` / ``/decline``.
* ``help``          — ``/chatops`` — справка по командам пакета.

Регистрация всех хендлеров — в :func:`register_handlers`, вызывается
из :mod:`app.handlers.registration`.

Сценарий использования
-----------------------

Владелец управляет MAX-мостом через эти команды::

    /resolve <ссылка>                    # превью чата без вступления
    /join <ссылка>                       # вступить в группу/канал
    /invite <chat_id> <user_id>          # пригласить по user_id
    /invite <chat_id> <+79...>           # пригласить (найти по телефону)
    /search_user <+79...>                # найти user_id по номеру
    /pending <chat_id>                   # список заявок на вступление
    /approve <chat_id> <user_id> [...]   # принять заявки
    /decline <chat_id> <user_id> [...]   # отклонить заявки
    /chatops                              # краткая справка

Все операции идут через очередь ``chat_ops_queue``: бот кладёт задачу в API,
MAX-процесс (``app.chat_ops.chat_ops_loop``) её выполняет. Синхронные
операции (``resolve`` / ``list_join_requests`` / ``search_user``) ждут
результат через polling ``GET /chat_ops/{id}?wait=true``.

Ограничения pymax 2.2.0 (см. ``vendor/pymax``):

* Поиск пользователя — ТОЛЬКО по номеру телефона (``search_by_phone``).
  Поиск по имени/нику НЕ реализован в библиотеке.
* Вступление в группу — через полную ссылку ``https://max.ru/join/<token>``.
* ``invite_users_to_group`` / ``_to_channel`` принимают ``user_ids``
  (числовые id), не телефон и не имя.

Поэтому:

* Если владелец хочет пригласить по телефону — сначала ``search_user``,
  затем ``/invite`` с полученным ``user_id``.
* Если хочет вступить в группу по имени — нужно сначала получить ссылку
  в самом MAX-клиенте и прислать её боту.
"""

from __future__ import annotations

import logging

from aiogram import Dispatcher

from app.handlers.chat_ops import help as help_mod
from app.handlers.chat_ops import invite as invite_mod
from app.handlers.chat_ops import join as join_mod
from app.handlers.chat_ops import join_requests as join_requests_mod

logger = logging.getLogger(__name__)


def register_handlers(dp: Dispatcher) -> None:
    """Зарегистрировать все хендлеры пакета в ``dp``.

    ``dp`` — ``aiogram.Dispatcher``. Вызывается из
    :mod:`app.handlers.registration`.
    """
    join_mod.register_handlers(dp)
    invite_mod.register_handlers(dp)
    join_requests_mod.register_handlers(dp)
    help_mod.register_handlers(dp)