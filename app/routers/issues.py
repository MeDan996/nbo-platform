"""Issues panel: staff report bugs, changes and feature ideas and update their status."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException
from sqlalchemy import select

from app.models.enums import IssueStatus, IssueType
from app.models.issue import Issue
from app.responses import redirect
from app.templating import PageContext, get_context, templates

router = APIRouter(prefix="/issues", tags=["issues"])

MAX_DESCRIPTION = 5000


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
        filters={"status": status, "type": type},
    )


@router.post("")
def create_issue(
    description: str = Form(...),
    issue_type: str = Form("bug"),
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
    ctx.db.add(
        Issue(description=description[:MAX_DESCRIPTION], issue_type=kind, reporter_id=user.id)
    )
    ctx.db.flush()
    return redirect(ctx, "/issues")


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
