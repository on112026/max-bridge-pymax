"""Хелперы для отображения имён пользователей из ``pymax.User``.

У ``pymax.User`` нет поля ``name`` — есть ``names: list[Name]``,
где каждый ``Name`` содержит ``name`` (полное), ``first_name``,
``last_name`` и ``type``. Здесь собираем «человеческое» имя из
этих кусков с приоритетом полного → fallback first+last.
"""

from __future__ import annotations

from typing import Any, Optional


def user_display_name(user: Any) -> Optional[str]:
    """Собирает отображаемое имя из ``User.names`` (pymax).

    Приоритет:
      1) Заполненное ``name`` (полное имя).
      2) ``first_name + last_name`` из любой записи.
      3) ``None``, если ничего подходящего нет.
    """
    if user is None:
        return None
    names_list = getattr(user, "names", None) or []
    # 1) Приоритет — заполненное полное имя.
    for n in names_list:
        full = (getattr(n, "name", None) or "").strip()
        if full:
            return full
    # 2) Fallback: склеить first_name + last_name из любой записи.
    for n in names_list:
        first = (getattr(n, "first_name", None) or "").strip()
        last = (getattr(n, "last_name", None) or "").strip()
        joined = (first + " " + last).strip()
        if joined:
            return joined
    return None