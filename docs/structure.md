# Структура каталогов

Карта файлов и модулей проекта. Для каждой директории — короткая
справка, зачем она нужна.

```
max-bridge-pymax/
├── README.md                  # короткий: обзор + быстрый старт + навигация по docs/
├── docs/                      # эта папка — расширенная документация
├── .env.example               # заполнить и скопировать в .env
├── .gitignore
├── .dockerignore
├── requirements.txt           # общие: FastAPI, SQLAlchemy, pydantic, httpx
├── Dockerfile                 # python:3.11-slim + supervisord
├── supervisord.conf           # 3 процесса: api, bot, max
├── docker-compose.yaml        # один сервис, тома /data и /app/cache
├── Makefile                   # бытовые команды
│
├── vendor/                    # вендор PyMax 2.2.0 (НЕ править)
│   ├── README.md
│   ├── VERSION
│   └── pymax/
│
├── shared/                    # общие модули (импортируются всеми тремя процессами)
│   ├── config.py              # dataclass Settings из env
│   ├── log_setup.py           # единый формат логов
│   ├── api_auth.py            # FastAPI Depends verify_api_key
│   ├── http_client.py         # базовый HTTP-клиент (max-процесс → API)
│   ├── http_client_chat_ops.py # клиент для chat_ops_queue
│   ├── models.py              # общие pydantic-модели
│   └── db/                    # ORM и таблицы (см. ниже)
│
├── api/                       # FastAPI-приложение
│   ├── main.py                # FastAPI() + подключение роутеров
│   ├── run.py                 # uvicorn-entrypoint
│   ├── requirements.txt
│   └── routers/               # все эндпойнты (см. ниже)
│
├── bot/                       # aiogram-бот
│   ├── run.py                 # точка входа
│   ├── requirements.txt
│   └── app/                   # код бота (см. ниже)
│
├── max/                       # PyMax-клиент + мост
│   ├── run.py                 # точка входа
│   ├── requirements.txt       # pymax-runtime + общие
│   └── app/                   # код max-процесса (см. ниже)
│
└── tests/
    └── test_schema_migration.py
```

## `shared/db/` — таблицы SQLite

Все три процесса работают с одной и той же БД (`/data/bridge.db`).
Модули здесь — единственное место, где описаны ORM-модели и
SQL-запросы к очередям.

| Файл | Зачем |
| --- | --- |
| `_engine.py` | SQLAlchemy `Engine` + миграции схемы + `session_scope` |
| `_models.py` | ORM-модели (`EventRow`, `SendItem`, `ChatOpItem`, …) |
| `auth_state.py` | состояние MAX-клиента (`unknown` / `need_2fa` / `ok`) |
| `events.py` | входящие события MAX (max → bot) |
| `send_queue.py` | исходящие сообщения (bot → max) |
| `chats.py` | кэш MAX-чатов |
| `chat_ops_queue.py` | admin-операции (bot → max) |
| `topics.py` | маппинг MAX-чат ↔ TG-топик |
| `supergroups.py` | привязка TG-супергруппы ↔ владелец |
| `topic_jobs.py` | фоновые задачи синхронизации топиков |
| `read_receipts.py` | доставленные/прочитанные сообщения |

Подробнее про направления потоков — [`queues.md`](./queues.md).

## `api/routers/` — эндпойнты

| Файл | Эндпойнты |
| --- | --- |
| `auth.py` | `/auth/2fa/request`, `/auth/2fa/{rid}` (SMS + 2FA) |
| `chats.py` | `GET /chats`, `GET /chats/{id}` |
| `chat_ops.py` | `POST/GET /chat_ops/*` (admin-операции) |
| `chat_ops_schemas.py` | pydantic-схемы для chat_ops |
| `events.py` | `GET /events`, `POST /events` (max → bot) |
| `health.py` | `GET /health` |
| `schemas.py` | общие pydantic-схемы |
| `send.py` | `POST /send`, `GET /send/next` (bot → max) |
| `sessions.py` | `POST /sessions/upload` (multipart, загрузка MAX-сессии) |
| `status.py` | `GET /status` |
| `sync.py` | синхронизация состояния MAX ↔ БД |
| `topic_jobs.py` | `GET/POST /topic_jobs/*` |
| `topics.py` | `GET /topics`, маппинг MAX-чат ↔ топик |

## `bot/app/` — код бота

```
bot/app/
├── api_client.py       # единый HTTP-клиент к API (BotApi)
├── config.py           # бот-локальные настройки
├── forwarder.py        # EventPoller: GET /events → Telegram
├── sender.py           # /reply → POST /send
├── keyboards.py        # inline- и reply-клавиатуры
├── states.py           # FSM-состояния
├── topic_worker.py     # TopicSyncWorker: тянет /topic_jobs
├── topics.py           # маппинг TG-топик ↔ MAX-чат
│
├── api/                # типизированные обёртки над BotApi
│   ├── auth.py
│   ├── chats.py
│   ├── core.py
│   ├── events.py
│   ├── send.py
│   ├── sessions.py
│   └── topics.py
│
└── handlers/           # aiogram-хэндлеры
    ├── _common.py      # общие хелперы (_is_allowed, _escape, _reject)
    ├── basic.py        # /start, /help, /status, /chats
    ├── registration.py # первичная регистрация владельца
    ├── reply.py        # /reply, inline-кнопка «Ответить»
    ├── sessions.py     # выбор/загрузка MAX-сессии
    ├── auth_watcher.py # фон: опрос /status, шлёт 2fa-реквесты
    ├── prune_topics.py # /prune_topics
    ├── auth/           # auth-flow (SMS, 2FA, reauth)
    │   ├── auth_action.py    # inline «sms / session / cancel»
    │   ├── event_callbacks.py # inline под событиями
    │   ├── reauth.py         # /reauth_sms
    │   └── topic_echo.py     # авто-форвард в топики
    ├── chat_ops/       # admin-операции над MAX
    │   ├── _common.py
    │   ├── help.py     # /chatops
    │   ├── invite.py   # /invite, /search_user
    │   ├── join.py     # /join, /resolve
    │   └── join_requests.py # /pending, /approve, /decline
    └── supergroup/     # привязка TG-супергруппы
        ├── attach.py   # прикрепление TG-чата
        └── commands.py # /setgroup, /supergroup
```

## `max/app/` — код max-процесса

```
max/app/
├── auth.py             # QueueSmsCodeProvider, QueuePasswordProvider
├── chat_ops.py         # chat_ops_loop (admin-операции)
├── config.py           # локальные настройки
├── sender.py           # sender_loop (POST /send → pymax)
│
├── bridge/             # обработчики событий PyMax
│   ├── __init__.py     # on_message / on_chat_update диспетчер
│   ├── chats.py        # нормализация Chat → DB
│   ├── media.py        # скачивание вложений
│   ├── on_start.py     # хук на успешный connect
│   └── users.py        # отображение имён пользователей
│
└── supervisor/         # управление жизненным циклом клиента
    ├── __init__.py     # главный supervisor-loop
    ├── _backoff.py     # экспоненциальный backoff при reconnect
    ├── cache.py        # wipe-cache + пути к main.db
    ├── client_runtime.py # build_client(phone, cache_dir)
    ├── read_receipts.py # read_receipts_loop
    └── twofa_drain.py  # дренаж очереди 2FA-кодов
```

## Дальше

- Что и куда течёт по таблицам — [`queues.md`](./queues.md).
- Какие пакеты зачем — [`dependencies.md`](./dependencies.md).