"""Registration, login, logout and locale switching."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.config import settings
from app.db import utcnow
from app.models.enums import Role
from app.models.security import AuditLog, LoginEvent
from app.models.user import School, User
from app.responses import redirect
from app.services import anticheat
from app.services.auth import (
    USERNAME_RE,
    clear_session_cookie,
    find_user_by_login,
    hash_password,
    issue_session,
    password_problems,
    set_session_cookie,
    verify_password,
)
from app.services.i18n import normalise
from app.templating import PageContext, get_context, templates

router = APIRouter(prefix="/auth", tags=["auth"])


def _safe_next(target: str | None) -> str:
    """Only allow same-site relative redirects, so `?next=` cannot be used to
    bounce a freshly-authenticated user to an attacker's page."""
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


@router.get("/login")
def login_form(request: Request, ctx: PageContext = Depends(get_context), next: str = "/"):
    if ctx.user is not None:
        return redirect(ctx, _safe_next(next))
    return templates.page(ctx, "auth/login.html", nav="", next=_safe_next(next), errors=[])


@router.post("/login")
def login(
    request: Request,
    ctx: PageContext = Depends(get_context),
    login: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    db = ctx.db
    user = find_user_by_login(db, login)
    ok = user is not None and verify_password(password, user.password_hash)

    db.add(
        LoginEvent(
            user_id=user.id if user else None,
            email_attempted=login[:255],
            success=bool(ok and user and not user.is_banned),
            at=utcnow(),
            ip_address=ctx.ip,
            fingerprint=ctx.fingerprint,
            user_agent=(request.headers.get("user-agent") or "")[:400],
            reason=None if ok else "bad_credentials",
        )
    )

    if not ok:
        return templates.page(
            ctx,
            "auth/login.html",
            status_code=400,
            nav="",
            next=_safe_next(next),
            errors=["auth.error.invalid_credentials"],
            login_value=login,
        )
    if user.is_banned:
        return templates.page(
            ctx,
            "auth/login.html",
            status_code=403,
            nav="",
            next=_safe_next(next),
            errors=["auth.error.account_banned"],
        )

    user.last_login_at = utcnow()
    anticheat.touch_ip(db, ctx.ip, user)
    if ctx.fingerprint:
        anticheat.touch_device(db, ctx.fingerprint, user)

    response = redirect(ctx, _safe_next(next))
    set_session_cookie(response, issue_session(user, ctx.fingerprint))
    response.set_cookie("nbo_lang", user.locale, max_age=31536000, samesite="lax", path="/")
    return response


@router.get("/register")
def register_form(ctx: PageContext = Depends(get_context)):
    if ctx.user is not None:
        return redirect(ctx, "/")
    schools = list(ctx.db.scalars(select(School).order_by(School.name)))
    return templates.page(ctx, "auth/register.html", nav="", schools=schools, errors=[], values={})


@router.post("/register")
def register(
    request: Request,
    ctx: PageContext = Depends(get_context),
    email: str = Form(...),
    username: str = Form(...),
    full_name: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    school_id: str = Form(""),
    grade: str = Form(""),
):
    db = ctx.db
    email = email.strip().lower()
    username = username.strip()
    errors: list[str] = []

    if db.scalars(select(User).where(User.email == email)).first():
        errors.append("auth.error.email_taken")
    if db.scalars(select(User).where(User.username == username)).first():
        errors.append("auth.error.username_taken")
    if not USERNAME_RE.match(username):
        errors.append("auth.error.username_invalid")
    if password != password_confirm:
        errors.append("auth.error.password_mismatch")
    errors.extend(f"auth.error.{p}" for p in password_problems(password))

    if errors:
        schools = list(db.scalars(select(School).order_by(School.name)))
        return templates.page(
            ctx,
            "auth/register.html",
            status_code=400,
            nav="",
            schools=schools,
            errors=errors,
            values={
                "email": email,
                "username": username,
                "full_name": full_name,
                "school_id": school_id,
                "grade": grade,
            },
        )

    user = User(
        email=email,
        username=username,
        full_name=full_name.strip()[:200],
        password_hash=hash_password(password),
        role=Role.PARTICIPANT,
        locale=normalise(ctx.locale),
        school_id=int(school_id) if school_id.isdigit() else None,
        grade=int(grade) if grade.isdigit() else None,
        registration_ip=ctx.ip,
        registration_fingerprint=ctx.fingerprint,
    )
    db.add(user)
    db.flush()

    anticheat.touch_ip(db, ctx.ip, user, is_registration=True)
    if ctx.fingerprint:
        anticheat.touch_device(db, ctx.fingerprint, user)

    # Registration-time signals never block sign-up - a shared family computer is
    # ordinary - but they are recorded so the integrity queue has the history if
    # the same cluster of accounts later turns up in one exam.
    signals = anticheat.registration_signals(db, ctx.ip, ctx.fingerprint)
    for signal in signals:
        db.add(
            AuditLog(
                actor_id=user.id,
                action="registration_signal",
                entity_type="user",
                entity_id=user.id,
                payload=signal,
                ip_address=ctx.ip,
            )
        )

    db.add(
        LoginEvent(
            user_id=user.id,
            email_attempted=email,
            success=True,
            at=utcnow(),
            ip_address=ctx.ip,
            fingerprint=ctx.fingerprint,
            user_agent=(request.headers.get("user-agent") or "")[:400],
            reason="registration",
        )
    )

    response = redirect(ctx, "/")
    set_session_cookie(response, issue_session(user, ctx.fingerprint))
    response.set_cookie("nbo_lang", user.locale, max_age=31536000, samesite="lax", path="/")
    return response


@router.post("/logout")
def logout():
    # Nothing to commit here, so a plain redirect is correct.
    response = RedirectResponse("/", status_code=303)
    clear_session_cookie(response)
    return response


@router.post("/locale")
def set_locale(
    ctx: PageContext = Depends(get_context),
    locale: str = Form(...),
    next: str = Form("/"),
):
    locale = normalise(locale)
    if ctx.user is not None:
        ctx.user.locale = locale
    response = redirect(ctx, _safe_next(next))
    response.set_cookie("nbo_lang", locale, max_age=31536000, samesite="lax", path="/")
    return response
