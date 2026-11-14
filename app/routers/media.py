"""Serve uploaded files out of the database."""
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import UPLOAD_DIR
from app.db import get_db
from app.models.media import MediaFile

router = APIRouter()


@router.get("/media/{path:path}")
def get_media(path: str, db: Session = Depends(get_db)):
    row = db.execute(
        select(MediaFile.data, MediaFile.mime_type).where(MediaFile.path == path)
    ).first()
    if row is None:
        # Legacy files written to disk before uploads moved into the database.
        legacy = (UPLOAD_DIR / path).resolve()
        if UPLOAD_DIR.resolve() in legacy.parents and legacy.is_file():
            return FileResponse(legacy)
        raise HTTPException(status_code=404, detail="common.not_found")
    headers = {
        "Cache-Control": "public, max-age=86400",
        "X-Content-Type-Options": "nosniff",
    }
    if row.mime_type == "image/svg+xml":
        headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"
    return Response(content=bytes(row.data), media_type=row.mime_type, headers=headers)
