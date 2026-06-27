"""Пакет моста PyMax → api (этап 2, без headful).

Регистрирует колбэки на готовом ``Client`` (после ``Client(...)``):

* ``@client.on_start()`` — auth_state = ok, fetch chats.
* ``@client.on_message()`` — кладём Event в api + (опц.) ChatInfo.
* ``@client.on_chat_update()`` — обновляем ChatInfo.

Структура:

* ``users`` — ``user_display_name``: имя пользователя из ``pymax.User.names``.
* ``chats`` — ``chat_to_dict``, ``display_name_of``, ``enrich_chat_titles``.
* ``media`` — ``process_photo``, ``process_video``, ``process_file``
  (скачивание вложений MAX на диск).
* ``on_start`` — ``on_start_actions``: sync чатов + sync топиков.

``max/app/bridge.py`` — публичная функция ``register_bridge(client)``,
импортируется из ``max/app/supervisor/client_runtime.py`` при создании
Client'а.
"""