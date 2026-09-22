"""Jinja environment, request context and shared template helpers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db, utcnow
from app.models.user import User
from app.services import markup
from app.services.auth import client_ip, device_fingerprint, get_current_user
from app.services.i18n import available_locales, pick_locale, translator
from app.services.rating import tier_for

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


@dataclass
class PageContext:
    """Everything a template needs that is not specific to one page."""

    request: Request
    db: Session
    user: User | None
    locale: str
    ip: str
    fingerprint: str | None

    @property
    def t(self):
        return translator(self.locale)

    def as_dict(self, **extra) -> dict:
        data = {
            "request": self.request,
            "user": self.user,
            "locale": self.locale,
            "t": self.t,
            "locales": available_locales(),
            "now": utcnow(),
            "settings": settings,
            "ctx": self,
        }
        data.update(extra)
        return data


def get_context(
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user),
) -> PageContext:
    locale = pick_locale(
        request.query_params.get("lang"),
        request.cookies.get("nbo_lang"),
        user.locale if user else None,
        request.headers.get("accept-language"),
    )
    return PageContext(
        request=request,
        db=db,
        user=user,
        locale=locale,
        ip=client_ip(request),
        fingerprint=device_fingerprint(request),
    )


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #
def _percent(value: float | None, digits: int = 0) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.{digits}f}%"


def _points(value: float | None) -> str:
    if value is None:
        return "—"
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def _duration(seconds: int | float | None) -> str:
    if not seconds:
        return "0:00"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _datetime(value: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.strftime(fmt)


def _relative(value: datetime | None, locale: str = "en") -> str:
    """Coarse relative time. Precise enough for 'closes in 2 h'."""
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    delta = (value - utcnow()).total_seconds()
    future = delta > 0
    delta = abs(delta)
    if delta < 60:
        amount, unit = int(delta), "s"
    elif delta < 3600:
        amount, unit = int(delta // 60), "m"
    elif delta < 86400:
        amount, unit = int(delta // 3600), "h"
    else:
        amount, unit = int(delta // 86400), "d"
    return f"in {amount}{unit}" if future else f"{amount}{unit} ago"


def _initials(name: str | None) -> str:
    parts = [p for p in (name or "").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


def asset_url(path: str) -> str:
    """Static URL stamped with the file's mtime.

    Without this a deployed CSS or JS change keeps serving from the browser
    cache, which during development looks exactly like an edit that did not take
    effect, and in production leaves participants on a stale exam player.
    """
    relative = path.lstrip("/")
    file_path = Path(__file__).resolve().parent / relative
    try:
        stamp = int(file_path.stat().st_mtime)
    except OSError:
        return f"/{relative}"
    return f"/{relative}?v={stamp}"


def _finalize(value):
    """Render a missing value as nothing rather than the string "None".

    Most columns here are nullable, and Jinja's default for `{{ None }}` is the
    literal text `None` - which showed up as pre-filled "None" inside empty
    editor textareas. Handling it once here beats an `or ''` on every field.
    """
    return "" if value is None else value


class Templating:
    def __init__(self) -> None:
        self.engine = Jinja2Templates(directory=str(TEMPLATE_DIR))
        env = self.engine.env
        env.finalize = _finalize
        env.filters["md"] = markup.render
        env.filters["excerpt"] = markup.plain
        env.filters["pct"] = _percent
        env.filters["points"] = _points
        env.filters["duration"] = _duration
        env.filters["dt"] = _datetime
        env.filters["since"] = _relative
        env.filters["initials"] = _initials
        env.globals["tier_for"] = tier_for
        env.globals["app_name"] = "NBO"
        env.globals["asset"] = asset_url
        env.trim_blocks = True
        env.lstrip_blocks = True

    def page(self, ctx: PageContext, name: str, status_code: int = 200, **extra) -> HTMLResponse:
        return self.engine.TemplateResponse(
            ctx.request, name, ctx.as_dict(**extra), status_code=status_code
        )

    def partial(self, ctx: PageContext, name: str, **extra) -> HTMLResponse:
        """Render without the page shell, for HTMX swaps."""
        return self.engine.TemplateResponse(ctx.request, name, ctx.as_dict(**extra))

    def error_page(self, request: Request, status_code: int, detail: str) -> HTMLResponse:
        locale = pick_locale(
            request.query_params.get("lang"),
            request.cookies.get("nbo_lang"),
            None,
            request.headers.get("accept-language"),
        )
        return self.engine.TemplateResponse(
            request,
            "error.html",
            {
                "request": request,
                "user": None,
                "locale": locale,
                "t": translator(locale),
                "locales": available_locales(),
                "now": utcnow(),
                "settings": settings,
                "status_code": status_code,
                "detail": detail,
            },
            status_code=status_code,
        )


templates = Templating()
