"""Password hashing, signed-cookie sessions and request identity."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db, utcnow
from app.models.enums import Role
from app.models.user import User

_hasher = PasswordHasher()
_serializer = URLSafeTimedSerializer(settings.secret_key, salt="nbo-session")

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,30}$")


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
def hash_password(raw: str) -> str:
    return _hasher.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, raw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(hashed: str) -> bool:
    try:
        return _hasher.check_needs_rehash(hashed)
    except InvalidHashError:
        return True


def password_problems(raw: str) -> list[str]:
    """Deliberately modest rules: length carries most of the strength."""
    problems = []
    if len(raw) < 8:
        problems.append("password_too_short")
    if raw.isdigit():
        problems.append("password_all_digits")
    if raw.lower() in {"password", "12345678", "qwertyui", "biology1"}:
        problems.append("password_too_common")
    return problems


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
def issue_session(user: User, fingerprint: str | None = None) -> str:
    """Sign a compact session payload. Binding the fingerprint into the cookie
    means a stolen cookie replayed from another browser is detectable."""
    payload = {"uid": user.id, "v": user.password_hash[-12:], "fp": (fingerprint or "")[:16]}
    return _serializer.dumps(payload)


def read_session(token: str) -> dict | None:
    try:
        return _serializer.loads(token, max_age=settings.session_max_age)
    except (BadSignature, SignatureExpired):
        return None


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        settings.session_cookie,
        token,
        max_age=settings.session_max_age,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(settings.session_cookie, path="/")


# --------------------------------------------------------------------------- #
# Request identity
# --------------------------------------------------------------------------- #
def client_ip(request: Request) -> str:
    """Resolve the real client address.

    ``X-Forwarded-For`` is attacker-controlled except for the entries appended by
    proxies we actually run, so we only trust the configured number of hops and
    count from the right-hand end. With no proxy configured we ignore the header
    entirely and use the socket address.
    """
    hops = settings.trusted_proxy_hops
    if hops > 0:
        forwarded = request.headers.get("x-forwarded-for", "")
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if len(parts) >= hops:
            candidate = parts[-hops]
            try:
                ipaddress.ip_address(candidate)
                return candidate
            except ValueError:
                pass
    return request.client.host if request.client else "0.0.0.0"


def is_private_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback or addr.is_link_local


def ip_in_cidrs(ip: str, cidrs: list[str]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for cidr in cidrs or []:
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


def device_fingerprint(request: Request) -> str | None:
    """The client posts a fingerprint in a cookie or header; we salt and hash it
    so the raw component data never sits in the database in reversible form."""
    raw = request.cookies.get("nbo_fp") or request.headers.get("x-nbo-fingerprint")
    if not raw:
        return None
    return hash_fingerprint(raw)


def hash_fingerprint(raw: str) -> str:
    return hmac.new(
        settings.secret_key.encode(), raw.strip().encode(), hashlib.sha256
    ).hexdigest()[:48]


def new_token() -> tuple[str, str]:
    """Return (raw token to email, hash to store)."""
    raw = secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


def token_expiry(hours: int = 2):
    return utcnow() + timedelta(hours=hours)


# --------------------------------------------------------------------------- #
# FastAPI dependencies
# --------------------------------------------------------------------------- #
def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """Resolve the signed-in user, or None. Never raises."""
    token = request.cookies.get(settings.session_cookie)
    if not token:
        return None
    data = read_session(token)
    if not data:
        return None
    user = db.get(User, data.get("uid"))
    if not user or not user.is_active or user.is_banned:
        return None
    # A password change invalidates every existing session.
    if data.get("v") != user.password_hash[-12:]:
        return None
    return user


def require_user(user: User | None = Depends(get_current_user)) -> User:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication_required",
            headers={"HX-Redirect": "/auth/login"},
        )
    return user


def require_role(minimum: Role):
    """Dependency factory: `Depends(require_role(Role.AUTHOR))`."""

    def _dep(user: User = Depends(require_user)) -> User:
        if not user.can(minimum):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
        return user

    return _dep


require_author = require_role(Role.AUTHOR)
require_reviewer = require_role(Role.REVIEWER)
require_admin = require_role(Role.ADMIN)


def find_user_by_login(db: Session, login: str) -> User | None:
    login = (login or "").strip()
    if not login:
        return None
    stmt = select(User).where(
        (User.email == login.lower()) | (User.username == login)
    )
    return db.scalars(stmt).first()
