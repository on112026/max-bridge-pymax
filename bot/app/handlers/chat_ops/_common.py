"""Общие хелперы для пакета ``chat_ops``.

Содержит функции, которые переиспользуются несколькими подмодулями:

* :func:`parse_user_ids` — парсинг строки ``"1 2 3"`` / ``"1,2,3"`` → ``list[int]``.
* :func:`format_chat_result` — форматирование ``result`` от ``join``/``resolve``.
* :func:`format_user_result` — форматирование ``result`` от ``search_user``.
* :func:`format_join_requests` — форматирование списка заявок.
* :func:`_escape` / :func:`_is_allowed` / :func:`_reject` — re-export из
  :mod:`app.handlers._common`, чтобы подмодули импортировали всё из одного
  места.

Никакой бизнес-логики и FSM здесь нет — только чистые утилиты, которые
можно тестировать отдельно.
"""

from __future__ import annotations

from typing import Any, List, Optional, Tuple

# Re-export для удобства: подмодули импортируют ``from app.handlers.chat_ops._common import _is_allowed``.
from app.handlers._common import _escape, _is_allowed, _reject  # noqa: F401


def parse_user_ids(arg: str) -> Optional[List[int]]:
    """Распарсить строку ``arg`` в ``List[int]``.

    Допускаются разделители — пробел и запятая. Возвращает ``None``, если
    хотя бы один токен не парсится в ``int`` или строка пуста.
    """
    arg = (arg or "").strip()
    if not arg:
        return None
    parts = [p.strip() for p in arg.replace(",", " ").split() if p.strip()]
    if not parts:
        return None
    out: List[int] = []
    for p in parts:
        try:
            out.append(int(p))
        except ValueError:
            return None
    return out


def _first_str(d: dict, *keys: str, default: str = "—") -> str:
    """Первый непустой строковый атрибут из ``keys``. Для форматирования."""
    for k in keys:
        v = d.get(k)
        if v:
            return str(v)
    return default


def _first_int(d: dict, *keys: str) -> Optional[int]:
    """Первый числовой атрибут из ``keys`` или ``None``."""
    for k in keys:
        v = d.get(k)
        if v is None or v == "":
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return None


def format_chat_result(result: Any) -> str:
    """Превратить ``result`` от ``join``/``resolve`` в человекочитаемый текст.

    ``result`` — JSON-сериализованный ``pymax.Chat`` (или похожий объект).
    Поддерживаем несколько вариантов имён полей, потому что формат
    ``Chat`` между версиями pymax немного различается.
    """
    if not result:
        return "(пустой ответ от MAX)"
    if not isinstance(result, dict):
        return f"<code>{_escape(str(result))}</code>"
    name = _first_str(result, "title", "name")
    cid = _first_str(result, "id", "chat_id", "max_chat_id")
    ctype = _first_str(result, "type", "chat_type")
    members = result.get("members_count") or result.get("participants_count")
    extra = f"\n👥 Участников: {members}" if members is not None else ""
    return (
        f"Название: <b>{_escape(name)}</b>\n"
        f"ID: <code>{_escape(cid)}</code>\n"
        f"Тип: {_escape(ctype)}{extra}"
    )


def format_user_result(user: Any) -> str:
    """Превратить ``result`` от ``search_user`` в человекочитаемый текст."""
    if not user:
        return "Пользователь не найден."
    if not isinstance(user, dict):
        return f"<code>{_escape(str(user))}</code>"
    name = " ".join(
        p for p in (
            _first_str(user, "first_name", default=""),
            _first_str(user, "last_name", default=""),
            _first_str(user, "nickname", default=""),
            _first_str(user, "name", default=""),
        ) if p
    ).strip() or "—"
    uid = _first_str(user, "id", "user_id")
    phone = _first_str(user, "phone", default="—")
    return (
        f"Имя: <b>{_escape(name)}</b>\n"
        f"ID: <code>{_escape(uid)}</code>\n"
        f"Телефон: <code>{_escape(phone)}</code>"
    )


def format_join_requests(items: Any) -> Tuple[str, List[int]]:
    """Превратить список заявок в текст. Возвращает ``(text, user_ids)``.

    ``user_ids`` — список int-идентификаторов из заявок (для последующего
    ``/approve <chat_id> 1 2 3``). Если id не парсится — он просто
    пропускается в списке.
    """
    if not items:
        return "Заявок нет.", []
    if not isinstance(items, list):
        return f"<code>{_escape(str(items))}</code>", []
    lines: List[str] = []
    ids: List[int] = []
    for idx, it in enumerate(items, start=1):
        if not isinstance(it, dict):
            lines.append(f"{idx}. <code>{_escape(str(it))}</code>")
            continue
        uid_int = _first_int(it, "user_id", "id")
        uid_disp = uid_int if uid_int is not None else _first_str(it, "user_id", "id")
        if uid_int is not None:
            ids.append(uid_int)
        name = " ".join(
            p for p in (
                _first_str(it, "first_name", default=""),
                _first_str(it, "last_name", default=""),
                _first_str(it, "nickname", default=""),
                _first_str(it, "name", default=""),
            ) if p
        ).strip() or "—"
        lines.append(
            f"{idx}. <b>{_escape(name)}</b> — <code>{_escape(str(uid_disp))}</code>"
        )
    return "\n".join(lines), ids