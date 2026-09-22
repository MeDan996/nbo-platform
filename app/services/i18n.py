"""Translation catalogue.

Strings live in ``app/locales/<locale>.json``. Lookup falls back locale ->
default locale -> English -> the key itself, so a missing translation degrades to
something readable rather than blowing up a page mid-exam.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from app.config import settings

LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"

LOCALE_NAMES = {"ru": "Русский", "ky": "Кыргызча", "en": "English"}
LOCALE_FLAGS = {"ru": "RU", "ky": "KG", "en": "EN"}


@lru_cache(maxsize=8)
def catalogue(locale: str) -> dict[str, str]:
    path = LOCALES_DIR / f"{locale}.json"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def normalise(locale: str | None) -> str:
    if not locale:
        return settings.default_locale
    locale = locale.split("-")[0].lower()
    return locale if locale in settings.locales else settings.default_locale


def translate(key: str, locale: str, **kwargs) -> str:
    """Look up `key`, then format it with `kwargs`."""
    locale = normalise(locale)
    for candidate in (locale, settings.default_locale, "en"):
        value = catalogue(candidate).get(key)
        if value:
            break
    else:
        value = key

    if kwargs:
        try:
            return value.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return value
    return value


def translator(locale: str):
    """Bind a locale, for use as `t` inside templates."""
    locale = normalise(locale)

    def _t(key: str, **kwargs) -> str:
        return translate(key, locale, **kwargs)

    return _t


def pick_locale(query: str | None, cookie: str | None, user_locale: str | None, header: str | None) -> str:
    """Resolve the locale for a request, most explicit signal first."""
    for candidate in (query, cookie, user_locale):
        if candidate and candidate.split("-")[0].lower() in settings.locales:
            return candidate.split("-")[0].lower()
    for part in (header or "").split(","):
        code = part.split(";")[0].strip().split("-")[0].lower()
        if code in settings.locales:
            return code
    return settings.default_locale


def available_locales() -> list[dict]:
    return [
        {"code": code, "name": LOCALE_NAMES.get(code, code), "short": LOCALE_FLAGS.get(code, code.upper())}
        for code in settings.locales
    ]


def reload_catalogues() -> None:
    """Drop the cache; used by tests and the dev reloader."""
    catalogue.cache_clear()
