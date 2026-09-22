"""The participant-facing statistics section."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from app.models.analytics import RatingChange, TopicMastery
from app.models.attempt import Attempt
from app.models.content import Question, Topic
from app.models.enums import AttemptStatus
from app.models.exam import Exam
from app.services import stats
from app.services.auth import require_user
from app.templating import PageContext, get_context, templates

router = APIRouter(prefix="/stats", tags=["stats"])

SCORED = (AttemptStatus.SUBMITTED, AttemptStatus.EXPIRED, AttemptStatus.GRADED)


@router.get("")
def my_stats(ctx: PageContext = Depends(get_context)):
    user = require_user(ctx.user)
    summary = stats.participant_summary(ctx.db, user)
    insights = stats.topic_insights(ctx.db, user, ctx.locale)
    strongest, weakest = stats.strengths_and_weaknesses(insights)

    attempts = list(
        ctx.db.scalars(
            select(Attempt)
            .where(Attempt.user_id == user.id, Attempt.status.in_(SCORED))
            .order_by(Attempt.submitted_at.desc())
            .limit(25)
        )
    )
    review = stats.weakest_statements(ctx.db, user)
    for entry in review:
        question = ctx.db.get(Question, entry["question_id"])
        entry["question"] = question
        entry["topic"] = (
            question.topic.name(ctx.locale) if question and question.topic else ""
        )

    return templates.page(
        ctx,
        "stats/index.html",
        nav="stats",
        summary=summary,
        insights=insights,
        strongest=strongest,
        weakest=weakest,
        attempts=attempts,
        calendar=stats.activity_calendar(ctx.db, user),
        review=review,
    )


@router.get("/topic/{slug}")
def topic_detail(slug: str, ctx: PageContext = Depends(get_context)):
    user = require_user(ctx.user)
    topic = ctx.db.scalars(select(Topic).where(Topic.slug == slug)).first()
    if topic is None:
        raise HTTPException(status_code=404, detail="common.not_found")

    mastery = ctx.db.scalars(
        select(TopicMastery).where(
            TopicMastery.user_id == user.id, TopicMastery.topic_id == topic.id
        )
    ).first()
    cohort = stats.cohort_topic_accuracy(ctx.db).get(topic.id)

    # Every question in this topic the participant has answered, worst first.
    rows = []
    answers = ctx.db.scalars(
        select(Attempt)
        .where(Attempt.user_id == user.id, Attempt.status.in_(SCORED))
        .order_by(Attempt.submitted_at.desc())
        .limit(50)
    )
    for attempt in answers:
        for answer in attempt.answers:
            question = ctx.db.get(Question, answer.question_id)
            if question is None or question.topic_id != topic.id:
                continue
            correctness = answer.per_statement_correct or {}
            if not correctness:
                continue
            rows.append(
                {
                    "question": question,
                    "attempt": attempt,
                    "correct": sum(1 for hit in correctness.values() if hit),
                    "total": len(correctness),
                    "points": answer.awarded_points,
                    "max_points": answer.max_points,
                    "seconds": answer.seconds_spent,
                }
            )
    rows.sort(key=lambda r: (r["correct"] / r["total"]) if r["total"] else 0)

    return templates.page(
        ctx,
        "stats/topic.html",
        nav="stats",
        topic=topic,
        topic_name=topic.name(ctx.locale),
        mastery=mastery,
        cohort=cohort,
        rows=rows[:40],
    )


@router.get("/rating")
def rating_history(ctx: PageContext = Depends(get_context)):
    user = require_user(ctx.user)
    changes = list(
        ctx.db.scalars(
            select(RatingChange)
            .where(RatingChange.user_id == user.id)
            .order_by(RatingChange.at)
        )
    )
    exams = {c.exam_id: ctx.db.get(Exam, c.exam_id) for c in changes}
    return templates.page(
        ctx,
        "stats/rating.html",
        nav="stats",
        changes=list(reversed(changes)),
        series=changes,
        exams=exams,
    )
