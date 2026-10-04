"""FastAPI application factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import UPLOAD_DIR, settings
from app.db import Base, engine
from app.routers import admin, auth, creative, practice, public, survival, userstats
from app.templating import templates

log = logging.getLogger("nbo")

BASE_DIR = Path(__file__).resolve().parent


def _bootstrap_admin() -> None:
    email, password = settings.bootstrap_admin_email, settings.bootstrap_admin_password
    if not (email and password):
        return
    from app.db import SessionLocal
    from app.models.enums import Role
    from app.models.user import User
    from app.services.auth import hash_password

    with SessionLocal() as db:
        if db.query(User).filter(User.email == email).one_or_none() is not None:
            return
        db.add(
            User(
                email=email,
                username=email.split("@")[0][:50],
                full_name="Administrator",
                password_hash=hash_password(password),
                role=Role.ADMIN,
            )
        )
        db.commit()
        log.info("Bootstrapped admin %s", email)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Alembic owns the schema in production; this keeps a fresh checkout and the
    # test database working with no migration step.
    Base.metadata.create_all(bind=engine)
    _bootstrap_admin()
    log.info("NBO platform ready (db=%s)", engine.url)
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="NBO Platform",
        description="Authoring and sitting IBO-style biology olympiad exams.",
        version="0.1.0",
        docs_url="/api/docs" if settings.debug else None,
        redoc_url=None,
        lifespan=lifespan,
    )

    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=UPLOAD_DIR), name="media")

    app.include_router(public.router)
    app.include_router(auth.router)
    app.include_router(survival.router)
    app.include_router(practice.router)
    app.include_router(creative.router)
    app.include_router(userstats.router)
    app.include_router(admin.router)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        # HTMX partials and the exam player's fetch() calls want JSON; a browser
        # navigating to a bad URL wants a page.
        wants_json = (
            request.headers.get("hx-request")
            or "application/json" in (request.headers.get("accept") or "")
            or request.url.path.startswith("/api")
        )
        if exc.status_code == 401 and not wants_json:
            from fastapi.responses import RedirectResponse

            return RedirectResponse(f"/auth/login?next={request.url.path}", status_code=303)
        if wants_json:
            return JSONResponse(
                {"error": exc.detail}, status_code=exc.status_code, headers=exc.headers or {}
            )
        try:
            return templates.error_page(request, exc.status_code, str(exc.detail))
        except Exception:  # pragma: no cover - the error page must never itself 500
            return PlainTextResponse(str(exc.detail), status_code=exc.status_code)

    @app.get("/healthz", response_class=PlainTextResponse, include_in_schema=False)
    async def healthz() -> str:
        return "ok"

    return app


app = create_app()
