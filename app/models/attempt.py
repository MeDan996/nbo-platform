"""Attempts: one participant sitting one exam, their answers and their telemetry."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, EnumType, UtcDateTime
from app.models.enums import AttemptStatus, EventKind

if TYPE_CHECKING:
    from app.models.exam import Exam, ExamQuestion
    from app.models.user import User


class Attempt(Base):
    __tablename__ = "attempts"

    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)

    status: Mapped[AttemptStatus] = mapped_column(
        EnumType(AttemptStatus), default=AttemptStatus.IN_PROGRESS, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    # Wall-clock deadline: min(start + duration, exam close). Authoritative.
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    submitted_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    graded_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    # Frozen presentation order so a refresh never reshuffles the paper.
    # [{"eq": <exam_question_id>, "statements": [<statement_id>, ...]}, ...]
    question_order: Mapped[list] = mapped_column(JSON, default=list)

    raw_score: Mapped[float] = mapped_column(Float, default=0.0)
    max_score: Mapped[float] = mapped_column(Float, default=0.0)
    percent: Mapped[float] = mapped_column(Float, default=0.0)
    rank: Mapped[int | None] = mapped_column(Integer, index=True)
    percentile: Mapped[float | None] = mapped_column(Float)

    rating_before: Mapped[int | None] = mapped_column(Integer)
    rating_after: Mapped[int | None] = mapped_column(Integer)
    rating_delta: Mapped[int | None] = mapped_column(Integer)
    performance: Mapped[int | None] = mapped_column(Integer)

    # Session identity, captured at start and re-checked on every write.
    ip_address: Mapped[str | None] = mapped_column(String(64), index=True)
    device_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    user_agent: Mapped[str | None] = mapped_column(String(400))
    timezone_offset: Mapped[int | None] = mapped_column(Integer)
    screen: Mapped[str | None] = mapped_column(String(40))
    # Every distinct IP seen during the attempt, for the review queue.
    seen_ips: Mapped[list] = mapped_column(JSON, default=list)

    risk_score: Mapped[int] = mapped_column(Integer, default=0, index=True)
    is_flagged: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    focus_losses: Mapped[int] = mapped_column(Integer, default=0)
    void_reason: Mapped[str | None] = mapped_column(Text)

    exam: Mapped["Exam"] = relationship(back_populates="attempts")
    user: Mapped["User"] = relationship(back_populates="attempts")
    answers: Mapped[list["AttemptAnswer"]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan", lazy="selectin"
    )
    events: Mapped[list["AttemptEvent"]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan"
    )

    @property
    def is_live(self) -> bool:
        return self.status is AttemptStatus.IN_PROGRESS

    @property
    def is_scored(self) -> bool:
        return self.status in (AttemptStatus.SUBMITTED, AttemptStatus.EXPIRED, AttemptStatus.GRADED)

    def seconds_remaining(self, now: datetime) -> int:
        if not self.expires_at:
            return 0
        return max(0, int((self.expires_at - now).total_seconds()))


class AttemptAnswer(Base):
    """One participant response to one question on the paper."""

    __tablename__ = "attempt_answers"

    attempt_id: Mapped[int] = mapped_column(
        ForeignKey("attempts.id", ondelete="CASCADE"), index=True
    )
    exam_question_id: Mapped[int] = mapped_column(
        ForeignKey("exam_questions.id", ondelete="CASCADE"), index=True
    )
    question_id: Mapped[int] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), index=True
    )

    # TF_BLOCK / MCQ: {"<statement_id>": true|false|null}
    # NUMERIC: {"value": 12.5}   SHORT_TEXT / OPEN_RESPONSE: {"text": "..."}
    response: Mapped[dict] = mapped_column(JSON, default=dict)

    correct_count: Mapped[int] = mapped_column(Integer, default=0)
    statement_count: Mapped[int] = mapped_column(Integer, default=0)
    awarded_points: Mapped[float] = mapped_column(Float, default=0.0)
    max_points: Mapped[float] = mapped_column(Float, default=1.0)
    is_fully_correct: Mapped[bool] = mapped_column(Boolean, default=False)
    # Which statements the participant got right, for statement-level analytics.
    # {"<statement_id>": true|false}
    per_statement_correct: Mapped[dict] = mapped_column(JSON, default=dict)

    seconds_spent: Mapped[int] = mapped_column(Integer, default=0)
    revision_count: Mapped[int] = mapped_column(Integer, default=0)
    flagged_for_review: Mapped[bool] = mapped_column(Boolean, default=False)  # by the participant
    answered_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    # Set by a human grader for OPEN_RESPONSE.
    grader_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    grader_note: Mapped[str | None] = mapped_column(Text)

    attempt: Mapped[Attempt] = relationship(back_populates="answers")
    exam_question: Mapped["ExamQuestion"] = relationship()

    @property
    def is_answered(self) -> bool:
        if not self.response:
            return False
        if "text" in self.response:
            return bool(str(self.response.get("text") or "").strip())
        if "value" in self.response:
            return self.response.get("value") is not None
        return any(v is not None for v in self.response.values())


class AttemptEvent(Base):
    """Proctoring telemetry. Append-only; never rewritten after the fact."""

    __tablename__ = "attempt_events"

    attempt_id: Mapped[int] = mapped_column(
        ForeignKey("attempts.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[EventKind] = mapped_column(EnumType(EventKind), index=True)
    at: Mapped[datetime | None] = mapped_column(UtcDateTime, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    ip_address: Mapped[str | None] = mapped_column(String(64))

    attempt: Mapped[Attempt] = relationship(back_populates="events")
