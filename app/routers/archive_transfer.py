"""Admin-only export and import of the IBO archive between environments."""
from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from app.archive import transfer
from app.config import settings
from app.db import SessionLocal
from app.models.user import User
from app.services.auth import require_admin
from app.templating import PageContext, get_context

log = logging.getLogger("nbo")
router = APIRouter(prefix="/admin/archive", tags=["admin"])


def _stream():
    # Its own connection: the request's session may be closed before the body ends.
    with SessionLocal() as db:
        yield from transfer.iter_gzip(db.connection())


@router.get("/export")
def export_archive(admin: User = Depends(require_admin)):
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M")
    log.info("archive export by %s", admin.username)
    return StreamingResponse(
        _stream(),
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="nbo-archive-{stamp}.jsonl.gz"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/import")
def import_archive(
    file: UploadFile = File(...),
    ctx: PageContext = Depends(get_context),
    admin: User = Depends(require_admin),
):
    """Replace this environment's archive with an uploaded dump.

    Refused on production (no `NBO_ENV_LABEL`): it deletes the existing archive.
    """
    if not settings.env_label:
        raise HTTPException(status_code=403, detail="archive import is only allowed on non-production environments")
    conn = ctx.db.connection()
    try:
        counts = transfer.load(conn, file.file)
    except (ValueError, OSError, EOFError) as exc:
        ctx.db.rollback()
        raise HTTPException(status_code=400, detail=f"bad archive dump: {exc}")
    log.info("archive imported by %s: %s", admin.username, counts)
    return JSONResponse({"imported": counts, "rows": sum(counts.values())})
