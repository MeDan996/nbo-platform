"""Performance analytics.

Two audiences:

* **Participants** want to know where they are weak. The unit that matters for
  that is the *statement*, not the question: a four-statement IBO block scored
  0.2 tells you almost nothing, but "you get plant water transport statements
  right 41% of the time, against a cohort average of 68%" is actionable.
* **Organisers** want item analysis - which questions discriminated between
  strong and weak candidates, and which were simply broken.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models.analytics import DailyActivity, ExamStatistics, RatingChange, TopicMastery
from app.models.attempt import Attempt, AttemptAnswer
from app.models.content import Question, Topic
from app.models.enums import AttemptStatus, ExamMode
from app.models.exam import Exam, ExamQuestion
from app.models.user import User

SCORED_STATUSES = (AttemptStatus.SUBMITTED, AttemptStatus.EXPIRED, AttemptStatus.GRADED)
# Weight given to the newest sitting when updating the rolling accuracy figure.
EMA_ALPHA = 0.35


# --------------------------------------------------------------------------- #
# Ingestion: fold a finished attempt into the participant's rolling stats
# --------------------------------------------------------------------------- #
def update_mastery_from_attempt(db: Session, attempt: Attempt) -> None:
    """Fold one graded attempt into the participant's per-topic mastery rows."""
    per_topic: dict[int, dict] = defaultdict(
        lambda: {"seen": 0, "stmts": 0, "correct": 0, "earned": 0.0, "possible": 0.0, "secs": 0}
    )

    for answer in attempt.answers:
        question = db.get(Question, answer.question_id)
        if question is None or question.topic_id is None:
            continue
        bucket = per_topic[question.topic_id]
        bucket["seen"] += 1
        correctness = answer.per_statement_correct or {}
        bucket["stmts"] += len(correctness) or answer.statement_count
        bucket["correct"] += sum(1 for hit in correctness.values() if hit)
        bucket["earned"] += answer.awarded_points
        bucket["possible"] += answer.max_points
        bucket["secs"] += answer.seconds_spent

    now = utcnow()
    for topic_id, bucket in per_topic.items():
        row = db.scalars(
            select(TopicMastery).where(
                TopicMastery.user_id == attempt.user_id, TopicMastery.topic_id == topic_id
            )
        ).first()
        if row is None:
            row = TopicMastery(user_id=attempt.user_id, topic_id=topic_id)
            db.add(row)
            db.flush()

        row.questions_seen += bucket["seen"]
        row.statements_seen += bucket["stmts"]
        row.statements_correct += bucket["correct"]
        row.points_earned += bucket["earned"]
        row.points_possible += bucket["possible"]
        row.accuracy = (
            row.statements_correct / row.statements_seen if row.statements_seen else 0.0
        )

        session_accuracy = bucket["correct"] / bucket["stmts"] if bucket["stmts"] else 0.0
        row.recent_accuracy = (
            session_accuracy
            if row.recent_accuracy == 0.0
            else EMA_ALPHA * session_accuracy + (1 - EMA_ALPHA) * row.recent_accuracy
        )
        if bucket["seen"]:
            row.avg_seconds = (
                (row.avg_seconds * (row.questions_seen - bucket["seen"]) + bucket["secs"])
                / row.questions_seen
            )
        row.last_practiced_at = now

    _bump_daily_activity(db, attempt)


def _bump_daily_activity(db: Session, attempt: Attempt) -> None:
    day = (attempt.submitted_at or utcnow()).date()
    row = db.scalars(
        select(DailyActivity).where(
            DailyActivity.user_id == attempt.user_id, DailyActivity.day == day
        )
    ).first()
    if row is None:
        row = DailyActivity(user_id=attempt.user_id, day=day)
        db.add(row)
        db.flush()
    row.questions_answered += sum(1 for a in attempt.answers if a.is_answered)
    row.points_earned += attempt.raw_score
    if attempt.started_at and attempt.submitted_at:
        row.seconds_active += int((attempt.submitted_at - attempt.started_at).total_seconds())

    _refresh_streak(db, attempt.user_id)


def _refresh_streak(db: Session, user_id: int) -> None:
    """Consecutive days ending today (or yesterday) with any activity."""
    user = db.get(User, user_id)
    if user is None:
        return
    days = set(
        db.scalars(
            select(DailyActivity.day)
            .where(DailyActivity.user_id == user_id)
            .order_by(DailyActivity.day.desc())
            .limit(400)
        )
    )
    if not days:
        user.streak_days = 0
        return

    today = utcnow().date()
    cursor = today if today in days else today - timedelta(days=1)
    streak = 0
    while cursor in days:
        streak += 1
        cursor -= timedelta(days=1)
    user.streak_days = streak
    user.last_active_date = utcnow()


# --------------------------------------------------------------------------- #
# Participant-facing views
# --------------------------------------------------------------------------- #
@dataclass
class TopicInsight:
    topic_id: int
    slug: str
    name: str
    color: str
    accuracy: float
    recent_accuracy: float
    cohort_accuracy: float | None
    statements_seen: int
    questions_seen: int
    avg_seconds: float

    @property
    def delta(self) -> float | None:
        if self.cohort_accuracy is None:
            return None
        return round(self.accuracy - self.cohort_accuracy, 4)

    @property
    def trend(self) -> float:
        return round(self.recent_accuracy - self.accuracy, 4)


def cohort_topic_accuracy(db: Session) -> dict[int, float]:
    """Platform-wide accuracy per topic, used as the comparison baseline."""
    rows = db.execute(
        select(
            TopicMastery.topic_id,
            func.sum(TopicMastery.statements_correct),
            func.sum(TopicMastery.statements_seen),
        ).group_by(TopicMastery.topic_id)
    ).all()
    baseline: dict[int, float] = {}
    for topic_id, correct, seen in rows:
        seen = float(seen or 0)
        if seen > 0:
            baseline[topic_id] = float(correct or 0) / seen
    return baseline


def topic_insights(db: Session, user: User, locale: str = "ru") -> list[TopicInsight]:
    cohort = cohort_topic_accuracy(db)
    rows = list(
        db.scalars(select(TopicMastery).where(TopicMastery.user_id == user.id))
    )
    insights: list[TopicInsight] = []
    for row in rows:
        topic = row.topic or db.get(Topic, row.topic_id)
        if topic is None:
            continue
        insights.append(
            TopicInsight(
                topic_id=row.topic_id,
                slug=topic.slug,
                name=topic.name(locale),
                color=topic.color,
                accuracy=round(row.accuracy, 4),
                recent_accuracy=round(row.recent_accuracy, 4),
                cohort_accuracy=round(cohort.get(row.topic_id), 4)
                if row.topic_id in cohort
                else None,
                statements_seen=row.statements_seen,
                questions_seen=row.questions_seen,
                avg_seconds=round(row.avg_seconds, 1),
            )
        )
    insights.sort(key=lambda i: i.accuracy)
    return insights


def strengths_and_weaknesses(
    insights: list[TopicInsight], min_statements: int = 8, count: int = 3
) -> tuple[list[TopicInsight], list[TopicInsight]]:
    """Split topics into what to celebrate and what to work on.

    Topics with too little data are excluded from both: telling a student they
    are weak at ethology on the strength of two statements is noise, not insight.
    """
    eligible = [i for i in insights if i.statements_seen >= min_statements]
    weakest = eligible[:count]
    strongest = list(reversed(eligible[-count:])) if eligible else []
    return strongest, weakest


@dataclass
class ParticipantSummary:
    attempts: int = 0
    rated_attempts: int = 0
    questions_answered: int = 0
    statements_seen: int = 0
    statements_correct: int = 0
    total_points: float = 0.0
    possible_points: float = 0.0
    best_percent: float = 0.0
    avg_percent: float = 0.0
    best_rank: int | None = None
    rating: int = 1200
    peak_rating: int = 1200
    streak_days: int = 0
    rating_history: list[dict] = field(default_factory=list)
    percent_history: list[dict] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.statements_correct / self.statements_seen if self.statements_seen else 0.0


def participant_summary(db: Session, user: User) -> ParticipantSummary:
    summary = ParticipantSummary(
        rating=user.rating, peak_rating=user.peak_rating, streak_days=user.streak_days
    )

    attempts = list(
        db.scalars(
            select(Attempt)
            .where(Attempt.user_id == user.id, Attempt.status.in_(SCORED_STATUSES))
            .order_by(Attempt.submitted_at)
        )
    )
    summary.attempts = len(attempts)
    percents = []
    for attempt in attempts:
        summary.total_points += attempt.raw_score
        summary.possible_points += attempt.max_score
        percents.append(attempt.percent)
        if attempt.rank is not None:
            summary.best_rank = (
                attempt.rank if summary.best_rank is None else min(summary.best_rank, attempt.rank)
            )
        summary.percent_history.append(
            {
                "at": attempt.submitted_at.isoformat() if attempt.submitted_at else None,
                "percent": round(attempt.percent, 2),
                "exam": attempt.exam.title if attempt.exam else "",
            }
        )

    if percents:
        summary.best_percent = round(max(percents), 2)
        summary.avg_percent = round(statistics.fmean(percents), 2)

    mastery = list(db.scalars(select(TopicMastery).where(TopicMastery.user_id == user.id)))
    summary.statements_seen = sum(m.statements_seen for m in mastery)
    summary.statements_correct = sum(m.statements_correct for m in mastery)
    summary.questions_answered = sum(m.questions_seen for m in mastery)

    changes = list(
        db.scalars(
            select(RatingChange)
            .where(RatingChange.user_id == user.id)
            .order_by(RatingChange.at)
        )
    )
    summary.rated_attempts = len(changes)
    summary.rating_history = [
        {
            "at": c.at.isoformat() if c.at else None,
            "rating": c.rating_after,
            "delta": c.delta,
            "rank": c.rank,
            "participants": c.participants,
            "exam_id": c.exam_id,
        }
        for c in changes
    ]
    return summary


def activity_calendar(db: Session, user: User, days: int = 119) -> list[dict]:
    """A Khan-Academy-style contribution grid for the last ~17 weeks."""
    today = utcnow().date()
    start = today - timedelta(days=days)
    rows = {
        row.day: row
        for row in db.scalars(
            select(DailyActivity).where(
                DailyActivity.user_id == user.id, DailyActivity.day >= start
            )
        )
    }
    out = []
    for offset in range(days + 1):
        day = start + timedelta(days=offset)
        row = rows.get(day)
        count = row.questions_answered if row else 0
        out.append(
            {
                "date": day.isoformat(),
                "count": count,
                "level": 0 if count == 0 else min(4, 1 + count // 8),
            }
        )
    return out


def weakest_statements(db: Session, user: User, limit: int = 10) -> list[dict]:
    """Individual statements the participant keeps getting wrong, newest first.
    Gives the review page something concrete to link to."""
    answers = db.scalars(
        select(AttemptAnswer)
        .join(Attempt, Attempt.id == AttemptAnswer.attempt_id)
        .where(Attempt.user_id == user.id, Attempt.status.in_(SCORED_STATUSES))
        .order_by(AttemptAnswer.id.desc())
        .limit(400)
    )
    misses: dict[int, dict] = {}
    for answer in answers:
        wrong = sum(1 for hit in (answer.per_statement_correct or {}).values() if not hit)
        if not wrong:
            continue
        entry = misses.setdefault(
            answer.question_id,
            {"question_id": answer.question_id, "wrong": 0, "seen": 0, "attempt_id": answer.attempt_id},
        )
        entry["wrong"] += wrong
        entry["seen"] += len(answer.per_statement_correct or {})
    ranked = sorted(misses.values(), key=lambda e: -e["wrong"])[:limit]
    for entry in ranked:
        entry["accuracy"] = round(1 - entry["wrong"] / entry["seen"], 3) if entry["seen"] else 0.0
    return ranked


# --------------------------------------------------------------------------- #
# Exam-level item analysis
# --------------------------------------------------------------------------- #
def compute_exam_statistics(db: Session, exam: Exam) -> ExamStatistics:
    """Cohort summary plus classical item analysis, cached on the exam."""
    attempts = list(
        db.scalars(
            select(Attempt).where(
                Attempt.exam_id == exam.id, Attempt.status.in_(SCORED_STATUSES)
            )
        )
    )
    row = db.scalars(select(ExamStatistics).where(ExamStatistics.exam_id == exam.id)).first()
    if row is None:
        row = ExamStatistics(exam_id=exam.id)
        db.add(row)
        db.flush()

    row.computed_at = utcnow()
    row.participants = len(attempts)
    if not attempts:
        row.histogram = [0] * 21
        row.topic_breakdown = {}
        row.question_stats = {}
        return row

    percents = [a.percent for a in attempts]
    scores = [a.raw_score for a in attempts]
    row.mean_score = round(statistics.fmean(scores), 3)
    row.median_score = round(statistics.median(scores), 3)
    row.stdev_score = round(statistics.pstdev(scores), 3) if len(scores) > 1 else 0.0
    row.max_score = round(max(scores), 3)
    row.min_score = round(min(scores), 3)

    histogram = [0] * 21
    for percent in percents:
        histogram[min(20, max(0, int(percent // 5)))] += 1
    row.histogram = histogram

    # Item analysis. `p` is the mean fraction of available marks; `d` is the
    # point-biserial correlation between scoring on this item and scoring
    # overall, which is the standard measure of whether an item separates strong
    # candidates from weak ones. Negative `d` means the item is misbehaving.
    totals = {a.id: a.raw_score for a in attempts}
    total_values = list(totals.values())
    total_sd = statistics.pstdev(total_values) if len(total_values) > 1 else 0.0
    total_mean = statistics.fmean(total_values)

    by_question: dict[int, list[tuple[float, float]]] = defaultdict(list)
    topic_hits: dict[int, list[int]] = defaultdict(lambda: [0, 0])  # [correct, seen]

    for attempt in attempts:
        for answer in attempt.answers:
            fraction = answer.awarded_points / answer.max_points if answer.max_points else 0.0
            by_question[answer.question_id].append((fraction, totals[attempt.id]))
            question = db.get(Question, answer.question_id)
            if question and question.topic_id:
                bucket = topic_hits[question.topic_id]
                correctness = answer.per_statement_correct or {}
                bucket[0] += sum(1 for hit in correctness.values() if hit)
                bucket[1] += len(correctness)

    question_stats: dict[str, dict] = {}
    for question_id, pairs in by_question.items():
        fractions = [p for p, _ in pairs]
        p_value = round(statistics.fmean(fractions), 4)
        discrimination = None
        if total_sd > 0 and len(pairs) > 2:
            item_sd = statistics.pstdev(fractions)
            if item_sd > 0:
                item_mean = statistics.fmean(fractions)
                covariance = statistics.fmean(
                    [(f - item_mean) * (t - total_mean) for f, t in pairs]
                )
                discrimination = round(covariance / (item_sd * total_sd), 4)
        question_stats[str(question_id)] = {
            "p": p_value,
            "d": discrimination,
            "n": len(pairs),
        }
        question = db.get(Question, question_id)
        if question is not None:
            question.p_value = p_value
            question.discrimination = discrimination
            question.times_used = len(pairs)

    row.question_stats = question_stats
    row.topic_breakdown = {
        str(topic_id): {"accuracy": round(correct / seen, 4) if seen else 0.0, "n": seen}
        for topic_id, (correct, seen) in topic_hits.items()
    }
    return row


def rank_attempts(db: Session, exam: Exam) -> list[Attempt]:
    """Assign rank and percentile to every scored attempt on an exam.

    Ties share the best rank. Percentile is the fraction of the field a
    participant finished at or above.
    """
    attempts = list(
        db.scalars(
            select(Attempt).where(
                Attempt.exam_id == exam.id, Attempt.status.in_(SCORED_STATUSES)
            )
        )
    )
    if not attempts:
        return []

    attempts.sort(key=lambda a: (-a.raw_score, a.submitted_at or utcnow()))
    total = len(attempts)
    rank = 0
    previous_score = None
    for index, attempt in enumerate(attempts, start=1):
        if attempt.raw_score != previous_score:
            rank = index
            previous_score = attempt.raw_score
        attempt.rank = rank
        attempt.percentile = round((total - rank + 1) / total * 100, 2)
    return attempts


def leaderboard(db: Session, limit: int = 100, school_id: int | None = None) -> list[User]:
    stmt = (
        select(User)
        .where(User.is_active.is_(True), User.is_banned.is_(False), User.rated_contests > 0)
        .order_by(User.rating.desc(), User.peak_rating.desc())
        .limit(limit)
    )
    if school_id:
        stmt = stmt.where(User.school_id == school_id)
    return list(db.scalars(stmt))


def exam_leaderboard(db: Session, exam: Exam, limit: int = 200) -> list[Attempt]:
    return list(
        db.scalars(
            select(Attempt)
            .where(Attempt.exam_id == exam.id, Attempt.status.in_(SCORED_STATUSES))
            .order_by(Attempt.raw_score.desc(), Attempt.submitted_at)
            .limit(limit)
        )
    )


def global_platform_stats(db: Session) -> dict:
    """Numbers for the public landing page."""
    return {
        "participants": int(
            db.scalar(select(func.count(User.id)).where(User.is_active.is_(True))) or 0
        ),
        "questions": int(db.scalar(select(func.count(Question.id))) or 0),
        "exams": int(db.scalar(select(func.count(Exam.id))) or 0),
        "attempts": int(
            db.scalar(
                select(func.count(Attempt.id)).where(Attempt.status.in_(SCORED_STATUSES))
            )
            or 0
        ),
    }
