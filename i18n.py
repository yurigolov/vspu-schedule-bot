"""Переводы интерфейса. Тексты лежат в locales/<язык>.toml — по файлу на язык.

Ключи — путь в файле через точку: t("en", "group.not_found").
Подстановки в тексте — {name}, заполняются аргументами t(..., name="...").
Если ключа нет в выбранном языке, берётся русский текст.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

LOCALES_DIR = Path(__file__).parent / "locales"
DEFAULT_LANG = "ru"
LANGS = ("ru", "en", "pt")  # порядок — как в кнопках выбора языка


def _flatten(table: dict, prefix: str = "") -> dict[str, object]:
    flat: dict[str, object] = {}
    for key, value in table.items():
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{prefix}{key}."))
        else:
            flat[f"{prefix}{key}"] = value
    return flat


def _load(lang: str) -> dict[str, object]:
    with open(LOCALES_DIR / f"{lang}.toml", "rb") as f:
        return _flatten(tomllib.load(f))


STRINGS: dict[str, dict[str, object]] = {lang: _load(lang) for lang in LANGS}


def _raw(lang: str, key: str) -> object:
    strings = STRINGS.get(lang, STRINGS[DEFAULT_LANG])
    if key in strings:
        return strings[key]
    return STRINGS[DEFAULT_LANG][key]  # KeyError — опечатка в ключе, пусть будет видна сразу


def t(lang: str, key: str, **kwargs: object) -> str:
    text = _raw(lang, key)
    assert isinstance(text, str), key
    return text.format(**kwargs) if kwargs else text


def tl(lang: str, key: str) -> list[str]:
    """Список из файла перевода (дни недели, месяцы)."""
    value = _raw(lang, key)
    assert isinstance(value, list), key
    return value


def has(lang: str, key: str) -> bool:
    return key in STRINGS.get(lang, {}) or key in STRINGS[DEFAULT_LANG]


def variants(key: str) -> set[str]:
    """Текст ключа на всех языках — чтобы узнавать нажатие кнопки на любом из них."""
    return {t(lang, key) for lang in LANGS}


def guess_lang(language_code: str | None) -> str:
    """Язык по настройкам Telegram — пока пользователь не выбрал сам."""
    code = (language_code or "").lower().split("-")[0]
    return code if code in LANGS else DEFAULT_LANG
