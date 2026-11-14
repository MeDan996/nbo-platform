"""Issues panel: staff report bugs, changes and feature ideas and update their status."""
from __future__ import annotations

import re
from pathlib import PurePath

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from sqlalchemy import select

from app.models.enums import IssueStatus, IssueType
from app.models.issue import Issue, IssueAttachment
from app.responses import redirect
from app.templating import PageContext, get_context, templates

router = APIRouter(prefix="/issues", tags=["issues"])

MAX_DESCRIPTION = 5000
MAX_FILES = 5
MAX_FILE_BYTES = 8 * 1024 * 1024

# extension -> (mime type, check that the content really is that kind of file)
_ZIP = lambda d: d.startswith(b"PK\x03\x04")
_TEXT = lambda d: b"\x00" not in d[:4096]
ALLOWED = {
    ".png": ("image/png", lambda d: d.startswith(b"\x89PNG\r\n\x1a\n")),
    ".jpg": ("image/jpeg", lambda d: d.startswith(b"\xff\xd8\xff")),
    ".jpeg": ("image/jpeg", lambda d: d.startswith(b"\xff\xd8\xff")),
    ".gif": ("image/gif", lambda d: d.startswith((b"GIF87a", b"GIF89a"))),
    ".webp": ("image/webp", lambda d: d[:4] == b"RIFF" and d[8:12] == b"WEBP"),
    ".heic": ("image/heic", lambda d: d[4:8] == b"ftyp"),
    ".pdf": ("application/pdf", lambda d: d.startswith(b"%PDF")),
    ".txt": ("text/plain", _TEXT),
    ".log": ("text/plain", _TEXT),
    ".csv": ("text/csv", _TEXT),
    ".json": ("application/json", _TEXT),
    ".docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", _ZIP),
    ".xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", _ZIP),
}
ACCEPT = "image/*," + ",".join(sorted(ALLOWED))


def _clean_filename(name: str) -> str:
    base = PurePath((name or "file").replace("\\", "/")).name
    base = re.sub(r"[^\w.\- ]+", "_", base).strip(" .") or "file"
    return base[-120:]


async def _read_attachments(files: list[UploadFile]) -> list[tuple[str, str, bytes]]:
    """Validate uploads. Empty file inputs (no file chosen) are skipped."""
    chosen = [f for f in files if f and f.filename]
    if len(chosen) > MAX_FILES:
        raise HTTPException(status_code=400, detail="issues.error.too_many_files")
    out = []
    for upload in chosen:
        name = _clean_filename(upload.filename)
        ext = PurePath(name).suffix.lower()
        if ext not in ALLOWED:
            raise HTTPException(status_code=400, detail="issues.error.file_type")
        data = await upload.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(status_code=400, detail="issues.error.file_too_large")
        mime, looks_right = ALLOWED[ext]
        if not data or not looks_right(data):
            raise HTTPException(status_code=400, detail="issues.error.file_type")
        out.append((name, mime, data))
    return out


def _staff(ctx: PageContext):
    if ctx.user is None:
        raise HTTPException(status_code=401, detail="authentication_required")
    if not ctx.user.is_staff:
        raise HTTPException(status_code=403, detail="common.forbidden")
    return ctx.user


@router.get("")
def list_issues(
    status: str = "",
    type: str = "",
    ctx: PageContext = Depends(get_context),
):
    _staff(ctx)
    query = select(Issue).order_by(Issue.id.desc())
    if status in {s.value for s in IssueStatus}:
        query = query.where(Issue.status == IssueStatus(status))
    else:
        status = ""
    if type in {t.value for t in IssueType}:
        query = query.where(Issue.issue_type == IssueType(type))
    else:
        type = ""
    issues = list(ctx.db.scalars(query))
    return templates.page(
        ctx,
        "issues/list.html",
        nav="issues",
        issues=issues,
        issue_types=list(IssueType),
        issue_statuses=list(IssueStatus),
        accept=ACCEPT,
        max_files=MAX_FILES,
        max_mb=MAX_FILE_BYTES // (1024 * 1024),
        filters={"status": status, "type": type},
    )


@router.post("")
async def create_issue(
    description: str = Form(...),
    issue_type: str = Form("bug"),
    files: list[UploadFile] = File(default=[]),
    ctx: PageContext = Depends(get_context),
):
    user = _staff(ctx)
    description = description.strip()
    if not description:
        raise HTTPException(status_code=400, detail="common.required")
    try:
        kind = IssueType(issue_type)
    except ValueError:
        raise HTTPException(status_code=400, detail="issues.type")
    attachments = await _read_attachments(files)
    issue = Issue(description=description[:MAX_DESCRIPTION], issue_type=kind, reporter_id=user.id)
    ctx.db.add(issue)
    ctx.db.flush()
    for name, mime, data in attachments:
        ctx.db.add(
            IssueAttachment(
                issue_id=issue.id, filename=name, mime_type=mime, size=len(data), data=data
            )
        )
    ctx.db.flush()
    return redirect(ctx, "/issues")


@router.get("/files/{attachment_id}")
def get_attachment(attachment_id: int, ctx: PageContext = Depends(get_context)):
    _staff(ctx)
    attachment = ctx.db.get(IssueAttachment, attachment_id)
    if attachment is None:
        raise HTTPException(status_code=404, detail="common.not_found")
    inline = attachment.is_image
    disposition = "inline" if inline else "attachment"
    safe_name = attachment.filename.encode("ascii", "replace").decode()
    return Response(
        content=bytes(attachment.data),
        media_type=attachment.mime_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="{safe_name}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=3600",
        },
    )


@router.post("/{issue_id}/status")
def set_status(
    issue_id: int,
    status: str = Form(...),
    ctx: PageContext = Depends(get_context),
):
    _staff(ctx)
    issue = ctx.db.get(Issue, issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="common.not_found")
    try:
        issue.status = IssueStatus(status)
    except ValueError:
        raise HTTPException(status_code=400, detail="issues.status")
    ctx.db.flush()
    return redirect(ctx, "/issues")
