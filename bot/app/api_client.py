"""Клиент к внутреннему API моста (тонкий re-export для обратной совместимости).

Логика разнесена по доменам в пакете ``app.api``:

* ``app.api.core``       — ``BotApi`` (фасад над ``ApiClient``).
* ``app.api.events``     — события MAX (``list_undelivered`` и т.д.).
* ``app.api.chats``      — чаты MAX + read receipts.
* ``app.api.send``       — отправка в MAX (``enqueue_send``) + ``status``.
* ``app.api.auth``       — авторизация (``post_auth_action`` и т.д.).
* ``app.api.sessions``   — загрузка/выбор session-файлов MAX.
* ``app.api.topics``     — очереди задач синка топиков + stale-топики.

Импорт ``from app.api_client import api`` сохранён для совместимости
со старым кодом (до рефакторинга).
"""

from __future__ import annotations

from app.api import BotApiComposite

# Глобальный singleton — все хэндлеры используют ``api.method(...)``.
api = BotApiComposite()