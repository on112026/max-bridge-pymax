# Архитектура

Документ описывает, как устроен `max-bridge-pymax` сверху: из каких
процессов состоит, что у них общего, как они общаются.

## Общая схема

```
                ┌──────────────────────────────────────┐
                │       SQLite (общая БД моста)        │
                │  events / send_queue / chats /       │
                │  auth_state / chat_ops_queue /       │
                │  topics / supergroups / topic_jobs   │
                └──────────────────────────────────────┘
                          ▲        ▲        ▲
                          │        │        │
        ┌─────────────────┐│   ┌────┴───┐ ┌──┴───────────────┐
        │   api (FastAPI) ││   │  bot   │ │  max (PyMax 2.2) │
        │   uvicorn:8000  ││   │ aiogram│ │  client.start()  │
        │                 ││   │        │ │  sender_loop     │
        │  POST /events   ││   │  polls │ │  chat_ops_loop   │
        │  GET  /send/next││   │ /events│ │  read_receipts   │
        │  POST /chat_ops ││   │        │ │  twofa_drain     │
        └─────────────────┘│   └────────┘ └──────────────────┘
                ▲         │
                │ HTTP    │
                └─────────┘
```

Внутри одного Docker-контейнера все три процесса ходят в API по
`http://127.0.0.1:${API_PORT}` (по умолчанию 8000).

## Роли процессов

### `api` — FastAPI + uvicorn

Единственный «общий» компонент: хранит события, очереди, чаты и
состояние авторизации. Ходит в него и бот, и max-процесс.

Не знает про PyMax и aiogram — только про SQLAlchemy + pydantic.

### `bot` — aiogram 3

Команды владельца (`/start`, `/reply`, `/chatops`, `/setgroup`, …),
опрос событий из API (`GET /events?wait=true`), отправка ответов
(`POST /send`), рассылка входящих в Telegram. Запускает фоновые
воркеры: `EventPoller`, `AuthWatcher`, `TopicSyncWorker`.

Не знает про PyMax напрямую — взаимодействует с MAX только через API.

### `max` — PyMax-клиент + циклы

Long-running процесс с PyMax-клиентом. Слушает входящие
(`on_message` / `on_chat_update`) → кладёт события в API
(`POST /events`). Параллельно крутит воркеры:

- `sender_loop` — `GET /send/next` → `pymax.send_message` →
  `POST /send/{id}/finish`;
- `chat_ops_loop` — `GET /chat_ops/next` → выполняет admin-операцию
  (`join`, `invite`, `pending`, …) → `POST /chat_ops/{id}/finish`;
- `read_receipts_loop` — синхронизация прочитанных сообщений;
- `twofa_drain` — дренаж in-memory очереди SMS/2FA-кодов.

## Зачем одна общая SQLite-БД

Все три процесса общаются через одну SQLite-БД
(`/data/bridge.db`), **без прямых сетевых вызовов между собой**.

Это даёт два важных свойства:

1. **Изоляция отказов.** Любой процесс можно перезапустить независимо
   (`supervisorctl restart api` / `bot` / `max`) — состояние не теряется.
2. **Единая точка наблюдения.** `GET /status`, `GET /chat_ops/stats`
   видят всё: и сколько событий в очереди, и сколько admin-операций
   повисло в `pending`, и `auth.status`.

Подробнее про таблицы и направления — в [`queues.md`](./queues.md).

## Модель владения

Мост рассчитан на **одного владельца**:

- один Telegram-аккаунт (`ALLOWED_TG_USER_IDS` — список, но
  фактически используется первый);
- один MAX-аккаунт (`MAX_PHONE`).

Расширение на нескольких владельцев потребует переписать ключи
`owner_user_id` в `shared/db/_models.py` и снять fail-closed в боте.

## Дальше

- Команды бота и ограничения протокола — [`features.md`](./features.md).
- Дерево каталогов — [`structure.md`](./structure.md).
- Потоки данных через таблицы — [`queues.md`](./queues.md).