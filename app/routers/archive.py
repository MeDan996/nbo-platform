"""Creative mode: browse the IBO archive and start a draft from any question."""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from app.models.archive import IboAsset, IboExam, IboItem, IboQuestion, IboQuestionElement
from app.models.content import Question
from app.models.enums import Role
from app.models.security import AuditLog
from app.responses import redirect
from app.routers.creative import _staff
from app.services import markup
from app.services.archive_templates import build_draft, rich_to_markdown, source_label, table_to_markdown
from app.templating import PageContext, get_context, templates

router = APIRouter(prefix="/creative/archive", tags=["archive"])

PAGE_SIZE = 40


@router.get("")
def archive_list(
    ctx: PageContext = Depends(get_context),
    year: str = "",
    paper: str = "",
    q: str = "",
    fmt: str = "",
    section: str = "",
    review: str = "",
    page: int = 1,
):
    _staff(ctx)
    db = ctx.db
    stmt = select(IboQuestion).join(IboExam)
    if year.isdigit():
        stmt = stmt.where(IboExam.year == int(year))
    if paper:
        stmt = stmt.where(IboExam.paper_code == paper)
    if fmt:
        stmt = stmt.where(IboQuestion.format == fmt)
    if section:
        stmt = stmt.where(IboQuestion.section == section)
    if review == "hide":
        stmt = stmt.where(IboQuestion.needs_review.is_(False))
    elif review == "only":
        stmt = stmt.where(IboQuestion.needs_review.is_(True))
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(
            IboQuestion.title.ilike(like),
            IboQuestion.id.in_(select(IboQuestionElement.question_id).where(IboQuestionElement.text_plain.ilike(like))),
            IboQuestion.id.in_(select(IboItem.question_id).where(IboItem.text_plain.ilike(like))),
        ))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    page = max(1, page)
    rows = list(db.scalars(
        stmt.options(selectinload(IboQuestion.exam), selectinload(IboQuestion.elements),
                     selectinload(IboQuestion.items))
        .order_by(IboExam.year, IboExam.paper_code, IboQuestion.number)
        .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
    ))
    used = dict(db.execute(
        select(Question.archive_question_id, func.count(Question.id))
        .where(Question.archive_question_id.in_([r.id for r in rows]))
        .group_by(Question.archive_question_id)
    ).all()) if rows else {}

    exams = list(db.scalars(select(IboExam).order_by(IboExam.year, IboExam.paper_code)))
    sections = [s for s in db.scalars(
        select(IboQuestion.section).where(IboQuestion.section.is_not(None)).distinct().order_by(IboQuestion.section))]
    formats = list(db.scalars(select(IboQuestion.format).distinct().order_by(IboQuestion.format)))

    def excerpt(question: IboQuestion) -> str:
        for e in question.elements:
            if e.explanation_id is None and e.role in ("paragraph", "statement_lead") and e.text_plain:
                return e.text_plain
        return question.items[0].text_plain if question.items else ""

    return templates.page(
        ctx, "creative/archive_list.html", nav="creative",
        questions=rows, excerpt=excerpt, used=used, total=total, page=page,
        pages=max(1, -(-total // PAGE_SIZE)), exams=exams, sections=sections, formats=formats,
        years=sorted({e.year for e in exams}),
        filters=(filters := {"year": year, "paper": paper, "q": q, "fmt": fmt, "section": section,
                             "review": review}),
        base_qs=urlencode({k: v for k, v in filters.items() if v}),
    )


def _archive_question(ctx: PageContext, qid: int) -> IboQuestion:
    question = ctx.db.get(IboQuestion, qid)
    if question is None:
        raise HTTPException(status_code=404, detail="common.not_found")
    return question


@router.get("/assets/{asset_id}")
def archive_asset(asset_id: int, ctx: PageContext = Depends(get_context)):
    _staff(ctx)
    row = ctx.db.execute(select(IboAsset.data, IboAsset.mime).where(IboAsset.id == asset_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="common.not_found")
    return Response(content=bytes(row.data), media_type=row.mime,
                    headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"})


@router.get("/{qid}")
def archive_detail(qid: int, ctx: PageContext = Depends(get_context)):
    user = _staff(ctx)
    question = _archive_question(ctx, qid)
    stem = [e for e in question.elements if e.explanation_id is None]
    legends = {}
    for e in stem:
        if e.parent_id is not None:
            legends.setdefault(e.parent_id, []).append(e)
    expl_by_item: dict[int | None, list] = {}
    for x in question.explanations:
        expl_by_item.setdefault(x.item_id, []).append(x)
    expl_elements = {}
    for e in question.elements:
        if e.explanation_id is not None:
            expl_elements.setdefault(e.explanation_id, []).append(e)

    drafts_stmt = select(Question).where(Question.archive_question_id == question.id)
    if not user.can(Role.REVIEWER):
        drafts_stmt = drafts_stmt.where(Question.author_id == user.id)
    drafts = list(ctx.db.scalars(drafts_stmt.order_by(Question.created_at.desc())))

    siblings = list(ctx.db.scalars(
        select(IboQuestion.id).where(IboQuestion.exam_id == question.exam_id).order_by(IboQuestion.number)))
    idx = siblings.index(question.id)
    return templates.page(
        ctx, "creative/archive_detail.html", nav="creative",
        q=question, stem=stem, legends=legends, expl_by_item=expl_by_item, expl_elements=expl_elements,
        drafts=drafts, label=source_label(question),
        rich=lambda text: markup.render(rich_to_markdown(text)),
        table_md=lambda rows: markup.render(table_to_markdown(rows or [])),
        prev_id=siblings[idx - 1] if idx > 0 else None,
        next_id=siblings[idx + 1] if idx + 1 < len(siblings) else None,
    )


@router.post("/{qid}/use")
def use_as_template(qid: int, ctx: PageContext = Depends(get_context)):
    """Copy an archive question into a new draft and open it in the editor."""
    user = _staff(ctx)
    question = _archive_question(ctx, qid)
    draft = build_draft(ctx.db, question, user.id)
    ctx.db.add(AuditLog(actor_id=user.id, action="question.from_archive", entity_type="question",
                        entity_id=draft.id, payload={"archive_question_id": question.id}))
    ctx.db.flush()
    return redirect(ctx, f"/creative/questions/{draft.id}?from_archive=1")
