# Очереди в общей SQLite-БД

Все три процесса общаются через одну SQLite-БД
(`/data/bridge.db`), **без прямых сетевых вызовов между собой**.
Это позволяет каждому процессу быть перезапущенным независимо.

## Таблицы и направления

| Таблица | Направление | Назначение |
| --- | --- | --- |
| `events` | max → bot | входящие события MAX (сообщения, изменения чатов) |
| `send_queue` | bot → max | исходящие сообщения (текст + одно вложение) |
| `chat_ops_queue` | bot → max | admin-операции (`/join`, `/invite`, `/pending`, …) |
| `reaction_ops_queue` | двусторонняя | операции над реакциями: `to_max` (add/remove/fetch_summary), `to_tg` (`setMessageReaction`), `to_tg_summary` (обновить сводку) |
| `chats` | max → bot | кэш MAX-чатов |
| `topics` | bot ↔ api | маппинг MAX-чат ↔ TG-топик |
| `supergroups` | bot → api | привязка TG-супергруппы к владельцу |
| `topic_jobs` | bot → api | фоновые задачи синка топиков (создание/переименование) |
| `auth_state` | max → bot | состояние MAX-клиента (`need_2fa` / `ok`) |
| `read_receipts` | bot → max | доставленные/прочитанные сообщения |
| `delivered_messages.tg_*` | bot → api | обратная TG-ссылка для двусторонней синхронизации реакций (`tg_chat_id` / `tg_thread_id` / `tg_message_id` / `tg_summary_message_id`) |

## Цикл-воркеры в `max/`

Каждый воркер в `max/app/` повторяет один и тот же паттерн:

1. `GET /<queue>/next` — атомарно забрать следующую `pending`-задачу.
2. Выполнить её через `pymax.Client`.
3. `POST /<queue>/{id}/finish` с `ok=true/false` и (опционально)
   `result`.

Это реализовано в:

| Воркер | Файл | Очередь |
| --- | --- | --- |
| `sender_loop` | `max/app/sender.py` | `send_queue` |
| `chat_ops_loop` | `max/app/chat_ops.py` | `chat_ops_queue` |
| `read_receipts_loop` | `max/app/supervisor/read_receipts.py` | `read_receipts` |

Все три живут в supervisor-loop'е и запускаются как `asyncio.Task`.

## Цикл-воркеры в `bot/`

| Воркер | Файл | Что делает |
| --- | --- | --- |
| `EventPoller` | `bot/app/forwarder.py` | `GET /events?wait=true` → Telegram |
| `AuthWatcher` | `bot/app/handlers/auth_watcher.py` | опрашивает `/status` для 2FA-реквестов |
| `TopicSyncWorker` | `bot/app/topic_worker.py` | тянет `/topic_jobs` и создаёт/переименовывает топики |

## Синхронные chat-ops

Для `/search_user`, `/pending` и других операций, у которых есть
конкретный результат, в `api/routers/chat_ops.py` есть
`GET /chat_ops/{id}?wait=true` — крутится в polling до `done`/`failed`
или до таймаута. Это позволяет боту получить результат одним
запросом, без отдельного polling-цикла на стороне бота.

## Супергруппа и топики

`/setgroup` привязывает TG-супергруппу к владельцу. Дальше:

- `TopicSyncWorker` (в боте) периодически тянет `GET /topic_jobs/next`
  и создаёт/переименовывает топики по актуальному списку MAX-чатов.
- Входящее из MAX-чата X → `EventPoller` шлёт сообщение в
  `message_thread_id` топика X (или в личку, если супергруппа не
  настроена).
- Исходящее из TG-топика X → `message_thread_id` маппится на
  `max_chat_id` → кладётся в `send_queue` → `sender_loop` шлёт в MAX.

## Наблюдение

Полезные эндпойнты для отладки:

- `GET /status` — `auth.status`, `last_error`, `client_running`.
- `GET /chat_ops/stats` — счётчики `pending` / `in_progress` / `done`
  / `failed` для chat_ops_queue.
- `GET /health` — liveness probe (для Railway / k8s).

## Дальше

- Команды бота — [`features.md`](./features.md).
- Структура файлов — [`structure.md`](./structure.md).