"""Question bank: topics, questions, statements, figures and their translations.

The content model is deliberately shaped around the IBO Theory Part A format -
a stem with figures, followed by N true/false statements scored on a partial
credit curve - while staying general enough for MCQ, numeric and open formats.
Every human-readable string lives in a `*_translations` row so the same question
can be served in Russian, Kyrgyz or English.
"""
from __future__ import annotations

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

from app.db import Base, EnumType
from app.models.enums import Difficulty, QuestionStatus, QuestionType, ReviewDecision

if TYPE_CHECKING:
    from app.models.user import User


class Topic(Base):
    """IBO subject taxonomy. Two levels: section (e.g. Genetics and Evolution)
    and optional subtopic (e.g. Linkage mapping)."""

    __tablename__ = "topics"

    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"))
    parent: Mapped["Topic | None"] = relationship(
        remote_side="Topic.id", back_populates="children"
    )
    children: Mapped[list["Topic"]] = relationship(back_populates="parent")

    # Official IBO weighting, used to build balanced exams and to weight stats.
    ibo_weight: Mapped[float] = mapped_column(Float, default=0.0)
    color: Mapped[str] = mapped_column(String(7), default="#1865f2")
    icon: Mapped[str | None] = mapped_column(String(40))
    order: Mapped[int] = mapped_column(Integer, default=0)

    translations: Mapped[list["TopicTranslation"]] = relationship(
        back_populates="topic", cascade="all, delete-orphan", lazy="selectin"
    )

    def name(self, locale: str, fallback: str = "en") -> str:
        by_locale = {t.locale: t.name for t in self.translations}
        return by_locale.get(locale) or by_locale.get(fallback) or self.slug


class TopicTranslation(Base):
    __tablename__ = "topic_translations"
    __table_args__ = (UniqueConstraint("topic_id", "locale", name="uq_topic_locale"),)

    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"), index=True)
    locale: Mapped[str] = mapped_column(String(5), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)

    topic: Mapped[Topic] = relationship(back_populates="translations")


class Question(Base):
    __tablename__ = "questions"

    question_type: Mapped[QuestionType] = mapped_column(
        EnumType(QuestionType), default=QuestionType.TF_BLOCK, index=True
    )
    status: Mapped[QuestionStatus] = mapped_column(
        EnumType(QuestionStatus), default=QuestionStatus.DRAFT, index=True
    )
    difficulty: Mapped[Difficulty] = mapped_column(EnumType(Difficulty), default=Difficulty.MEDIUM)

    topic_id: Mapped[int | None] = mapped_column(
        ForeignKey("topics.id", ondelete="SET NULL"), index=True
    )
    topic: Mapped[Topic | None] = relationship()

    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    author: Mapped["User | None"] = relationship(foreign_keys=[author_id])

    # Provenance. When an author clones a real IBO question as a template we keep
    # the pointer, so the original stays attributable and is never silently reused.
    source_ref: Mapped[str | None] = mapped_column(String(200), index=True)
    source_year: Mapped[int | None] = mapped_column(Integer, index=True)
    is_official_ibo: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    template_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("questions.id", ondelete="SET NULL")
    )
    template_of: Mapped["Question | None"] = relationship(remote_side="Question.id")

    max_points: Mapped[float] = mapped_column(Float, default=1.0)
    # Overrides the default IBO partial-credit curve for this question.
    # Shape: {"4": [0, 0, 0.2, 0.6, 1.0]} keyed by statement count.
    scoring_curve: Mapped[dict | None] = mapped_column(JSON)
    # For NUMERIC / SHORT_TEXT: {"value": 42.0, "tolerance": 0.5, "unit": "mm"}
    # or {"accept": ["mitochondrion", "mitohondriya"], "case_sensitive": false}
    answer_spec: Mapped[dict | None] = mapped_column(JSON)

    estimated_seconds: Mapped[int] = mapped_column(Integer, default=180)
    # Live difficulty statistics, refreshed from real attempts.
    times_used: Mapped[int] = mapped_column(Integer, default=0)
    p_value: Mapped[float | None] = mapped_column(Float)  # mean fraction of max points
    discrimination: Mapped[float | None] = mapped_column(Float)  # point-biserial correlation

    tags: Mapped[list] = mapped_column(JSON, default=list)

    translations: Mapped[list["QuestionTranslation"]] = relationship(
        back_populates="question", cascade="all, delete-orphan", lazy="selectin"
    )
    statements: Mapped[list["Statement"]] = relationship(
        back_populates="question",
        cascade="all, delete-orphan",
        order_by="Statement.order",
        lazy="selectin",
    )
    figures: Mapped[list["Figure"]] = relationship(
        back_populates="question",
        cascade="all, delete-orphan",
        order_by="Figure.order",
        lazy="selectin",
    )
    reviews: Mapped[list["QuestionReview"]] = relationship(
        back_populates="question", cascade="all, delete-orphan", order_by="QuestionReview.id"
    )

    def tr(self, locale: str, fallback: str = "en") -> "QuestionTranslation | None":
        by_locale = {t.locale: t for t in self.translations}
        return (
            by_locale.get(locale)
            or by_locale.get(fallback)
            or (self.translations[0] if self.translations else None)
        )

    def title(self, locale: str) -> str:
        t = self.tr(locale)
        if t and t.title:
            return t.title
        return "Question #%s" % self.id

    @property
    def locales(self) -> list[str]:
        return sorted(t.locale for t in self.translations)


class QuestionTranslation(Base):
    __tablename__ = "question_translations"
    __table_args__ = (UniqueConstraint("question_id", "locale", name="uq_question_locale"),)

    question_id: Mapped[int] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), index=True
    )
    locale: Mapped[str] = mapped_column(String(5), index=True)
    title: Mapped[str | None] = mapped_column(String(300))
    stem: Mapped[str] = mapped_column(Text, default="")  # Markdown + LaTeX
    instructions: Mapped[str | None] = mapped_column(Text)
    general_explanation: Mapped[str | None] = mapped_column(Text)

    question: Mapped[Question] = relationship(back_populates="translations")


class Statement(Base):
    """One row of the answer block.

    For TF_BLOCK each statement is judged true or false; for MCQ_SINGLE /
    MCQ_MULTI the same row is an option and `is_true` marks a correct one.
    """

    __tablename__ = "statements"

    question_id: Mapped[int] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), index=True
    )
    order: Mapped[int] = mapped_column(Integer, default=0)
    label: Mapped[str] = mapped_column(String(4), default="A")  # A, B, C, D
    is_true: Mapped[bool] = mapped_column(Boolean, default=False)
    # Keeps the statement in position when the rest of the block is shuffled.
    pin_position: Mapped[bool] = mapped_column(Boolean, default=False)

    translations: Mapped[list["StatementTranslation"]] = relationship(
        back_populates="statement", cascade="all, delete-orphan", lazy="selectin"
    )
    question: Mapped[Question] = relationship(back_populates="statements")

    def tr(self, locale: str, fallback: str = "en") -> "StatementTranslation | None":
        by_locale = {t.locale: t for t in self.translations}
        return (
            by_locale.get(locale)
            or by_locale.get(fallback)
            or (self.translations[0] if self.translations else None)
        )

    def text(self, locale: str) -> str:
        t = self.tr(locale)
        return t.text if t else ""

    def explanation(self, locale: str) -> str:
        t = self.tr(locale)
        return (t.explanation or "") if t else ""


class StatementTranslation(Base):
    __tablename__ = "statement_translations"
    __table_args__ = (UniqueConstraint("statement_id", "locale", name="uq_statement_locale"),)

    statement_id: Mapped[int] = mapped_column(
        ForeignKey("statements.id", ondelete="CASCADE"), index=True
    )
    locale: Mapped[str] = mapped_column(String(5), index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    explanation: Mapped[str | None] = mapped_column(Text)

    statement: Mapped[Statement] = relationship(back_populates="translations")


class Figure(Base):
    __tablename__ = "figures"

    question_id: Mapped[int] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), index=True
    )
    order: Mapped[int] = mapped_column(Integer, default=0)
    # Path relative to data/uploads, served through /media/<path>.
    path: Mapped[str] = mapped_column(String(300))
    mime_type: Mapped[str] = mapped_column(String(80), default="image/png")
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    # Figures may live in an appendix rather than inline, as in the real exam.
    in_appendix: Mapped[bool] = mapped_column(Boolean, default=False)

    translations: Mapped[list["FigureTranslation"]] = relationship(
        back_populates="figure", cascade="all, delete-orphan", lazy="selectin"
    )
    question: Mapped[Question] = relationship(back_populates="figures")

    def caption(self, locale: str) -> str:
        by_locale = {t.locale: t for t in self.translations}
        t = by_locale.get(locale) or next(iter(by_locale.values()), None)
        return t.caption if t and t.caption else ""

    def alt(self, locale: str) -> str:
        by_locale = {t.locale: t for t in self.translations}
        t = by_locale.get(locale) or next(iter(by_locale.values()), None)
        return t.alt_text if t and t.alt_text else "Figure"


class FigureTranslation(Base):
    __tablename__ = "figure_translations"
    __table_args__ = (UniqueConstraint("figure_id", "locale", name="uq_figure_locale"),)

    figure_id: Mapped[int] = mapped_column(ForeignKey("figures.id", ondelete="CASCADE"), index=True)
    locale: Mapped[str] = mapped_column(String(5), index=True)
    caption: Mapped[str | None] = mapped_column(Text)
    alt_text: Mapped[str | None] = mapped_column(Text)

    figure: Mapped[Figure] = relationship(back_populates="translations")


class QuestionReview(Base):
    """Editorial workflow: an author submits, a reviewer approves or asks for changes."""

    __tablename__ = "question_reviews"

    question_id: Mapped[int] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), index=True
    )
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decision: Mapped[ReviewDecision] = mapped_column(EnumType(ReviewDecision), default=ReviewDecision.COMMENT)
    comment: Mapped[str | None] = mapped_column(Text)

    question: Mapped[Question] = relationship(back_populates="reviews")
    reviewer: Mapped["User | None"] = relationship()
