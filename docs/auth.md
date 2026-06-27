# Авторизация MAX (SMS / 2FA)

MAX требует SMS-код (или 2FA-пароль). Мы не показываем вэб-форму — код
забирает владелец через Telegram-бот.

## Flow при первом запуске

1. Контейнер поднимается, max-процесс создаёт PyMax-клиент.
2. PyMax зовёт `SmsCodeProvider.get_code()` →
   `POST /auth/2fa/request` → регистрирует `rid` в in-memory очереди.
3. Бот раз в 3 секунды опрашивает `/status`. Видит `need_2fa` → шлёт
   владельцу сообщение: «🔐 MAX запрашивает код…».
4. Владелец пишет боту: `/code 12345`.
5. Бот → `PUT /auth/2fa/{rid}` → провайдер возвращает код → PyMax
   логинится → `on_start` ставит `auth.status=ok`.
6. Бот видит `ok` → пишет «✅ MAX: вход выполнен успешно».

## Persistent session

Сессия сохраняется в `CACHE_DIR/main.db` (по умолчанию `/app/cache/main.db`).
Повторно SMS не спрашивается, пока не сделать одно из двух:

- `make wipe-cache` — полная очистка кэша PyMax.
- `/reauth_sms` в боте — бот просит supervisor пересоздать клиент с
  чистого листа.

## Компоненты, участвующие в auth-flow

| Где | Что делает |
| --- | --- |
| `max/app/auth.py` | `QueueSmsCodeProvider`, `QueuePasswordProvider` — адаптеры, которые зовут HTTP API |
| `max/app/supervisor/client_runtime.py` | `build_client(phone, cache_dir)` — создаёт `pymax.Client` с этими провайдерами |
| `max/app/supervisor/__init__.py` | главный supervisor-loop: реагирует на reauth |
| `max/app/supervisor/twofa_drain.py` | дренаж in-memory очереди 2FA-кодов |
| `api/routers/auth.py` | `/auth/2fa/request`, `/auth/2fa/{rid}` (HTTP-эндпойнты) |
| `bot/app/handlers/auth_watcher.py` | фоновый опрос `/status`, шлёт 2fa-реквесты владельцу |
| `bot/app/handlers/auth/auth_action.py` | inline-кнопки выбора способа авторизации (sms / session / cancel) |
| `bot/app/handlers/auth/reauth.py` | `/reauth_sms` — пересоздать клиент |
| `shared/db/auth_state.py` | `auth.status`: `unknown` / `need_2fa` / `ok` |

## Команды владельца для auth-flow

| Команда | Что делает |
| --- | --- |
| `/code 12345` | отдать SMS-код MAX |
| `/reauth_sms` | пересоздать MAX-клиент с чистого листа |
| Inline «🔐 SMS-авторизация» | запуск SMS-flow |
| Inline «📂 Подключиться по сессии» | использовать существующий session-файл |
| Inline «📎 Загрузить файл сессии» | загрузить новый session-файл (multipart) |

## Дальше

- Что делать, если что-то сломалось — [`troubleshooting.md`](./troubleshooting.md).
- Полный список команд бота — [`features.md`](./features.md).