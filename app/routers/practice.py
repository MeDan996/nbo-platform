"""Practice mode: short, unrated, adaptive sets drawn from the question bank.

A practice session materialises as a real `Exam` row in PRACTICE mode, owned by
the participant. That is deliberate: it means practice reuses the exact same
attempt, autosave, grading and analytics machinery as a graded sitting, so there
is only one exam runtime to reason about and to keep correct.
"""
from __future__ import annotations

import random

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from app.db import utcnow
from app.models.analytics import TopicMastery
from app.models.attempt import Attempt, AttemptAnswer
from app.models.content import Question, Topic
from app.models.enums import AttemptStatus, Difficulty, ExamMode, ExamStatus, QuestionStatus
from app.models.exam import Exam, ExamQuestion, ExamSection
from app.responses import redirect
from app.services import exam_engine
from app.services.slugs import unique_exam_slug
from app.services.auth import require_user
from app.templating import PageContext, get_context, templates

router = APIRouter(prefix="/practice", tags=["practice"])

SET_SIZES = (5, 10, 20)


@router.get("")
def practice_home(ctx: PageContext = Depends(get_context)):
    topics = list(
        ctx.db.scalars(
            select(Topic).where(Topic.parent_id.is_(None)).order_by(Topic.order, Topic.id)
        )
    )
    counts = dict(
        ctx.db.execute(
            select(Question.topic_id, func.count(Question.id))
            .where(Question.status == QuestionStatus.APPROVED)
            .group_by(Question.topic_id)
        ).all()
    )
    mastery = {}
    recent: list[Attempt] = []
    if ctx.user is not None:
        mastery = {
            row.topic_id: row
            for row in ctx.db.scalars(
                select(TopicMastery).where(TopicMastery.user_id == ctx.user.id)
            )
        }
        recent = list(
            ctx.db.scalars(
                select(Attempt)
                .join(Exam, Exam.id == Attempt.exam_id)
                .where(Attempt.user_id == ctx.user.id, Exam.mode == ExamMode.PRACTICE)
                .order_by(Attempt.id.desc())
                .limit(8)
            )
        )
    return templates.page(
        ctx,
        "practice/home.html",
        nav="practice",
        topics=topics,
        counts=counts,
        mastery=mastery,
        recent=recent,
        set_sizes=SET_SIZES,
    )


def _select_questions(
    ctx: PageContext, topic: Topic | None, count: int, user_id: int
) -> list[Question]:
    """Pick a practice set.

    Questions the participant has never seen come first. Beyond that the set is
    weighted towards the difficulty band just above their current accuracy on
    the topic, which keeps practice challenging without being demoralising.
    """
    stmt = select(Question).where(Question.status == QuestionStatus.APPROVED)
    if topic is not None:
        topic_ids = [topic.id] + [child.id for child in topic.children]
        stmt = stmt.where(Question.topic_id.in_(topic_ids))
    pool = list(ctx.db.scalars(stmt))
    if not pool:
        return []

    seen_ids = set(
        ctx.db.scalars(
            select(AttemptAnswer.question_id)
            .join(Attempt, Attempt.id == AttemptAnswer.attempt_id)
            .where(Attempt.user_id == user_id, Attempt.status.in_(
                [AttemptStatus.SUBMITTED, AttemptStatus.EXPIRED, AttemptStatus.GRADED]
            ))
        )
    )

    accuracy = 0.5
    if topic is not None:
        row = ctx.db.scalars(
            select(TopicMastery).where(
                TopicMastery.user_id == user_id, TopicMastery.topic_id == topic.id
            )
        ).first()
        if row is not None and row.statements_seen >= 8:
            accuracy = row.recent_accuracy or row.accuracy

    target = _target_difficulty(accuracy)
    rng = random.Random()

    def weight(question: Question) -> float:
        distance = abs(Difficulty(question.difficulty).weight - target)
        score = 1.0 / (1.0 + distance * 2)
        if question.id not in seen_ids:
            score *= 2.5  # strongly prefer unseen material
        return score * rng.uniform(0.75, 1.25)

    pool.sort(key=weight, reverse=True)
    return pool[:count]


def _target_difficulty(accuracy: float) -> float:
    if accuracy >= 0.85:
        return Difficulty.OLYMPIAD.weight
    if accuracy >= 0.7:
        return Difficulty.HARD.weight
    if accuracy >= 0.5:
        return Difficulty.MEDIUM.weight
    return Difficulty.EASY.weight


@router.post("/start")
def start_practice(
    ctx: PageContext = Depends(get_context),
    topic_slug: str = Form(""),
    count: int = Form(10),
):
    user = require_user(ctx.user)
    count = max(3, min(50, count))

    topic = None
    if topic_slug:
        topic = ctx.db.scalars(select(Topic).where(Topic.slug == topic_slug)).first()
        if topic is None:
            raise HTTPException(status_code=404, detail="common.not_found")

    questions = _select_questions(ctx, topic, count, user.id)
    if not questions:
        return templates.page(
            ctx, "practice/empty.html", nav="practice", topic=topic, status_code=200
        )

    now = utcnow()
    label = topic.name(ctx.locale) if topic else "Mixed"
    exam = Exam(
        slug=unique_exam_slug(ctx.db, f"practice-{user.id}-{int(now.timestamp())}"),
        title=f"Practice: {label}",
        mode=ExamMode.PRACTICE,
        status=ExamStatus.OPEN,
        owner_id=user.id,
        # Generous but finite: practice should not be a timed ordeal, and an
        # abandoned session still needs a deadline so it can be swept up.
        duration_minutes=max(15, len(questions) * 4),
        max_attempts=1,
        is_rated=False,
        show_feedback_immediately=True,
        allow_backtracking=True,
    )
    ctx.db.add(exam)
    ctx.db.flush()

    section = ExamSection(exam_id=exam.id, title=label, order=0, topic_id=topic.id if topic else None)
    ctx.db.add(section)
    ctx.db.flush()

    for order, question in enumerate(questions):
        ctx.db.add(
            ExamQuestion(
                exam_id=exam.id,
                section_id=section.id,
                question_id=question.id,
                order=order,
                points=question.max_points,
            )
        )
    ctx.db.flush()
    ctx.db.refresh(exam)

    attempt = exam_engine.start_attempt(
        ctx.db, user, exam, ip=ctx.ip, fingerprint=ctx.fingerprint
    )
    return redirect(ctx, f"/survival/attempt/{attempt.id}")
