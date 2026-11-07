"""Нечёткий поиск группы или преподавателя по тому, что ввёл пользователь.

Названия групп вида «ИЯ-АН_Оb251» неудобно набирать: в них смешаны кириллица
и латиница, дефисы и подчёркивания. Поэтому сравниваем нормализованные строки:
без регистра и разделителей, латинские буквы заменены на русские аналоги.
Запрос «ан 251» найдёт «ИЯ-АН_Оb251», «анисимова» — «Анисимова О.Н.».

Иностранным студентам трудно набрать кириллицу, поэтому, если так ничего
не нашлось, запрос латиницей сравнивается с транслитерацией названий:
«Anisimova» найдёт «Анисимова О.Н.», «AN 251» — «ИЯ-АН_Оb251».
"""

from __future__ import annotations

import re

_LATIN_TO_CYRILLIC = str.maketrans({
    "a": "а", "b": "б", "c": "с", "e": "е", "g": "г", "k": "к", "m": "м",
    "o": "о", "p": "п", "t": "т", "x": "х", "y": "у", "ё": "е",
})

# Транслитерация как в загранпаспорте РФ, с привычными вариантами (й → y, ь — без буквы).
# «х» — «h»: запрос с «kh» приводится к нему же, так находятся оба написания.
_CYRILLIC_TO_LATIN = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
})


def normalize(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.lower().translate(_LATIN_TO_CYRILLIC))


def transliterate(text: str) -> str:
    """Кириллица → латиница без регистра и разделителей: «ИЯ-АН_Оb251» → «iyaanob251»."""
    return re.sub(r"[\W_]+", "", text.lower().translate(_CYRILLIC_TO_LATIN))


def _translit_parts(name: str) -> list[tuple[str, set[int]]]:
    """Части названия (по дефисам, пробелам, точкам) в латинице и позиции,
    где в латинице начинается очередная русская буква."""
    parts = []
    for part in re.split(r"[\W_]+", name.lower()):
        latin, bounds = "", {0}
        for ch in part:
            latin += ch.translate(_CYRILLIC_TO_LATIN)
            bounds.add(len(latin))
        if latin:
            parts.append((latin, bounds))
    return parts


def _aligned_in(token: str, parts: list[tuple[str, set[int]]]) -> bool:
    """Есть ли token внутри одной части и ровно по границам русских букв.
    Иначе «an» нашлось бы в «РЯЯНР» → «ryayanr» на стыке «я» и «н»."""
    for latin, bounds in parts:
        start = latin.find(token)
        while start != -1:
            if start in bounds and start + len(token) in bounds:
                return True
            start = latin.find(token, start + 1)
    return False


def _match_latin(query: str, names: list[str]) -> list[str]:
    # «х» пишут и «kh», и «h» (Khrenova / Hrenova) — в названиях это «h», приводим к нему
    query = query.lower().replace("kh", "h")
    target = transliterate(query)
    if not target:
        return []
    exact = [n for n in names if transliterate(n) == target]
    if exact:
        return exact
    tokens = [tok for tok in (transliterate(t) for t in re.split(r"[\W_]+", query)) if tok]
    return [n for n in names if all(_aligned_in(tok, _translit_parts(n)) for tok in tokens)]


def find(query: str, names: list[str]) -> list[str]:
    """Точное совпадение — сразу единственный результат. Иначе — все названия,
    в которых встречается каждое слово запроса."""
    target = normalize(query)
    if not target:
        return []
    found = [n for n in names if normalize(n) == target]
    if not found:
        tokens = [k for k in (normalize(t) for t in query.split()) if k]
        found = [n for n in names if all(t in normalize(n) for t in tokens)]
    if not found and re.search(r"[a-zA-Z]", query):
        found = _match_latin(query, names)
    return found
