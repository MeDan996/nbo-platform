"""Landing page, leaderboard and public profiles."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select

from app.models.enums import ExamStatus
from app.models.exam import Exam
from app.models.user import School, User
from app.services import stats
from app.templating import PageContext, get_context, templates

router = APIRouter()


@router.get("/")
def landing(ctx: PageContext = Depends(get_context)):
    if ctx.user is not None:
        return dashboard(ctx)
    open_exams = list(
        ctx.db.scalars(
            select(Exam)
            .where(Exam.status.in_([ExamStatus.OPEN, ExamStatus.SCHEDULED]))
            .order_by(Exam.opens_at)
            .limit(3)
        )
    )
    return templates.page(
        ctx,
        "landing.html",
        nav="home",
        platform=stats.global_platform_stats(ctx.db),
        open_exams=open_exams,
    )


def dashboard(ctx: PageContext):
    """Signed-in home: what to do next, plus a glance at progress."""
    user = ctx.user
    summary = stats.participant_summary(ctx.db, user)
    insights = stats.topic_insights(ctx.db, user, ctx.locale)
    strongest, weakest = stats.strengths_and_weaknesses(insights)

    live = list(
        ctx.db.scalars(
            select(Exam)
            .where(Exam.status.in_([ExamStatus.OPEN, ExamStatus.SCHEDULED]))
            .order_by(Exam.opens_at)
            .limit(4)
        )
    )
    return templates.page(
        ctx,
        "dashboard.html",
        nav="home",
        summary=summary,
        insights=insights,
        strongest=strongest,
        weakest=weakest,
        open_exams=[e for e in live if e.is_open_at()],
        upcoming=[e for e in live if not e.is_open_at()],
        calendar=stats.activity_calendar(ctx.db, user),
    )


@router.get("/leaderboard")
def leaderboard(
    ctx: PageContext = Depends(get_context),
    school_id: int | None = Query(default=None),
):
    users = stats.leaderboard(ctx.db, limit=100, school_id=school_id)
    schools = list(ctx.db.scalars(select(School).order_by(School.name)))
    my_position = None
    if ctx.user is not None:
        my_position = next(
            (i for i, u in enumerate(users, start=1) if u.id == ctx.user.id), None
        )
    return templates.page(
        ctx,
        "leaderboard.html",
        nav="leaderboard",
        users=users,
        schools=schools,
        school_id=school_id,
        my_position=my_position,
    )


@router.get("/u/{username}")
def profile(username: str, ctx: PageContext = Depends(get_context)):
    target = ctx.db.scalars(select(User).where(User.username == username)).first()
    if target is None or not target.is_active:
        raise HTTPException(status_code=404, detail="common.not_found")

    summary = stats.participant_summary(ctx.db, target)
    insights = stats.topic_insights(ctx.db, target, ctx.locale)
    strongest, weakest = stats.strengths_and_weaknesses(insights)
    return templates.page(
        ctx,
        "profile.html",
        nav="",
        profile_user=target,
        summary=summary,
        insights=insights,
        strongest=strongest,
        weakest=weakest,
        calendar=stats.activity_calendar(ctx.db, target),
        is_self=ctx.user is not None and ctx.user.id == target.id,
    )
