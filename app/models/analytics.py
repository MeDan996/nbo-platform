"""Derived performance data: rating history and per-topic mastery."""
from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Date,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, UtcDateTime

if TYPE_CHECKING:
    from app.models.content import Topic
    from app.models.user import User


class RatingChange(Base):
    """One rated sitting's effect on a participant's rating."""

    __tablename__ = "rating_changes"
    __table_args__ = (UniqueConstraint("user_id", "attempt_id", name="uq_rating_change_attempt"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id", ondelete="CASCADE"), index=True)
    attempt_id: Mapped[int] = mapped_column(ForeignKey("attempts.id", ondelete="CASCADE"))

    rating_before: Mapped[int] = mapped_column(Integer)
    rating_after: Mapped[int] = mapped_column(Integer)
    delta: Mapped[int] = mapped_column(Integer)
    rank: Mapped[int] = mapped_column(Integer)
    participants: Mapped[int] = mapped_column(Integer)
    seed: Mapped[float | None] = mapped_column(Float)
    performance: Mapped[int | None] = mapped_column(Integer)
    at: Mapped[datetime | None] = mapped_column(UtcDateTime, index=True)

    user: Mapped["User"] = relationship()


class TopicMastery(Base):
    """Rolling per-topic performance, the backbone of the strengths/weaknesses view.

    `accuracy` is the lifetime figure; `recent_accuracy` is an exponential moving
    average that reacts to the last few sittings, so improvement shows up quickly.
    """

    __tablename__ = "topic_mastery"
    __table_args__ = (UniqueConstraint("user_id", "topic_id", name="uq_user_topic_mastery"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"), index=True)

    questions_seen: Mapped[int] = mapped_column(Integer, default=0)
    statements_seen: Mapped[int] = mapped_column(Integer, default=0)
    statements_correct: Mapped[int] = mapped_column(Integer, default=0)
    points_earned: Mapped[float] = mapped_column(Float, default=0.0)
    points_possible: Mapped[float] = mapped_column(Float, default=0.0)

    accuracy: Mapped[float] = mapped_column(Float, default=0.0)
    recent_accuracy: Mapped[float] = mapped_column(Float, default=0.0)
    avg_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    # Same figure averaged across every participant, so the UI can say
    # "you are 12 points below the cohort here".
    cohort_accuracy: Mapped[float | None] = mapped_column(Float)
    last_practiced_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    user: Mapped["User"] = relationship()
    topic: Mapped["Topic"] = relationship(lazy="selectin")

    @property
    def delta_vs_cohort(self) -> float | None:
        if self.cohort_accuracy is None:
            return None
        return self.accuracy - self.cohort_accuracy


class DailyActivity(Base):
    """Khan-Academy-style practice streak and daily volume."""

    __tablename__ = "daily_activity"
    __table_args__ = (UniqueConstraint("user_id", "day", name="uq_user_day"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    questions_answered: Mapped[int] = mapped_column(Integer, default=0)
    seconds_active: Mapped[int] = mapped_column(Integer, default=0)
    points_earned: Mapped[float] = mapped_column(Float, default=0.0)


class ExamStatistics(Base):
    """Cohort summary computed once an exam closes, cached for fast leaderboards."""

    __tablename__ = "exam_statistics"

    exam_id: Mapped[int] = mapped_column(
        ForeignKey("exams.id", ondelete="CASCADE"), unique=True, index=True
    )
    participants: Mapped[int] = mapped_column(Integer, default=0)
    mean_score: Mapped[float] = mapped_column(Float, default=0.0)
    median_score: Mapped[float] = mapped_column(Float, default=0.0)
    stdev_score: Mapped[float] = mapped_column(Float, default=0.0)
    max_score: Mapped[float] = mapped_column(Float, default=0.0)
    min_score: Mapped[float] = mapped_column(Float, default=0.0)
    # Percent-score histogram in 5-point buckets: [count, count, ...] length 21.
    histogram: Mapped[list] = mapped_column(JSON, default=list)
    # {"<topic_id>": {"accuracy": 0.63, "n": 120}}
    topic_breakdown: Mapped[dict] = mapped_column(JSON, default=dict)
    # {"<question_id>": {"p": 0.41, "d": 0.32}}
    question_stats: Mapped[dict] = mapped_column(JSON, default=dict)
    computed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    notes: Mapped[str | None] = mapped_column(String(400))
