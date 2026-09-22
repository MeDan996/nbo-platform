"""Creative mode: authoring questions, reviewing them, and assembling exams."""
from __future__ import annotations

import re
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select

from app.config import UPLOAD_DIR, settings
from app.db import utcnow
from app.models.content import (
    Figure,
    FigureTranslation,
    Question,
    QuestionReview,
    QuestionTranslation,
    Statement,
    StatementTranslation,
    Topic,
)
from app.models.enums import (
    Difficulty,
    ExamMode,
    ExamStatus,
    QuestionStatus,
    QuestionType,
    ReviewDecision,
    Role,
)
from app.models.exam import Exam, ExamQuestion, ExamSection
from app.models.security import AuditLog
from app.responses import redirect
from app.services.auth import require_author, require_reviewer
from app.services.scoring import default_curve, expected_guessing_score, resolve_curve
from app.services.slugs import unique_exam_slug
from app.templating import PageContext, get_context, templates

router = APIRouter(prefix="/creative", tags=["creative"])

LABELS = "ABCDEFGH"
ALLOWED_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
MAX_FIGURE_BYTES = 8 * 1024 * 1024
# First bytes of each format we accept, so a renamed .exe cannot be stored.
MAGIC = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
    b"RIFF": "image/webp",
}


def _staff(ctx: PageContext):
    if ctx.user is None:
        raise HTTPException(status_code=401, detail="authentication_required")
    if not ctx.user.is_staff:
        raise HTTPException(status_code=403, detail="common.forbidden")
    return ctx.user


def _owned_question(ctx: PageContext, question_id: int) -> Question:
    question = ctx.db.get(Question, question_id)
    if question is None:
        raise HTTPException(status_code=404, detail="common.not_found")
    user = _staff(ctx)
    if question.author_id != user.id and not user.can(Role.REVIEWER):
        raise HTTPException(status_code=403, detail="common.forbidden")
    return question


# --------------------------------------------------------------------------- #
# Dashboard and bank
# --------------------------------------------------------------------------- #
@router.get("")
def creative_home(ctx: PageContext = Depends(get_context)):
    user = _staff(ctx)
    db = ctx.db
    mine = list(
        db.scalars(
            select(Question)
            .where(Question.author_id == user.id)
            .order_by(Question.updated_at.desc())
            .limit(8)
        )
    )
    counts = {
        status.value: int(
            db.scalar(
                select(func.count(Question.id)).where(
                    Question.author_id == user.id, Question.status == status
                )
            )
            or 0
        )
        for status in QuestionStatus
    }
    pending_review = 0
    if user.can(Role.REVIEWER):
        pending_review = int(
            db.scalar(
                select(func.count(Question.id)).where(
                    Question.status == QuestionStatus.IN_REVIEW
                )
            )
            or 0
        )
    my_exams = list(
        db.scalars(
            select(Exam)
            .where(Exam.owner_id == user.id, Exam.mode != ExamMode.PRACTICE)
            .order_by(Exam.updated_at.desc())
            .limit(6)
        )
    )
    return templates.page(
        ctx,
        "creative/home.html",
        nav="creative",
        recent=mine,
        counts=counts,
        pending_review=pending_review,
        my_exams=my_exams,
        template_count=int(
            db.scalar(
                select(func.count(Question.id)).where(Question.is_official_ibo.is_(True))
            )
            or 0
        ),
    )


@router.get("/questions")
def question_bank(
    ctx: PageContext = Depends(get_context),
    q: str = "",
    topic: str = "",
    status: str = "",
    mine: str = "",
    templates_only: str = "",
):
    user = _staff(ctx)
    db = ctx.db
    stmt = select(Question)

    if mine:
        stmt = stmt.where(Question.author_id == user.id)
    elif not user.can(Role.REVIEWER):
        # Authors see their own drafts plus everything already approved.
        stmt = stmt.where(
            or_(Question.author_id == user.id, Question.status == QuestionStatus.APPROVED)
        )
    if topic:
        topic_row = db.scalars(select(Topic).where(Topic.slug == topic)).first()
        if topic_row is not None:
            stmt = stmt.where(Question.topic_id == topic_row.id)
    if status:
        stmt = stmt.where(Question.status == status)
    if templates_only:
        stmt = stmt.where(Question.is_official_ibo.is_(True))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            Question.id.in_(
                select(QuestionTranslation.question_id).where(
                    or_(
                        QuestionTranslation.stem.ilike(like),
                        QuestionTranslation.title.ilike(like),
                    )
                )
            )
            | Question.source_ref.ilike(like)
        )

    questions = list(db.scalars(stmt.order_by(Question.updated_at.desc()).limit(200)))
    topics = list(db.scalars(select(Topic).order_by(Topic.order, Topic.id)))
    return templates.page(
        ctx,
        "creative/bank.html",
        nav="creative",
        questions=questions,
        topics=topics,
        filters={"q": q, "topic": topic, "status": status, "mine": mine,
                 "templates_only": templates_only},
        statuses=list(QuestionStatus),
    )


# --------------------------------------------------------------------------- #
# Creating and editing
# --------------------------------------------------------------------------- #
@router.post("/questions/new")
def new_question(
    ctx: PageContext = Depends(get_context),
    question_type: str = Form(QuestionType.TF_BLOCK.value),
    topic_id: str = Form(""),
):
    user = _staff(ctx)
    question = Question(
        question_type=QuestionType(question_type),
        status=QuestionStatus.DRAFT,
        author_id=user.id,
        topic_id=int(topic_id) if topic_id.isdigit() else None,
        max_points=1.0,
    )
    ctx.db.add(question)
    ctx.db.flush()

    for locale in settings.locales:
        ctx.db.add(QuestionTranslation(question_id=question.id, locale=locale, stem=""))
    if QuestionType(question_type) is QuestionType.TF_BLOCK:
        # The IBO block is four statements; the author can add or remove them.
        for index in range(4):
            statement = Statement(
                question_id=question.id, order=index, label=LABELS[index], is_true=False
            )
            ctx.db.add(statement)
            ctx.db.flush()
            for locale in settings.locales:
                ctx.db.add(
                    StatementTranslation(statement_id=statement.id, locale=locale, text="")
                )
    ctx.db.flush()
    return redirect(ctx, f"/creative/questions/{question.id}")


@router.post("/questions/{question_id}/clone")
def clone_question(question_id: int, ctx: PageContext = Depends(get_context)):
    """Copy an existing question - typically a real IBO paper - as a starting point.

    The clone records `template_of_id` and drops the `is_official_ibo` marker, so
    a derived question is never mistaken for the original and the provenance
    chain stays intact.
    """
    user = _staff(ctx)
    source = ctx.db.get(Question, question_id)
    if source is None:
        raise HTTPException(status_code=404, detail="common.not_found")

    clone = Question(
        question_type=source.question_type,
        status=QuestionStatus.DRAFT,
        difficulty=source.difficulty,
        topic_id=source.topic_id,
        author_id=user.id,
        source_ref=source.source_ref,
        source_year=source.source_year,
        is_official_ibo=False,
        template_of_id=source.id,
        max_points=source.max_points,
        scoring_curve=source.scoring_curve,
        answer_spec=source.answer_spec,
        estimated_seconds=source.estimated_seconds,
        tags=list(source.tags or []),
    )
    ctx.db.add(clone)
    ctx.db.flush()

    for tr in source.translations:
        ctx.db.add(
            QuestionTranslation(
                question_id=clone.id,
                locale=tr.locale,
                title=tr.title,
                stem=tr.stem,
                instructions=tr.instructions,
                general_explanation=tr.general_explanation,
            )
        )
    for st in source.statements:
        copy = Statement(
            question_id=clone.id, order=st.order, label=st.label,
            is_true=st.is_true, pin_position=st.pin_position,
        )
        ctx.db.add(copy)
        ctx.db.flush()
        for tr in st.translations:
            ctx.db.add(
                StatementTranslation(
                    statement_id=copy.id, locale=tr.locale,
                    text=tr.text, explanation=tr.explanation,
                )
            )
    for fig in source.figures:
        copy_fig = Figure(
            question_id=clone.id, order=fig.order, path=fig.path,
            mime_type=fig.mime_type, width=fig.width, height=fig.height,
            in_appendix=fig.in_appendix,
        )
        ctx.db.add(copy_fig)
        ctx.db.flush()
        for tr in fig.translations:
            ctx.db.add(
                FigureTranslation(
                    figure_id=copy_fig.id, locale=tr.locale,
                    caption=tr.caption, alt_text=tr.alt_text,
                )
            )
    ctx.db.flush()
    return redirect(ctx, f"/creative/questions/{clone.id}")


@router.get("/questions/{question_id}")
def edit_question(question_id: int, ctx: PageContext = Depends(get_context)):
    question = _owned_question(ctx, question_id)
    topics = list(ctx.db.scalars(select(Topic).order_by(Topic.order, Topic.id)))

    statement_count = len(question.statements)
    curve = resolve_curve(statement_count, question.scoring_curve)
    guess_rate = expected_guessing_score(statement_count, curve) if statement_count else 0.0

    return templates.page(
        ctx,
        "creative/edit.html",
        nav="creative",
        question=question,
        topics=topics,
        question_types=list(QuestionType),
        difficulties=list(Difficulty),
        curve=curve,
        default=default_curve(statement_count),
        guess_rate=guess_rate,
        can_review=ctx.user.can(Role.REVIEWER),
        editor_locales=list(settings.locales),
    )


@router.post("/questions/{question_id}")
async def save_question(
    question_id: int, request: Request, ctx: PageContext = Depends(get_context)
):
    """Save the whole editor form: metadata, every translation, every statement."""
    question = _owned_question(ctx, question_id)
    form = await request.form()

    question.topic_id = int(form.get("topic_id")) if str(form.get("topic_id", "")).isdigit() else None
    question.difficulty = Difficulty(form.get("difficulty") or Difficulty.MEDIUM)
    question.question_type = QuestionType(form.get("question_type") or question.question_type)
    try:
        question.max_points = max(0.1, min(50.0, float(form.get("max_points") or 1.0)))
        question.estimated_seconds = max(10, min(7200, int(form.get("estimated_seconds") or 180)))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="bad_request")
    question.source_ref = (form.get("source_ref") or "").strip()[:200] or None
    question.source_year = (
        int(form["source_year"]) if str(form.get("source_year", "")).isdigit() else None
    )
    question.tags = [t.strip() for t in (form.get("tags") or "").split(",") if t.strip()]

    # A blank custom curve means "use the default", which keeps the stored value
    # null rather than freezing today's default into the row.
    raw_curve = (form.get("scoring_curve") or "").strip()
    if raw_curve:
        try:
            values = [float(v) for v in re.split(r"[,\s]+", raw_curve) if v]
            question.scoring_curve = {str(len(values) - 1): values}
        except ValueError:
            raise HTTPException(status_code=400, detail="bad_curve")
    else:
        question.scoring_curve = None

    by_locale = {tr.locale: tr for tr in question.translations}
    for locale in settings.locales:
        tr = by_locale.get(locale)
        if tr is None:
            tr = QuestionTranslation(question_id=question.id, locale=locale)
            ctx.db.add(tr)
        tr.title = (form.get(f"title__{locale}") or "").strip()[:300] or None
        tr.stem = form.get(f"stem__{locale}") or ""
        tr.instructions = (form.get(f"instructions__{locale}") or "").strip() or None
        tr.general_explanation = (form.get(f"explanation__{locale}") or "").strip() or None

    for statement in question.statements:
        statement.is_true = form.get(f"st__{statement.id}__true") == "on"
        statement.pin_position = form.get(f"st__{statement.id}__pin") == "on"
        st_by_locale = {tr.locale: tr for tr in statement.translations}
        for locale in settings.locales:
            tr = st_by_locale.get(locale)
            if tr is None:
                tr = StatementTranslation(statement_id=statement.id, locale=locale)
                ctx.db.add(tr)
            tr.text = form.get(f"st__{statement.id}__text__{locale}") or ""
            tr.explanation = (
                form.get(f"st__{statement.id}__expl__{locale}") or ""
            ).strip() or None

    for figure in question.figures:
        fig_by_locale = {tr.locale: tr for tr in figure.translations}
        for locale in settings.locales:
            tr = fig_by_locale.get(locale)
            if tr is None:
                tr = FigureTranslation(figure_id=figure.id, locale=locale)
                ctx.db.add(tr)
            tr.caption = (form.get(f"fig__{figure.id}__caption__{locale}") or "").strip() or None
            tr.alt_text = (form.get(f"fig__{figure.id}__alt__{locale}") or "").strip() or None

    if question.status is QuestionStatus.CHANGES_REQUESTED:
        question.status = QuestionStatus.DRAFT

    ctx.db.flush()
    return redirect(ctx, f"/creative/questions/{question.id}?saved=1")


@router.post("/questions/{question_id}/statements/add")
def add_statement(question_id: int, ctx: PageContext = Depends(get_context)):
    question = _owned_question(ctx, question_id)
    order = len(question.statements)
    if order >= len(LABELS):
        raise HTTPException(status_code=400, detail="too_many_statements")
    statement = Statement(
        question_id=question.id, order=order, label=LABELS[order], is_true=False
    )
    ctx.db.add(statement)
    ctx.db.flush()
    for locale in settings.locales:
        ctx.db.add(StatementTranslation(statement_id=statement.id, locale=locale, text=""))
    ctx.db.flush()
    return redirect(ctx, f"/creative/questions/{question.id}#statements")


@router.post("/questions/{question_id}/statements/{statement_id}/delete")
def delete_statement(
    question_id: int, statement_id: int, ctx: PageContext = Depends(get_context)
):
    question = _owned_question(ctx, question_id)
    statement = ctx.db.get(Statement, statement_id)
    if statement is None or statement.question_id != question.id:
        raise HTTPException(status_code=404, detail="common.not_found")
    ctx.db.delete(statement)
    ctx.db.flush()
    # Re-label what is left so the block stays A, B, C, ...
    for index, remaining in enumerate(
        sorted(question.statements, key=lambda s: s.order)
    ):
        remaining.order = index
        remaining.label = LABELS[index]
    ctx.db.flush()
    return redirect(ctx, f"/creative/questions/{question.id}#statements")


# --------------------------------------------------------------------------- #
# Figures
# --------------------------------------------------------------------------- #
def _validate_image(data: bytes, filename: str) -> str:
    """Return a mime type, or raise. Extension and magic bytes must agree."""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_IMAGE_EXT:
        raise HTTPException(status_code=400, detail="unsupported_image_type")
    if len(data) > MAX_FIGURE_BYTES:
        raise HTTPException(status_code=400, detail="image_too_large")
    if ext == ".svg":
        # SVG is XML and can carry script, so it is stored only if it has no
        # script or event handlers at all.
        text = data[:200_000].decode("utf-8", errors="ignore").lower()
        if "<script" in text or "javascript:" in text or re.search(r"\son\w+\s*=", text):
            raise HTTPException(status_code=400, detail="unsafe_svg")
        return "image/svg+xml"
    for magic, mime in MAGIC.items():
        if data.startswith(magic):
            return mime
    raise HTTPException(status_code=400, detail="not_an_image")


@router.post("/questions/{question_id}/figures")
async def upload_figure(
    question_id: int,
    ctx: PageContext = Depends(get_context),
    file: UploadFile = File(...),
    in_appendix: str = Form(""),
):
    question = _owned_question(ctx, question_id)
    data = await file.read()
    mime = _validate_image(data, file.filename or "upload.png")

    folder = UPLOAD_DIR / "figures" / str(question.id)
    folder.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename or "x.png").suffix.lower()
    name = f"{secrets.token_hex(8)}{ext}"
    (folder / name).write_bytes(data)

    figure = Figure(
        question_id=question.id,
        order=len(question.figures),
        path=f"figures/{question.id}/{name}",
        mime_type=mime,
        in_appendix=bool(in_appendix),
    )
    ctx.db.add(figure)
    ctx.db.flush()
    for locale in settings.locales:
        ctx.db.add(FigureTranslation(figure_id=figure.id, locale=locale))
    ctx.db.flush()
    return redirect(ctx, f"/creative/questions/{question.id}#figures")


@router.post("/questions/{question_id}/figures/{figure_id}/delete")
def delete_figure(question_id: int, figure_id: int, ctx: PageContext = Depends(get_context)):
    question = _owned_question(ctx, question_id)
    figure = ctx.db.get(Figure, figure_id)
    if figure is None or figure.question_id != question.id:
        raise HTTPException(status_code=404, detail="common.not_found")
    # The file itself is left on disk: another question may have been cloned from
    # this one and still reference the same path.
    ctx.db.delete(figure)
    ctx.db.flush()
    return redirect(ctx, f"/creative/questions/{question.id}#figures")


# --------------------------------------------------------------------------- #
# Review workflow
# --------------------------------------------------------------------------- #
@router.post("/questions/{question_id}/submit")
def submit_for_review(question_id: int, ctx: PageContext = Depends(get_context)):
    question = _owned_question(ctx, question_id)
    problems = validate_question(question)
    if problems:
        return templates.page(
            ctx,
            "creative/validation.html",
            status_code=400,
            nav="creative",
            question=question,
            problems=problems,
        )
    question.status = QuestionStatus.IN_REVIEW
    ctx.db.flush()
    return redirect(ctx, f"/creative/questions/{question.id}")


def validate_question(question: Question) -> list[str]:
    """Everything that must hold before a question can go in front of students."""
    problems: list[str] = []
    default_locale = settings.default_locale
    by_locale = {tr.locale: tr for tr in question.translations}

    primary = by_locale.get(default_locale)
    if primary is None or not (primary.stem or "").strip():
        problems.append(f"Question text is empty in the primary language ({default_locale}).")

    qtype = QuestionType(question.question_type)
    if qtype in (QuestionType.TF_BLOCK, QuestionType.MCQ_SINGLE, QuestionType.MCQ_MULTI):
        if len(question.statements) < 2:
            problems.append("At least two statements are required.")
        for statement in question.statements:
            tr = {t.locale: t for t in statement.translations}.get(default_locale)
            if tr is None or not (tr.text or "").strip():
                problems.append(
                    f"Statement {statement.label} has no text in {default_locale}."
                )
        if qtype is QuestionType.MCQ_SINGLE:
            correct = sum(1 for s in question.statements if s.is_true)
            if correct != 1:
                problems.append(
                    f"A single-answer question needs exactly one correct option (found {correct})."
                )
        if qtype is QuestionType.TF_BLOCK:
            truths = sum(1 for s in question.statements if s.is_true)
            if truths in (0, len(question.statements)) and len(question.statements) > 2:
                problems.append(
                    "Every statement has the same answer, which makes the block guessable."
                )
    elif qtype is QuestionType.NUMERIC:
        if not (question.answer_spec or {}).get("value"):
            problems.append("A numeric question needs an expected value.")
    elif qtype is QuestionType.SHORT_TEXT:
        if not (question.answer_spec or {}).get("accept"):
            problems.append("A short-answer question needs at least one accepted answer.")

    if question.topic_id is None:
        problems.append("Pick a topic, or the question cannot appear in practice sets.")

    if question.statements:
        curve = resolve_curve(len(question.statements), question.scoring_curve)
        rate = expected_guessing_score(len(question.statements), curve)
        if rate > 0.4:
            problems.append(
                f"With this scoring curve, random guessing scores {rate * 100:.0f}% on average."
            )
    return problems


@router.get("/review")
def review_queue(ctx: PageContext = Depends(get_context), user=Depends(require_reviewer)):
    questions = list(
        ctx.db.scalars(
            select(Question)
            .where(Question.status == QuestionStatus.IN_REVIEW)
            .order_by(Question.updated_at)
        )
    )
    return templates.page(ctx, "creative/review.html", nav="creative", questions=questions)


@router.post("/questions/{question_id}/review")
def submit_review(
    question_id: int,
    ctx: PageContext = Depends(get_context),
    user=Depends(require_reviewer),
    decision: str = Form(...),
    comment: str = Form(""),
):
    question = ctx.db.get(Question, question_id)
    if question is None:
        raise HTTPException(status_code=404, detail="common.not_found")

    choice = ReviewDecision(decision)
    ctx.db.add(
        QuestionReview(
            question_id=question.id,
            reviewer_id=user.id,
            decision=choice,
            comment=comment.strip() or None,
        )
    )
    if choice is ReviewDecision.APPROVE:
        problems = validate_question(question)
        if problems:
            return templates.page(
                ctx,
                "creative/validation.html",
                status_code=400,
                nav="creative",
                question=question,
                problems=problems,
            )
        question.status = QuestionStatus.APPROVED
    elif choice is ReviewDecision.REQUEST_CHANGES:
        question.status = QuestionStatus.CHANGES_REQUESTED

    ctx.db.add(
        AuditLog(
            actor_id=user.id,
            action=f"question_review_{choice.value}",
            entity_type="question",
            entity_id=question.id,
            payload={"comment": comment[:500]},
            ip_address=ctx.ip,
        )
    )
    ctx.db.flush()
    return redirect(ctx, "/creative/review")


# --------------------------------------------------------------------------- #
# Exam builder
# --------------------------------------------------------------------------- #
@router.get("/exams")
def exam_list(ctx: PageContext = Depends(get_context)):
    _staff(ctx)
    exams = list(
        ctx.db.scalars(
            select(Exam)
            .where(Exam.mode != ExamMode.PRACTICE)
            .order_by(Exam.updated_at.desc())
        )
    )
    return templates.page(ctx, "creative/exams.html", nav="creative", exams=exams)


@router.post("/exams/new")
def new_exam(ctx: PageContext = Depends(get_context), title: str = Form("Untitled exam")):
    user = _staff(ctx)
    exam = Exam(
        slug=unique_exam_slug(ctx.db, title or "exam"),
        title=title.strip()[:300] or "Untitled exam",
        owner_id=user.id,
        mode=ExamMode.MOCK,
        status=ExamStatus.DRAFT,
    )
    ctx.db.add(exam)
    ctx.db.flush()
    return redirect(ctx, f"/creative/exams/{exam.id}")


@router.get("/exams/{exam_id}")
def edit_exam(exam_id: int, ctx: PageContext = Depends(get_context)):
    _staff(ctx)
    exam = ctx.db.get(Exam, exam_id)
    if exam is None:
        raise HTTPException(status_code=404, detail="common.not_found")
    available = list(
        ctx.db.scalars(
            select(Question)
            .where(Question.status == QuestionStatus.APPROVED)
            .order_by(Question.topic_id, Question.id)
            .limit(500)
        )
    )
    on_paper = {eq.question_id for eq in exam.questions}
    topics = list(ctx.db.scalars(select(Topic).order_by(Topic.order, Topic.id)))
    return templates.page(
        ctx,
        "creative/exam_edit.html",
        nav="creative",
        exam=exam,
        available=[q for q in available if q.id not in on_paper],
        topics=topics,
        modes=list(ExamMode),
        statuses=list(ExamStatus),
    )


@router.post("/exams/{exam_id}")
async def save_exam(exam_id: int, request: Request, ctx: PageContext = Depends(get_context)):
    _staff(ctx)
    exam = ctx.db.get(Exam, exam_id)
    if exam is None:
        raise HTTPException(status_code=404, detail="common.not_found")
    form = await request.form()

    exam.title = (form.get("title") or exam.title).strip()[:300]
    exam.subtitle = (form.get("subtitle") or "").strip()[:300] or None
    exam.description = (form.get("description") or "").strip() or None
    exam.title_i18n = {
        locale: (form.get(f"title__{locale}") or "").strip()
        for locale in settings.locales
        if (form.get(f"title__{locale}") or "").strip()
    }
    exam.mode = ExamMode(form.get("mode") or exam.mode)
    exam.status = ExamStatus(form.get("status") or exam.status)
    try:
        exam.duration_minutes = max(1, min(1440, int(form.get("duration_minutes") or 195)))
        exam.max_attempts = max(1, min(20, int(form.get("max_attempts") or 1)))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="bad_request")

    exam.is_rated = form.get("is_rated") == "on"
    exam.shuffle_questions = form.get("shuffle_questions") == "on"
    exam.shuffle_statements = form.get("shuffle_statements") == "on"
    exam.show_feedback_immediately = form.get("show_feedback_immediately") == "on"
    exam.registration_required = form.get("registration_required") == "on"

    for field in ("opens_at", "closes_at", "results_visible_at"):
        raw = (form.get(field) or "").strip()
        if raw:
            from datetime import datetime, timezone

            try:
                setattr(
                    exam, field,
                    datetime.fromisoformat(raw).replace(tzinfo=timezone.utc),
                )
            except ValueError:
                raise HTTPException(status_code=400, detail="bad_datetime")
        else:
            setattr(exam, field, None)

    policy = dict(exam.anticheat_policy or {})
    for key in (
        "block_duplicate_ip", "block_duplicate_device", "require_fullscreen",
        "track_focus_loss", "block_copy_paste", "detect_collusion",
    ):
        policy[key] = form.get(f"policy__{key}") == "on"
    if str(form.get("policy__ip_account_threshold", "")).isdigit():
        policy["ip_account_threshold"] = max(
            1, min(200, int(form["policy__ip_account_threshold"]))
        )
    if str(form.get("policy__max_focus_losses", "")).isdigit():
        policy["max_focus_losses"] = max(0, min(100, int(form["policy__max_focus_losses"])))
    exam.anticheat_policy = policy

    ctx.db.flush()
    return redirect(ctx, f"/creative/exams/{exam.id}?saved=1")


@router.post("/exams/{exam_id}/questions/add")
def add_to_exam(
    exam_id: int,
    ctx: PageContext = Depends(get_context),
    question_id: int = Form(...),
    points: float = Form(1.0),
):
    _staff(ctx)
    exam = ctx.db.get(Exam, exam_id)
    question = ctx.db.get(Question, question_id)
    if exam is None or question is None:
        raise HTTPException(status_code=404, detail="common.not_found")
    if any(eq.question_id == question.id for eq in exam.questions):
        return redirect(ctx, f"/creative/exams/{exam.id}")

    section = None
    if question.topic_id is not None:
        section = next(
            (s for s in exam.sections if s.topic_id == question.topic_id), None
        )
        if section is None:
            topic = ctx.db.get(Topic, question.topic_id)
            section = ExamSection(
                exam_id=exam.id,
                topic_id=question.topic_id,
                title=topic.name(settings.default_locale) if topic else "",
                title_i18n={loc: topic.name(loc) for loc in settings.locales} if topic else {},
                order=len(exam.sections),
            )
            ctx.db.add(section)
            ctx.db.flush()

    ctx.db.add(
        ExamQuestion(
            exam_id=exam.id,
            section_id=section.id if section else None,
            question_id=question.id,
            order=len(exam.questions),
            points=max(0.1, min(50.0, float(points))),
        )
    )
    ctx.db.flush()
    return redirect(ctx, f"/creative/exams/{exam.id}#questions")


@router.post("/exams/{exam_id}/questions/{exam_question_id}/remove")
def remove_from_exam(
    exam_id: int, exam_question_id: int, ctx: PageContext = Depends(get_context)
):
    _staff(ctx)
    exam_question = ctx.db.get(ExamQuestion, exam_question_id)
    if exam_question is None or exam_question.exam_id != exam_id:
        raise HTTPException(status_code=404, detail="common.not_found")
    ctx.db.delete(exam_question)
    ctx.db.flush()
    return redirect(ctx, f"/creative/exams/{exam_id}#questions")


@router.post("/exams/{exam_id}/publish")
def publish_exam(exam_id: int, ctx: PageContext = Depends(get_context)):
    user = _staff(ctx)
    exam = ctx.db.get(Exam, exam_id)
    if exam is None:
        raise HTTPException(status_code=404, detail="common.not_found")
    if not exam.questions:
        raise HTTPException(status_code=400, detail="exam_has_no_questions")
    exam.status = ExamStatus.SCHEDULED if exam.opens_at else ExamStatus.OPEN
    ctx.db.add(
        AuditLog(
            actor_id=user.id,
            action="exam_published",
            entity_type="exam",
            entity_id=exam.id,
            payload={"questions": len(exam.questions), "mode": exam.mode},
            ip_address=ctx.ip,
        )
    )
    ctx.db.flush()
    return redirect(ctx, f"/creative/exams/{exam.id}")
