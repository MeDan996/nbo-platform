"""Survival mode: sitting exams, autosaving, submitting, results and standings."""
from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select

from app.db import utcnow
from app.models.analytics import ExamStatistics
from app.models.attempt import Attempt
from app.models.content import Question, Topic
from app.models.enums import AttemptStatus, EventKind, ExamMode, ExamStatus
from app.models.exam import Exam
from app.models.security import CheatFlag
from app.responses import redirect
from app.services import anticheat, exam_engine, stats
from app.services.auth import require_user
from app.services.exam_engine import ExamEngineError
from app.templating import PageContext, get_context, templates

router = APIRouter(prefix="/survival", tags=["survival"])


def _get_exam(ctx: PageContext, slug: str) -> Exam:
    exam = ctx.db.scalars(select(Exam).where(Exam.slug == slug)).first()
    is_staff = bool(ctx.user and ctx.user.is_staff)
    if exam is None or (exam.status is ExamStatus.DRAFT and not is_staff):
        raise HTTPException(status_code=404, detail="common.not_found")
    return exam


def _get_attempt(ctx: PageContext, attempt_id: int) -> Attempt:
    attempt = ctx.db.get(Attempt, attempt_id)
    if attempt is None:
        raise HTTPException(status_code=404, detail="common.not_found")
    if ctx.user is None or (
        attempt.user_id != ctx.user.id and not ctx.user.is_admin
    ):
        raise HTTPException(status_code=403, detail="common.forbidden")
    return attempt


# --------------------------------------------------------------------------- #
# Browsing
# --------------------------------------------------------------------------- #
@router.get("")
def exam_list(ctx: PageContext = Depends(get_context)):
    exams = list(
        ctx.db.scalars(
            select(Exam)
            .where(
                Exam.status.in_([ExamStatus.OPEN, ExamStatus.SCHEDULED, ExamStatus.CLOSED]),
                Exam.mode != ExamMode.PRACTICE,
            )
            .order_by(Exam.opens_at.desc().nullslast(), Exam.id.desc())
        )
    )
    my_attempts: dict[int, Attempt] = {}
    if ctx.user is not None:
        for attempt in ctx.db.scalars(
            select(Attempt)
            .where(Attempt.user_id == ctx.user.id)
            .order_by(Attempt.id.desc())
        ):
            my_attempts.setdefault(attempt.exam_id, attempt)

    now = utcnow()
    return templates.page(
        ctx,
        "survival/list.html",
        nav="survival",
        open_exams=[e for e in exams if e.is_open_at(now)],
        upcoming=[e for e in exams if e.opens_at and e.opens_at > now],
        past=[e for e in exams if e.closes_at and e.closes_at < now],
        my_attempts=my_attempts,
    )


@router.get("/{slug}")
def exam_detail(slug: str, ctx: PageContext = Depends(get_context)):
    exam = _get_exam(ctx, slug)
    my_attempts = []
    decision = None
    if ctx.user is not None:
        my_attempts = list(
            ctx.db.scalars(
                select(Attempt)
                .where(Attempt.exam_id == exam.id, Attempt.user_id == ctx.user.id)
                .order_by(Attempt.id.desc())
            )
        )
        decision = anticheat.check_exam_access(
            ctx.db, ctx.user, exam, ctx.ip, ctx.fingerprint
        )
    return templates.page(
        ctx,
        "survival/detail.html",
        nav="survival",
        exam=exam,
        my_attempts=my_attempts,
        decision=decision,
        results_released=exam.results_released(),
    )


# --------------------------------------------------------------------------- #
# Starting
# --------------------------------------------------------------------------- #
@router.post("/{slug}/start")
def start(request: Request, slug: str, ctx: PageContext = Depends(get_context)):
    user = require_user(ctx.user)
    exam = _get_exam(ctx, slug)

    decision = anticheat.check_exam_access(ctx.db, user, exam, ctx.ip, ctx.fingerprint)
    if decision.denied:
        anticheat.record_denial(ctx.db, user, exam, decision)
        return templates.page(
            ctx,
            "survival/denied.html",
            status_code=403,
            nav="survival",
            exam=exam,
            decision=decision,
        )

    attempt = exam_engine.start_attempt(
        ctx.db,
        user,
        exam,
        ip=ctx.ip,
        fingerprint=ctx.fingerprint,
        user_agent=request.headers.get("user-agent"),
        screen=request.headers.get("x-nbo-screen"),
    )
    return redirect(ctx, f"/survival/attempt/{attempt.id}")


# --------------------------------------------------------------------------- #
# The player
# --------------------------------------------------------------------------- #
@router.get("/attempt/{attempt_id}")
def player(attempt_id: int, ctx: PageContext = Depends(get_context)):
    attempt = _get_attempt(ctx, attempt_id)
    exam_engine.enforce_deadline(ctx.db, attempt)

    if attempt.status is not AttemptStatus.IN_PROGRESS:
        return redirect(ctx, f"/survival/attempt/{attempt.id}/result")

    answers = exam_engine.ordered_answers(ctx.db, attempt)
    questions = []
    for index, answer in enumerate(answers, start=1):
        exam_question = answer.exam_question
        question = exam_question.question
        order = exam_engine.statement_order(attempt, exam_question.id)
        by_id = {s.id: s for s in question.statements}
        statements = [by_id[sid] for sid in order if sid in by_id] or list(question.statements)
        questions.append(
            {
                "index": index,
                "answer": answer,
                "exam_question": exam_question,
                "question": question,
                "statements": statements,
                "section": exam_question.section,
            }
        )

    return templates.page(
        ctx,
        "survival/player.html",
        nav="",
        body_class="exam-mode",
        attempt=attempt,
        exam=attempt.exam,
        questions=questions,
        progress=exam_engine.attempt_progress(attempt),
        seconds_remaining=attempt.seconds_remaining(utcnow()),
    )


@router.post("/attempt/{attempt_id}/answer")
def autosave(
    attempt_id: int,
    ctx: PageContext = Depends(get_context),
    payload: dict = Body(...),
):
    """Autosave one answer. Returns the refreshed progress counters."""
    attempt = _get_attempt(ctx, attempt_id)
    try:
        exam_engine.save_answer(
            ctx.db,
            attempt,
            exam_question_id=int(payload.get("exam_question_id")),
            response=payload.get("response") or {},
            seconds_spent=payload.get("seconds_spent"),
            flagged=payload.get("flagged"),
            ip=ctx.ip,
        )
    except ExamEngineError as exc:
        return JSONResponse(
            {"error": str(exc), "redirect": f"/survival/attempt/{attempt.id}/result"},
            status_code=409,
        )
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="bad_request")

    return {
        "ok": True,
        "progress": exam_engine.attempt_progress(attempt),
        "seconds_remaining": attempt.seconds_remaining(utcnow()),
    }


@router.post("/attempt/{attempt_id}/event")
def telemetry(
    attempt_id: int,
    ctx: PageContext = Depends(get_context),
    payload: dict = Body(...),
):
    """Proctoring signals from the player."""
    attempt = _get_attempt(ctx, attempt_id)
    if attempt.status is not AttemptStatus.IN_PROGRESS:
        return {"ok": False}
    try:
        kind = EventKind(payload.get("kind"))
    except ValueError:
        raise HTTPException(status_code=400, detail="unknown_event")
    anticheat.handle_event(ctx.db, attempt, kind, payload.get("data") or {}, ctx.ip)
    return {"ok": True}


@router.get("/attempt/{attempt_id}/state")
def state(attempt_id: int, ctx: PageContext = Depends(get_context)):
    """The clock and the saved answers, straight from the server.

    The player polls this so a tampered client clock, a sleeping laptop or a
    dropped connection all reconcile against the authoritative deadline.
    """
    attempt = _get_attempt(ctx, attempt_id)
    exam_engine.enforce_deadline(ctx.db, attempt)
    return {
        "status": attempt.status,
        "seconds_remaining": attempt.seconds_remaining(utcnow()),
        "progress": exam_engine.attempt_progress(attempt),
        "answers": {
            str(a.exam_question_id): {"response": a.response, "flagged": a.flagged_for_review}
            for a in attempt.answers
        },
    }


@router.post("/attempt/{attempt_id}/submit")
def submit(attempt_id: int, ctx: PageContext = Depends(get_context)):
    attempt = _get_attempt(ctx, attempt_id)
    exam_engine.submit_attempt(ctx.db, attempt, auto=False, ip=ctx.ip)
    return redirect(ctx, f"/survival/attempt/{attempt.id}/result")


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #
@router.get("/attempt/{attempt_id}/result")
def result(attempt_id: int, ctx: PageContext = Depends(get_context)):
    attempt = _get_attempt(ctx, attempt_id)
    exam = attempt.exam

    if attempt.status is AttemptStatus.IN_PROGRESS:
        return redirect(ctx, f"/survival/attempt/{attempt.id}")

    released = exam.results_released() or (ctx.user and ctx.user.is_staff)
    if not released:
        return templates.page(
            ctx, "survival/result_pending.html", nav="survival", attempt=attempt, exam=exam
        )

    answers = exam_engine.ordered_answers(ctx.db, attempt)
    review = []
    for index, answer in enumerate(answers, start=1):
        question = answer.exam_question.question
        order = exam_engine.statement_order(attempt, answer.exam_question_id)
        by_id = {s.id: s for s in question.statements}
        statements = [by_id[sid] for sid in order if sid in by_id] or list(question.statements)
        review.append(
            {
                "index": index,
                "answer": answer,
                "question": question,
                "statements": statements,
            }
        )

    exam_stats = stats.compute_exam_statistics(ctx.db, exam)
    topic_rows = _topic_rows_for_attempt(ctx, attempt)
    return templates.page(
        ctx,
        "survival/result.html",
        nav="survival",
        attempt=attempt,
        exam=exam,
        review=review,
        exam_stats=exam_stats,
        topic_rows=topic_rows,
        my_bucket=min(20, max(0, int(attempt.percent // 5))),
    )


def _topic_rows_for_attempt(ctx: PageContext, attempt: Attempt) -> list[dict]:
    """Per-topic accuracy for this one sitting, next to the cohort's."""
    buckets: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for answer in attempt.answers:
        question = ctx.db.get(Question, answer.question_id)
        if question is None or question.topic_id is None:
            continue
        bucket = buckets[question.topic_id]
        correctness = answer.per_statement_correct or {}
        bucket[0] += sum(1 for hit in correctness.values() if hit)
        bucket[1] += len(correctness)

    exam_stats = ctx.db.scalars(
        select(ExamStatistics).where(ExamStatistics.exam_id == attempt.exam_id)
    ).first()
    cohort = (exam_stats.topic_breakdown if exam_stats else {}) or {}

    rows = []
    for topic_id, (correct, seen) in buckets.items():
        topic = ctx.db.get(Topic, topic_id)
        if topic is None or not seen:
            continue
        rows.append(
            {
                "topic": topic,
                "name": topic.name(ctx.locale),
                "color": topic.color,
                "accuracy": correct / seen,
                "correct": correct,
                "seen": seen,
                "cohort": (cohort.get(str(topic_id)) or {}).get("accuracy"),
            }
        )
    rows.sort(key=lambda r: r["accuracy"])
    return rows


@router.get("/{slug}/standings")
def standings(slug: str, ctx: PageContext = Depends(get_context)):
    exam = _get_exam(ctx, slug)
    if not exam.results_released() and not (ctx.user and ctx.user.is_staff):
        return templates.page(
            ctx, "survival/result_pending.html", nav="survival", attempt=None, exam=exam
        )
    rows = stats.exam_leaderboard(ctx.db, exam)
    flags = {}
    if ctx.user and ctx.user.is_admin:
        for flag in ctx.db.scalars(select(CheatFlag).where(CheatFlag.exam_id == exam.id)):
            flags.setdefault(flag.attempt_id, []).append(flag)
    return templates.page(
        ctx,
        "survival/standings.html",
        nav="survival",
        exam=exam,
        rows=rows,
        flags=flags,
        exam_stats=stats.compute_exam_statistics(ctx.db, exam),
    )
