# Troubleshooting

## «MAX запрашивает код» прилетает при каждом рестарте

Скорее всего, том `/app/cache` не сохраняется между запусками.

- **Локально**: проверьте, что в `docker-compose.yaml` остался том
  `bridge_cache`.
- **Railway**: добавьте том `/app/cache` в **Settings → Volumes**.

## Бот молчит после `/start`

Проверьте, что ваш Telegram ID есть в `ALLOWED_TG_USER_IDS`. Бот
намеренно fail-closed: если список пуст — он не отвечает никому
(в логах будет warning про пустой `ALLOWED_TG_USER_IDS`).

## `auth.status=unknown`

Max-процесс не смог стартовать. Смотрите `make restart-max` →
`make logs`. Типичные причины:

- неверный `MAX_PHONE` (нужен `+…` с плюсом),
- провайдер MAX выдаёт captcha (PyMax не умеет) — попробуйте позже.

## Бот не прислал «MAX запрашивает код», хотя MAX точно прислал SMS

Проверьте `make status`. Должно быть `"auth": {"status": "need_2fa", ...}`.
Если статус `unknown` — значит supervisor ещё не успел обновить state.
Подождите до 10 секунд.

## Топики не создаются автоматически

Сначала `/setgroup` (привязать супергруппу), затем подождите —
`TopicSyncWorker` обходит `topic_jobs` каждые несколько секунд и
создаёт топики по актуальным MAX-чатам. Если не помогло —
`/prune_topics`.

## `chat_ops_queue` растёт, операции висят в `pending`

Скорее всего, MAX-клиент не готов (`auth.status != ok`) или вообще не
поднят (`is_ready()=False`). Проверьте `make status` и `make logs-max`.

Для отладки:

```bash
curl -H "X-Api-Key: $BRIDGE_API_KEY" \
     "http://localhost:${API_PORT:-8000}/chat_ops/stats"
```

## Медиа не доходят в Telegram

- В MAX видео больше 49 МБ? Telegram Bot API не примет.
- Бот поддерживает только **одно** вложение за раз. Если в MAX
  прислали 3 фото — в Telegram уйдёт первое, остальные отбросятся.

## Полный сброс

```bash
make nuke && make up
```

## Дальше

- Архитектура — [`architecture.md`](./architecture.md).
- Очереди — [`queues.md`](./queues.md).