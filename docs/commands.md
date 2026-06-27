# Команды `make` и API-эндпойнты

## `make help`

```bash
make help            # полный список команд
```

## Сборка и запуск

| Команда | Что делает |
| --- | --- |
| `make build` | собрать образ |
| `make up` | поднять контейнер в фоне |
| `make down` | остановить контейнер |
| `make restart` | перезапустить контейнер |
| `make ps` | статус контейнера |

## Логи и отладка

| Команда | Что делает |
| --- | --- |
| `make logs` | трейс логов всего контейнера |
| `make shell` | войти в bash контейнера |
| `make supervisorctl` | supervisorctl внутри контейнера |
| `make restart-api` | перезапустить api внутри supervisord |
| `make restart-bot` | перезапустить bot внутри supervisord |
| `make restart-max` | перезапустить max внутри supervisord |

## Состояние MAX

| Команда | Что делает |
| --- | --- |
| `make status` | `GET /status` API (auth, last_error) |
| `make chats` | `GET /chats?limit=20` |
| `make events` | `GET /events?limit=20` |

## Сброс / обслуживание

| Команда | Что делает |
| --- | --- |
| `make wipe-cache` | удалить кэш PyMax (== «reauth с нуля»). НЕ стирает `/data`. |
| `make wipe-data` | ⚠️ полный сброс БД и медиа (с подтверждением) |
| `make nuke` | `down` + `wipe-data` |

## Утилиты

| Команда | Что делает |
| --- | --- |
| `make env` | показать текущий `.env` (без секретов) |
| `make gen-key` | сгенерировать `BRIDGE_API_KEY` (`openssl rand -hex 32`) |

## HTTP API (для отладки вручную)

Все эндпойнты (кроме `/health`) требуют заголовок
`X-Api-Key: $BRIDGE_API_KEY`.

### Статус и здоровье

| Метод | Путь | Что делает |
| --- | --- | --- |
| GET | `/health` | liveness probe |
| GET | `/status` | `auth.status`, `last_error`, `client_running` |

### События (max → bot)

| Метод | Путь | Что делает |
| --- | --- | --- |
| GET | `/events?limit=N&wait=true` | последние события (polling) |
| POST | `/events` | max-процесс кладёт входящие события |

### Отправка (bot → max)

| Метод | Путь | Что делает |
| --- | --- | --- |
| POST | `/send` | бот кладёт сообщение в очередь |
| GET | `/send/next` | max-процесс забирает следующее |
| POST | `/send/{id}/finish` | max-процесс сообщает о результате |

### Чаты

| Метод | Путь | Что делает |
| --- | --- | --- |
| GET | `/chats?limit=N` | список MAX-чатов из БД |
| GET | `/chats/{id}` | один чат по id |
| POST | `/chats/{id}/read-up-to` | пометить прочитанным |

### Chat-ops (admin)

| Метод | Путь | Что делает |
| --- | --- | --- |
| POST | `/chat_ops/join` | вступить в группу/канал |
| POST | `/chat_ops/resolve` | превью чата |
| POST | `/chat_ops/invite` | пригласить пользователей |
| POST | `/chat_ops/list_join_requests` | заявки на вступление |
| POST | `/chat_ops/confirm_join_request` | принять заявки |
| POST | `/chat_ops/decline_join_request` | отклонить заявки |
| POST | `/chat_ops/search_user` | поиск по телефону |
| GET | `/chat_ops/next` | max-процесс забирает задачу |
| POST | `/chat_ops/{id}/finish` | max-процесс сообщает о результате |
| GET | `/chat_ops/{id}?wait=true` | polling результата |
| GET | `/chat_ops/stats` | счётчики очереди |

### Авторизация

| Метод | Путь | Что делает |
| --- | --- | --- |
| POST | `/auth/2fa/request` | max-процесс: зарегистрировать SMS-запрос |
| PUT | `/auth/2fa/{rid}` | бот: отдать SMS-код |

### Сессии

| Метод | Путь | Что делает |
| --- | --- | --- |
| POST | `/sessions/upload` | загрузить session-файл (multipart) |
| GET | `/sessions/list` | список session-файлов в `CACHE_DIR` |

### Топики и супергруппы

| Метод | Путь | Что делает |
| --- | --- | --- |
| GET | `/topics` | маппинг MAX-чат ↔ TG-топик |
| POST | `/supergroups` | привязать супергруппу |
| GET | `/supergroups/{owner_uid}` | текущая привязка |
| GET | `/topic_jobs/next` | бот забирает задачу синхронизации |
| POST | `/topic_jobs/{id}/finish` | бот сообщает о результате |

### Read receipts

| Метод | Путь | Что делает |
| --- | --- | --- |
| GET | `/read_receipts/pending` | список доставленных/прочитанных |
| POST | `/read_receipts/{id}/finish` | max-процесс сообщает о результате |

## Дальше

- Деплой на Railway — [`deployment.md`](./deployment.md).
- Troubleshooting — [`troubleshooting.md`](./troubleshooting.md).