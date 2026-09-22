"""Exams: the assembled paper, its sections, and who may sit it."""
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
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, EnumType, UtcDateTime, utcnow
from app.models.enums import ExamMode, ExamStatus

if TYPE_CHECKING:
    from app.models.attempt import Attempt
    from app.models.content import Question, Topic
    from app.models.user import User


# Defaults mirror the real IBO paper: a shared exam window, one attempt,
# results withheld until the window closes.
DEFAULT_ANTICHEAT_POLICY: dict = {
    "block_duplicate_ip": True,
    "block_duplicate_device": True,
    "ip_account_threshold": 3,
    "require_fullscreen": False,
    "track_focus_loss": True,
    "max_focus_losses": 5,
    "block_copy_paste": True,
    "lock_after_submit": True,
    "detect_collusion": True,
    "void_on_critical_flag": False,
}


class Exam(Base):
    __tablename__ = "exams"

    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(300))
    subtitle: Mapped[str | None] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    # Translations of the shell copy; question text carries its own translations.
    title_i18n: Mapped[dict] = mapped_column(JSON, default=dict)

    mode: Mapped[ExamMode] = mapped_column(EnumType(ExamMode), default=ExamMode.PRACTICE, index=True)
    status: Mapped[ExamStatus] = mapped_column(EnumType(ExamStatus), default=ExamStatus.DRAFT, index=True)

    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    owner: Mapped["User | None"] = relationship()

    duration_minutes: Mapped[int] = mapped_column(Integer, default=195)  # IBO Part A: 3h15
    opens_at: Mapped[datetime | None] = mapped_column(UtcDateTime, index=True)
    closes_at: Mapped[datetime | None] = mapped_column(UtcDateTime, index=True)
    results_visible_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    max_attempts: Mapped[int] = mapped_column(Integer, default=1)
    is_rated: Mapped[bool] = mapped_column(Boolean, default=False)
    shuffle_questions: Mapped[bool] = mapped_column(Boolean, default=False)
    shuffle_statements: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_backtracking: Mapped[bool] = mapped_column(Boolean, default=True)
    show_feedback_immediately: Mapped[bool] = mapped_column(Boolean, default=False)
    passing_percent: Mapped[float | None] = mapped_column(Float)

    # {"4": [0, 0, 0.2, 0.6, 1.0]} - the house default when a question sets none.
    scoring_curve: Mapped[dict | None] = mapped_column(JSON)
    anticheat_policy: Mapped[dict] = mapped_column(JSON, default=lambda: dict(DEFAULT_ANTICHEAT_POLICY))

    # Restrict entry to specific grades or schools; empty means open to all.
    allowed_grades: Mapped[list] = mapped_column(JSON, default=list)
    allowed_school_ids: Mapped[list] = mapped_column(JSON, default=list)
    registration_required: Mapped[bool] = mapped_column(Boolean, default=False)

    sections: Mapped[list["ExamSection"]] = relationship(
        back_populates="exam",
        cascade="all, delete-orphan",
        order_by="ExamSection.order",
        lazy="selectin",
    )
    questions: Mapped[list["ExamQuestion"]] = relationship(
        back_populates="exam",
        cascade="all, delete-orphan",
        order_by="ExamQuestion.order",
    )
    attempts: Mapped[list["Attempt"]] = relationship(
        back_populates="exam", cascade="all, delete-orphan"
    )
    enrollments: Mapped[list["ExamEnrollment"]] = relationship(
        back_populates="exam", cascade="all, delete-orphan"
    )

    def policy(self, key: str, default=None):
        merged = dict(DEFAULT_ANTICHEAT_POLICY)
        merged.update(self.anticheat_policy or {})
        return merged.get(key, default)

    @property
    def total_points(self) -> float:
        return sum(eq.points for eq in self.questions) or 0.0

    @property
    def question_count(self) -> int:
        return len(self.questions)

    def is_open_at(self, when: datetime | None = None) -> bool:
        when = when or utcnow()
        if self.status not in (ExamStatus.OPEN, ExamStatus.SCHEDULED):
            return False
        if self.opens_at and when < self.opens_at:
            return False
        if self.closes_at and when > self.closes_at:
            return False
        return True

    def results_released(self, when: datetime | None = None) -> bool:
        when = when or utcnow()
        if self.mode is ExamMode.PRACTICE or self.show_feedback_immediately:
            return True
        if self.results_visible_at:
            return when >= self.results_visible_at
        if self.closes_at:
            return when >= self.closes_at
        return self.status is ExamStatus.CLOSED

    def localized_title(self, locale: str) -> str:
        return (self.title_i18n or {}).get(locale) or self.title


class ExamSection(Base):
    """A topic block, e.g. 'Cell and Molecular Biology: Q4-Q12'."""

    __tablename__ = "exam_sections"

    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id", ondelete="CASCADE"), index=True)
    topic_id: Mapped[int | None] = mapped_column(ForeignKey("topics.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(200), default="")
    title_i18n: Mapped[dict] = mapped_column(JSON, default=dict)
    order: Mapped[int] = mapped_column(Integer, default=0)
    instructions: Mapped[str | None] = mapped_column(Text)

    exam: Mapped[Exam] = relationship(back_populates="sections")
    topic: Mapped["Topic | None"] = relationship()
    questions: Mapped[list["ExamQuestion"]] = relationship(
        back_populates="section", order_by="ExamQuestion.order"
    )

    def localized_title(self, locale: str) -> str:
        return (self.title_i18n or {}).get(locale) or self.title


class ExamQuestion(Base):
    """A question placed on a paper, with the points it is worth there."""

    __tablename__ = "exam_questions"
    __table_args__ = (UniqueConstraint("exam_id", "question_id", name="uq_exam_question"),)

    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id", ondelete="CASCADE"), index=True)
    section_id: Mapped[int | None] = mapped_column(
        ForeignKey("exam_sections.id", ondelete="SET NULL"), index=True
    )
    question_id: Mapped[int] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), index=True
    )
    order: Mapped[int] = mapped_column(Integer, default=0)
    points: Mapped[float] = mapped_column(Float, default=1.0)

    exam: Mapped[Exam] = relationship(back_populates="questions")
    section: Mapped[ExamSection | None] = relationship(back_populates="questions")
    question: Mapped["Question"] = relationship(lazy="selectin")


class ExamEnrollment(Base):
    """Pre-registration for an official sitting. Lets organisers close the roster
    before the window opens, which is itself an anti-cheat measure."""

    __tablename__ = "exam_enrollments"
    __table_args__ = (UniqueConstraint("exam_id", "user_id", name="uq_exam_enrollment"),)

    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    approved: Mapped[bool] = mapped_column(Boolean, default=True)
    seat_code: Mapped[str | None] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text)

    exam: Mapped[Exam] = relationship(back_populates="enrollments")
    user: Mapped["User"] = relationship()
