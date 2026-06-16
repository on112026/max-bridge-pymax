# max-bridge-pymax

Личный «мост» между мессенджером **MAX** и **Telegram-ботом**, работающий
через официальную Python-библиотеку
[PyMax](https://github.com/MaxApiTeam/PyMax) **2.2.0** (вендорится в
[`vendor/pymax/`](./vendor/pymax/)). Без Playwright, без Chromium, без
VNC, без виртуального дисплея.

> Этап 2: полноценный мост из 3 процессов (api + bot + max) в одном
> контейнере под supervisord.

## Что внутри

```
MAX (PyMax 2.2.0)  ──┐
                     │  HTTP (X-Api-Key)
                     ▼
            ┌─────────────────┐         ┌────────────────┐
            │  api (FastAPI)  │◄────────┤  bot (aiogram) │
            │  SQLite /events │         │  /start /chats │
            │  /send /chats   │         │  /reply /code  │
            │  /auth/2fa/*    │         └────────────────┘
            └─────────────────┘
                     ▲
                     │  POST /events /chats
                     │
            ┌─────────────────┐
            │  max (PyMax)    │
            │  client.start() │
            │  sender loop    │
            └─────────────────┘
```

- **`api`** — FastAPI + uvicorn. Хранит события, очередь отправки,
  список чатов и состояние авторизации. Единственный «общий» компонент.
- **`bot`** — aiogram 3. Команды `/start`, `/chats`, `/reply`, `/code`,
  `/reauth_sms`, `/status`. Рассылает события в Telegram, отправляет
  ответы, забирает SMS-коды.
- **`max`** — long-running процесс с PyMax-клиентом. Слушает входящие,
  кладёт события в API, забирает из API сообщения для отправки, шлёт
  в MAX.

Все три живут в одном Docker-контейнере под `supervisord`. На хосте —
тома `/data` (БД + медиа) и `/app/cache` (сессия PyMax).

## Что мы шлём в Telegram

| В MAX | В Telegram |
| --- | --- |
| Текст | текст + Markdown-экранирование |
| Фото (одно) | `send_photo` + caption |
| Видео (одно, ≤ 49 МБ) | `send_video` + caption |
| Файл (≤ 49 МБ) | `send_document` + caption |
| Голосовое / стикер / аудио / видео-сообщение | текст «🎤 Голосовое сообщение (N сек)» + плейсхолдер |
| Несколько вложений | берём первое; остальные отбрасываем (Telegram API принимает одно) |

49 МБ — лимит Telegram Bot API для `sendDocument` / `sendVideo` при
аплоаде через локальный URL.

## Что мы шлём в MAX

Из Telegram в MAX уходит текст и **ровно одно** вложение
(`Photo`/`Video`/`File` из PyMax), как описано в `/reply` бота.

## Авторизация

MAX требует SMS-код (или 2FA-пароль). Мы не показываем вэб-форму — код
забирает владелец через Telegram-бот.

Флоу при первом запуске:

1. Контейнер поднимается, max-процесс создаёт PyMax-клиент.
2. PyMax зовёт `SmsCodeProvider.get_code()` →
   `POST /auth/2fa/request` → регистрирует `rid` в in-memory очереди.
3. Бот раз в 3 секунды опрашивает `/status`. Видит `need_2fa` → шлёт
   владельцу сообщение: «🔐 MAX запрашивает код…».
4. Владелец пишет боту: `/code 12345`.
5. Бот → `PUT /auth/2fa/{rid}` → провайдер возвращает код → PyMax
   логинится → `on_start` ставит `auth.status=ok`.
6. Бот видит `ok` → пишет «✅ MAX: вход выполнен успешно».

Сессия сохраняется в `CACHE_DIR/main.db`. Повторно SMS не спрашивается,
пока не сделать:

- `make wipe-cache` (полная очистка кэша),
- или `/reauth_sms` в боте (бот просит supervisor пересоздать клиент с
  чистого листа).

## Структура

```
max-bridge-pymax/
├── README.md
├── .env.example                # заполнить и скопировать в .env
├── .gitignore
├── .dockerignore
├── requirements.txt            # PyMax-зависимости
├── Dockerfile                  # python:3.11-slim + supervisord
├── supervisord.conf            # 3 процесса: api, bot, max
├── docker-compose.yaml         # один сервис, тома /data и /app/cache
├── Makefile                    # бытовые команды
│
├── vendor/                     # вендор PyMax 2.2.0
│   ├── README.md
│   ├── VERSION
│   └── pymax/
│
├── shared/                     # общие модули (импортируются всеми тремя)
│   ├── config.py
│   ├── db.py
│   ├── models.py
│   ├── http_client.py
│   ├── api_auth.py
│   └── log_setup.py
│
├── api/                        # FastAPI-приложение
│   ├── main.py                 # все роуты
│   ├── run.py                  # uvicorn
│   └── requirements.txt
│
├── bot/                        # aiogram-бот
│   ├── run.py
│   ├── app/
│   │   ├── handlers.py
│   │   ├── api_client.py
│   │   ├── forwarder.py        # опрос /events → шлёт в Telegram
│   │   ├── sender.py           # /reply → POST /send
│   │   ├── keyboards.py
│   │   ├── states.py
│   │   └── config.py
│   └── requirements.txt
│
├── max/                        # PyMax-клиент + мост
│   ├── run.py                  # точка входа
│   ├── app/
│   │   ├── auth.py             # QueueSmsCodeProvider, QueuePasswordProvider
│   │   ├── bridge.py           # on_start, on_message, on_chat_update
│   │   ├── sender.py           # sender_loop — GET /send/next → send_message
│   │   ├── supervisor.py       # главный цикл: создаёт Client, реагирует на reauth
│   │   └── config.py
│   └── requirements.txt
│
│ # Артефакты PoC (этап 1), оставлены для истории:
├── Dockerfile.poc
├── docker-compose.poc.yaml
├── run_poc.py
└── cache/                      # main.db PyMax после PoC (gitignore)
```

## Требования

- Docker Engine ≥ 24, docker compose v2.
- 512 МБ RAM хватит (PyMax не прожорлив).
- Свободный TCP-порт `API_PORT` (по умолчанию 8000) — если пробрасываете
  наружу. Внутри одного контейнера все три процесса ходят на
  `http://127.0.0.1:${API_PORT}`.

## Быстрый старт (локально)

```bash
cd max-bridge-pymax

# 1) Скопировать и заполнить .env
cp .env.example .env
$EDITOR .env
#   TELEGRAM_BOT_TOKEN=...          # @BotFather
#   ALLOWED_TG_USER_IDS=123456789   # свой ID (узнать у @userinfobot)
#   MAX_PHONE=+79xxxxxxxxx
#   BRIDGE_API_KEY=...              # сгенерируйте: make gen-key

# 2) Собрать и поднять
make build
make up
make logs
```

При первом запуске в логах появится что-то вроде:

```
max  | [WARN] reauth requested by owner, wiping cache and recreating client
api  | INFO:     127.0.0.1 - "GET /status" 200 OK
```

MAX пришлёт SMS. Бот пришлёт вам в Telegram:

> 🔐 MAX запрашивает код. Откройте SMS и ответьте: `/code 12345`.

После ввода кода:

> ✅ MAX: вход выполнен успешно.

Отправьте себе сообщение с другого клиента MAX — оно появится в
Telegram.

## Повседневные команды (`make help`)

```bash
make help            # полный список
make logs            # лог всего контейнера
make ps              # статус контейнера
make status          # GET /status API
make chats           # GET /chats?limit=20
make events          # GET /events?limit=20
make shell           # bash внутрь контейнера
make supervisorctl   # supervisorctl внутрь
make restart-api     # перезапустить api внутри
make restart-bot     # перезапустить bot внутри
make restart-max     # перезапустить max внутри

make wipe-cache      # стереть сессию PyMax (== reauth с нуля)
make wipe-data       # ⚠️ стереть /data (БД + медиа)
make nuke            # down + wipe-data
make gen-key         # сгенерировать BRIDGE_API_KEY
```

## Деплой на Railway

1. Залить репозиторий (или подключить текущий) в Railway.
2. **New Project → Deploy from GitHub → выбрать репо**.
3. Railway сам подхватит `Dockerfile`. Переменные окружения задать
   через **Variables** (`TELEGRAM_BOT_TOKEN`, `ALLOWED_TG_USER_IDS`,
   `MAX_PHONE`, `BRIDGE_API_KEY`, при необходимости `API_PORT`).
4. В **Settings → Volumes** добавить два тома:
   - `/data` (для SQLite + скачанных медиа),
   - `/app/cache` (для сессии PyMax).
5. **Settings → Healthcheck Path**: `/health`.
6. Дождаться деплоя. Логи — во вкладке **Logs** (`supervisord` сразу
   видно).
7. SMS-флоу проходит так же, как локально: бот пришлёт вам код-реквест
   в Telegram.

> **Только один контейнер.** Railway-сервис — это один Dockerfile, у
> нас внутри supervisord с 3 процессами. Пробрасывать наружу порт
> `API_PORT` необязательно: бот ходит в API по `127.0.0.1` внутри
> одного контейнера. Порт нужен, только если хотите смотреть
> `/status` снаружи — тогда включите **Settings → Networking → Port**.

## Troubleshooting

### «MAX запрашивает код» прилетает при каждом рестарте
Скорее всего, том `/app/cache` не сохраняется между запусками.
- **Локально**: проверьте, что в `docker-compose.yaml` остался том
  `bridge_cache`.
- **Railway**: добавьте том `/app/cache` в **Settings → Volumes**.

### Бот молчит после `/start`
Проверьте, что ваш Telegram ID есть в `ALLOWED_TG_USER_IDS`. Бот
намеренно fail-closed: если список пуст — он не отвечает никому.

### `auth.status=unknown`
Max-процесс не смог стартовать. Смотрите `make restart-max` →
`make logs`. Типичные причины:
- неверный `MAX_PHONE` (нужен `+…` с плюсом),
- провайдер MAX выдаёт captcha (PyMax не умеет) — попробуйте позже.

### Бот не прислал «MAX запрашивает код», хотя MAX точно прислал SMS
Проверьте `make status`. Должно быть `"auth": {"status": "need_2fa", ...}`.
Если статус `unknown` — значит supervisor ещё не успел обновить state.
Подождите до 10 секунд.

### Медиа не доходят в Telegram
- В MAX видео больше 49 МБ? Telegram Bot API не примет.
- Бот поддерживает только **одно** вложение за раз. Если в MAX
  прислали 3 фото — в Telegram уйдёт первое, остальные отбросятся.

### Полный сброс
```bash
make nuke && make up
```

## Этап 1 (PoC) — история

В репозитории остались артефакты PoC (`Dockerfile.poc`,
`docker-compose.poc.yaml`, `run_poc.py`) — минимальный клиент, чтобы
проверить, что PyMax реально логинится в MAX и принимает входящие.

PoC проверен локально на аккаунте `Aleksander`:

| Параметр | Значение |
| --- | --- |
| `me` (user id) | `302084623` |
| Чатов в первом sync | `3` |
| Входящие | принято реальное сообщение «Новый вход в MAX» |
| Повторный запуск без SMS | ✅ (сессия читается из `cache/main.db`) |

Запустить PoC:

```bash
make poc-build
make poc-up
# Ctrl+C для выхода
make poc-down
```

## Лицензия

Только для личного использования. PyMax использует неофициальный API
MAX — используйте на свой страх и риск.