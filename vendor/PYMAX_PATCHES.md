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

## Patch 4: `App.on_event` — не валить long-poll при ошибке парсинга

**Симптом:** в логах MAX-процесса появляется

```
ERROR pymax.app: Failed to dispatch inbound frame: ...
pydantic_core._pydantic_core.ValidationError: ...
```

после чего long-poll реконнектится, и из-за одного битого фрейма
(например, новая схема payload или ``User.gender: int`` вместо ``str``)
**все** последующие события теряются до переподключения. Если сервер
шлёт тот же payload — цикл повторяется бесконечно.

**Причина:** в `pymax/app.py::App.on_event` диспатчер сделан «хрупким»:
любое исключение (включая ``ValidationError`` из Pydantic-моделей событий)
заворачивается в ``RuntimeError`` и **raise** обратно в цикл long-poll.
Long-poll в свою очередь тоже падает → реконнект → повтор.

**Workaround:** `_patch_app_on_event_safe()` подменяет `App.on_event` на
безопасную обёртку, которая при любом исключении логирует его в
``logger.warning`` (видно в Railway-логах), но **не** raise-ит. Long-poll
продолжает работать, битый фрейм просто пропускается, а следующие фреймы
обрабатываются в штатном режиме.

Идемпотентен — проверяет флаг ``_pymax_patched_safe`` на методе.

**Когда удалять:** при обновлении PyMax до версии, где `App.on_event`
делает ``try/except`` вокруг диспатчера сам (или логирует ошибку, не
поднимая её в long-poll). Тогда всю функцию ``_patch_app_on_event_safe``
можно удалить.

## Patch 5: `get_reactions` — `messageIds` как `list[int]`

**Симптом:** при попытке узнать, какие реакции поставил **сам** владелец
моста на сообщение оппонента, мост логирует:

```
ERROR pymax.app: api error opcode=180 seq=N error=proto.payload
  title=Ошибка валидации message=Expected number at 26
```

После чего ``client.get_reactions`` возвращает ``None`` →
``your_reaction`` определяется как ``None`` → зеркальная реакция
MAX → TG не ставится (хотя в ``bridge.on_reaction_update: event received``
счётчики от MAX приходят — то есть сервер событие прислал, но мы не
можем из него вытащить «свою» реакцию).

**Причина:** в `pymax/api/messages/payloads.py::GetReactionsPayload`
объявлено

```python
class GetReactionsPayload(CamelModel):
    chat_id: int
    message_ids: list[str]   # ← PyMax 2.2.0 BUG
```

PyMax шлёт ``messageIds`` как ``list[str]`` (строки), MAX-сервер по
протоколу ``proto.payload`` ожидает ``list[int64]`` → ошибка
``Expected number at 26``. Точно такой же баг, как и в Patch 1, только
для ``get_reactions``.

**Workaround:** `_patch_get_reactions_message_ids_int()` подменяет
``MessageService.get_reactions`` так, чтобы формировать payload как dict
с ``messageIds = [int(mid), ...]`` (минуя pydantic-валидацию
``GetReactionsPayload.message_ids: list[str]``) и слать через
``app.invoke(Opcode.MSG_GET_REACTIONS, payload)``. Разбор ответа
(``messagesReactions`` → ``dict[message_id, ReactionInfo]``) повторяет
логику оригинального метода.

Идемпотентен — проверяет флаг ``_pymax_patched_msgids`` на методе.

**Когда удалять:** при обновлении PyMax до версии, где
``GetReactionsPayload.message_ids: list[int]``. Тогда функцию
``_patch_get_reactions_message_ids_int`` можно удалить.

---

Другие баги добавляйте здесь по мере обнаружения.

## Как добавить новый патч

1. Добавьте функцию `_patch_<name>()` в `max/app/pymax_patches.py`.
2. Вызовите её из `apply()`.
3. Задокументируйте здесь: симптом, причина, workaround, способ
   удаления после фикса upstream.