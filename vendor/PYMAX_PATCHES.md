# PyMax 2.2.0 — известные баги и патчи

Этот каталог — **vendor** для [PyMax](https://github.com/MaxApiTeam/PyMax) 2.2.0.
Мы не правим файлы PyMax напрямую — иначе патчи молча слетят при
любом `git pull` или обновлении субмодуля.

Вместо этого обходные пути лежат в нашем коде и активируются через
monkey-patches при старте MAX-процесса.

## Patch 1: `add_reaction` / `remove_reaction` — `messageId` как int

**Симптом:** каждый вызов `client.add_reaction(...)` возвращает:

```
ERROR pymax.app: api error opcode=178 seq=8 error=proto.payload
  title=Ошибка валидации message=Expected number at 24
```

После чего MAX-сервер принудительно разрывает long-poll соединение,
PyMax реконнектится — цикл бесконечный.

**Причина:** в `pymax/api/messages/payloads.py`:

```python
class AddReactionPayload(CamelModel):
    chat_id: int
    message_id: str          # ← объявлено как str (PyMax 2.2.0 BUG)
    reaction: ReactionInfoPayload

class RemoveReactionPayload(CamelModel):
    chat_id: int
    message_id: str          # ← то же
```

PyMax шлёт в JSON `{"messageId": "116837694944467385", ...}` — строку.
MAX-сервер по протоколу `proto.payload` ожидает `int64` → ошибка
`Expected number at 24`.

**Workaround:** в `max/app/pymax_patches.py::_patch_reaction_message_id_int`
мы подменяем `MessageService.add_reaction` и `remove_reaction` так,
чтобы они формировали payload как обычный `dict` (минуя pydantic-валидацию
`AddReactionPayload.message_id: str`) и шли его через
`app.invoke(Opcode.MSG_REACTION, payload)`. При этом `messageId`
передаётся как `int(message_id)` — ровно то, что ждёт MAX.

Патч применяется автоматически при `register_bridge(client)`,
**до** `client.start()`. Идемпотентен (`_APPLIED` флаг).

**Когда удалять:** при обновлении PyMax до версии, где
`AddReactionPayload.message_id: int` (т.е. upstream починил баг),
просто удалите `max/app/pymax_patches.py` и уберёте вызов
`apply_pymax_patches()` из `bridge/__init__.py`. Наш `reactions_loop`
продолжит работать — патч нужен только для текущего бага.

## Patch 2: `EVENT_MAP[NOTIF_MSG_YOU_REACTED]` → `REACTION_UPDATE`

**Симптом:** при постановке реакции **владельцем моста** в MAX на любое
сообщение бот не ставит зеркальную реакцию в Telegram. В логах
`bridge.on_reaction_update: event received …` не появляется.

**Причина:** в `pymax/dispatch/mapping.py::EVENT_MAP` вендор зарегистрировал
только один opcode для событий реакций:

```python
EVENT_MAP: dict[Opcode, Resolver] = {
    ...
    Opcode.NOTIF_MSG_REACTIONS_CHANGED: resolve_reaction_update,  # 155
}
```

MAX-сервер шлёт **два** разных opcode:

| opcode | имя | когда |
|---|---|---|
| 155 | `NOTIF_MSG_REACTIONS_CHANGED` | кто-то другой поставил реакцию (counters) |
| 156 | `NOTIF_MSG_YOU_REACTED` | **вы сами** поставили реакцию |

Без opcode 156 `EventResolver.resolve()` возвращает `None`, фрейм попадает
только в `on_raw()`, наш `on_reaction_update()` не вызывается.

**Workaround:** `_patch_event_map_you_reacted()` добавляет
`Opcode.NOTIF_MSG_YOU_REACTED → resolve_reaction_update` в `EVENT_MAP`.

**Когда удалять:** при обновлении PyMax до версии, где `EVENT_MAP` уже
содержит `Opcode.NOTIF_MSG_YOU_REACTED` (upstream добавил). Функция
идемпотентна — проверит наличие и пропустит.

## Patch 3: `ReactionUpdateEvent.model_validate` coerce `messageId` int→str

**Симптом:** при любом входящем событии реакции (opcode=155 или 156)
dispatcher падает в `RuntimeError("Failed to dispatch inbound frame")`:

```
pydantic_core._pydantic_core.ValidationError: 1 validation error for ReactionUpdateEvent
messageId
  Input should be a valid string [type=string_type, input_value=116838091054923435, input_type=int]
```

`opcode=155 cmd=0 seq=19` — фрейм полностью теряется, мост не зеркалит
реакции MAX → TG даже после фикса Patch 2.

**Причина:** в `pymax/types/events/reaction.py::ReactionUpdateEvent`
поле объявлено как `message_id: str`, но MAX-сервер шлёт в JSON
`"messageId": <int64>`. Pydantic 2.x в строгом режиме **не** coerce-ит
int → str → ValidationError → фрейм падает в `RuntimeError`.

**Workaround:** `_patch_reaction_event_message_id_coerce()` подменяет
`ReactionUpdateEvent.model_validate` так, чтобы перед валидацией
привести `messageId` из int в str (только если он int). Другие поля
(`chat_id`, `counters`, `total_count`) Pydantic coerce-ит корректно —
их не трогаем.

**Когда удалять:** при обновлении PyMax до версии, где
`ReactionUpdateEvent.message_id: Union[int, str]` (или просто `int`).
Функция идемпотентна — проверит наличие маркера и пропустит.

---

Другие баги добавляйте здесь по мере обнаружения.

## Как добавить новый патч

1. Добавьте функцию `_patch_<name>()` в `max/app/pymax_patches.py`.
2. Вызовите её из `apply()`.
3. Задокументируйте здесь: симптом, причина, workaround, способ
   удаления после фикса upstream.