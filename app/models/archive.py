"""IBO archive: past olympiad theory papers, decomposed into reusable parts.

This is a *source archive*, separate from the live question bank in
`app.models.content`. Its job is to keep every object of an original question
individually addressable so a question can be rebuilt piece by piece, or used as
a template where one figure, legend or statement is swapped out.

    IboSourceDocument   one PDF on disk (questions, answer key, solutions ...)
      IboSourcePage     raw text of each page, kept for search and re-parsing
    IboExam             one paper of one year (2013 Theory 1, 2014 Part A ...)
      IboQuestion       one numbered task
        IboQuestionElement  ordered content blocks of the stem: paragraphs,
                            figures, legends, tables, instructions, appendices.
                            Tree-shaped (a legend points at its figure).
        IboItem             the answerable parts: T/F statements, choice or
                            numeric sub-questions, gaps ...
          IboItemOption     choices of a choice item
          IboAnswer         the official answer(s) for an item
        IboExplanation      solution text, per item or for the whole question;
                            may own elements of its own (figures in a solution)
    IboAsset            image bytes (figure crops, embedded images), deduplicated
    IboTextVariant      a translation of any text field of any archive row

Design rules:
- `role`, `kind`, `format` columns are free strings, not enums. The vocabulary
  lives in the constants below and is enforced by the importer, not the
  database, so a "creative" question shape never needs a migration.
- Each text-bearing row records its own `lang`: a Russian stem can have English
  answers and an English explanation.
- Text is stored twice: `text` with a tiny inline markup subset
  (<i> <b> <sub> <sup> <br>) that keeps chemical formulae and gene names intact,
  and `text_plain` for searching.
- `extra` / `attrs` JSON columns carry whatever a specific paper needs that the
  columns do not (figure-internal labels, table header rows, OCR confidence).
- `needs_review` + `parse_notes` mark everything the parser was unsure of.
"""
from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# JSONB on Postgres (indexable, compact), plain JSON on SQLite for local dev.
JsonType = JSON().with_variant(JSONB(), "postgresql")

# --- vocabularies (documentation + importer validation, not DB constraints) ---
DOCUMENT_ROLES = ("questions", "questions_with_solutions", "answer_key", "answer_sheet")
TEXT_LAYERS = ("native", "embedded_ocr", "ocr", "none")
QUESTION_FORMATS = ("true_false", "single_choice", "numeric_choice", "gap_fill", "mixed", "other")
QUESTION_STATUSES = ("active", "cancelled", "disputed")
ELEMENT_ROLES = (
    "paragraph",       # body text of the stem
    "heading",         # a title inside the stem
    "figure",          # an image region (asset_id set)
    "legend",          # figure caption / legend / label glossary (parent_id -> figure)
    "table",           # structured table (attrs.rows)
    "equation",        # formula given in the stem
    "instruction",     # "Indicate if each of the following statements is true or false."
    "statement_lead",  # lead-in shared by all statements ("Protein X affects binding of ERa to ...")
    "appendix",        # extra material after the statements
    "note",            # anything else
)
ITEM_TYPES = ("true_false", "single_choice", "numeric", "gap", "text")
EXPLANATION_KINDS = ("explanation", "commentary", "note", "reference")


class IboSourceDocument(Base):
    __tablename__ = "ibo_source_documents"

    year: Mapped[int] = mapped_column(Integer, index=True)
    exam_id: Mapped[int | None] = mapped_column(
        ForeignKey("ibo_exams.id", ondelete="SET NULL"), index=True
    )
    # Path relative to the project root, forward slashes.
    path: Mapped[str] = mapped_column(String(500), unique=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(40))
    language: Mapped[str] = mapped_column(String(5))
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    text_layer: Mapped[str] = mapped_column(String(20), default="native")
    notes: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict] = mapped_column(JsonType, default=dict)

    pages: Mapped[list["IboSourcePage"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", order_by="IboSourcePage.page_no"
    )


class IboSourcePage(Base):
    __tablename__ = "ibo_source_pages"
    __table_args__ = (UniqueConstraint("document_id", "page_no", name="uq_ibo_page"),)

    document_id: Mapped[int] = mapped_column(
        ForeignKey("ibo_source_documents.id", ondelete="CASCADE"), index=True
    )
    page_no: Mapped[int] = mapped_column(Integer)  # 1-based
    text: Mapped[str] = mapped_column(Text, default="")
    width: Mapped[float] = mapped_column(Float, default=0)
    height: Mapped[float] = mapped_column(Float, default=0)
    is_scanned: Mapped[bool] = mapped_column(Boolean, default=False)

    document: Mapped[IboSourceDocument] = relationship(back_populates="pages")


class IboExam(Base):
    __tablename__ = "ibo_exams"
    __table_args__ = (UniqueConstraint("year", "paper_code", name="uq_ibo_exam"),)

    year: Mapped[int] = mapped_column(Integer, index=True)
    paper_code: Mapped[str] = mapped_column(String(10))  # "A" or "B" (papers numbered 1/2 are stored as A/B)
    title: Mapped[str] = mapped_column(String(200))
    host_country: Mapped[str | None] = mapped_column(String(80))
    language: Mapped[str] = mapped_column(String(5))  # language of the question text
    total_points: Mapped[float | None] = mapped_column(Float)
    duration_minutes: Mapped[int | None] = mapped_column(Integer)
    instructions: Mapped[str | None] = mapped_column(Text)
    # Scoring rule of the paper, e.g. {"type": "ibo_curve", "curve": {"4": [0,0,0.2,0.6,1]}}
    # or {"type": "per_item", "points": 1}.
    scoring: Mapped[dict | None] = mapped_column(JsonType)
    extra: Mapped[dict] = mapped_column(JsonType, default=dict)

    questions: Mapped[list["IboQuestion"]] = relationship(
        back_populates="exam", cascade="all, delete-orphan", order_by="IboQuestion.number"
    )


class IboAsset(Base):
    """Binary image data. Stored in the database so the archive is self-contained
    on hosts without a persistent volume; deduplicated by content hash."""

    __tablename__ = "ibo_assets"

    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    mime: Mapped[str] = mapped_column(String(40), default="image/png")
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    # "page_crop" (rendered region of a page) or "embedded" (original image bytes)
    origin: Mapped[str] = mapped_column(String(20), default="page_crop")
    source_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("ibo_source_documents.id", ondelete="SET NULL")
    )
    page_no: Mapped[int | None] = mapped_column(Integer)
    bbox: Mapped[list | None] = mapped_column(JsonType)  # [x0, y0, x1, y1] in PDF points


class IboQuestion(Base):
    __tablename__ = "ibo_questions"
    __table_args__ = (UniqueConstraint("exam_id", "number", name="uq_ibo_question"),)

    exam_id: Mapped[int] = mapped_column(ForeignKey("ibo_exams.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer)  # 1-based, as printed
    label: Mapped[str] = mapped_column(String(40))  # "Q. 12", "12."
    title: Mapped[str | None] = mapped_column(String(300))
    section: Mapped[str | None] = mapped_column(String(200), index=True)  # topic as printed
    # One of the eight common sections (see app.archive.sections), for filtering.
    section_group: Mapped[str | None] = mapped_column(String(40), index=True)
    format: Mapped[str] = mapped_column(String(30), default="true_false", index=True)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    language: Mapped[str] = mapped_column(String(5))
    max_points: Mapped[float | None] = mapped_column(Float)
    scoring: Mapped[dict | None] = mapped_column(JsonType)  # overrides the exam rule
    source_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("ibo_source_documents.id", ondelete="SET NULL")
    )
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    parse_notes: Mapped[list] = mapped_column(JsonType, default=list)
    # Template lineage: a question built from this one points back at it.
    derived_from_id: Mapped[int | None] = mapped_column(
        ForeignKey("ibo_questions.id", ondelete="SET NULL")
    )
    tags: Mapped[list] = mapped_column(JsonType, default=list)
    extra: Mapped[dict] = mapped_column(JsonType, default=dict)

    exam: Mapped[IboExam] = relationship(back_populates="questions")
    elements: Mapped[list["IboQuestionElement"]] = relationship(
        back_populates="question",
        cascade="all, delete-orphan",
        order_by="IboQuestionElement.position",
        foreign_keys="IboQuestionElement.question_id",
    )
    items: Mapped[list["IboItem"]] = relationship(
        back_populates="question", cascade="all, delete-orphan", order_by="IboItem.position"
    )
    explanations: Mapped[list["IboExplanation"]] = relationship(
        back_populates="question", cascade="all, delete-orphan", order_by="IboExplanation.position"
    )


class IboQuestionElement(Base):
    """One content block of a question. The stem is the ordered list of elements
    with `explanation_id IS NULL`; elements with `explanation_id` set belong to
    that explanation (e.g. a graph drawn in the solution)."""

    __tablename__ = "ibo_question_elements"

    question_id: Mapped[int] = mapped_column(
        ForeignKey("ibo_questions.id", ondelete="CASCADE"), index=True
    )
    explanation_id: Mapped[int | None] = mapped_column(
        ForeignKey("ibo_explanations.id", ondelete="CASCADE"), index=True
    )
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("ibo_question_elements.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(30), index=True)
    label: Mapped[str | None] = mapped_column(String(80))  # "Fig. 1", "Table 2", "A"
    text: Mapped[str | None] = mapped_column(Text)
    text_plain: Mapped[str | None] = mapped_column(Text)
    lang: Mapped[str] = mapped_column(String(5))
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("ibo_assets.id", ondelete="SET NULL"))
    # Role-specific payload: table -> {"rows": [[...]]}; figure -> {"labels": [...]}
    attrs: Mapped[dict] = mapped_column(JsonType, default=dict)
    page_no: Mapped[int | None] = mapped_column(Integer)
    bbox: Mapped[list | None] = mapped_column(JsonType)
    text_source: Mapped[str] = mapped_column(String(20), default="native")

    question: Mapped[IboQuestion] = relationship(
        back_populates="elements", foreign_keys=[question_id]
    )
    asset: Mapped[IboAsset | None] = relationship()
    children: Mapped[list["IboQuestionElement"]] = relationship(
        order_by="IboQuestionElement.position"
    )


class IboItem(Base):
    """An answerable unit: a T/F statement, a choice question, a gap."""

    __tablename__ = "ibo_items"
    __table_args__ = (UniqueConstraint("question_id", "position", name="uq_ibo_item"),)

    question_id: Mapped[int] = mapped_column(
        ForeignKey("ibo_questions.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(10))  # "A", "B", "1"
    item_type: Mapped[str] = mapped_column(String(20), default="true_false")
    text: Mapped[str] = mapped_column(Text, default="")
    text_plain: Mapped[str] = mapped_column(Text, default="")
    lang: Mapped[str] = mapped_column(String(5))
    # Optional pointer to the element the item is about (e.g. one figure panel).
    element_id: Mapped[int | None] = mapped_column(
        # use_alter: items -> elements -> explanations -> items is a cycle, so this
        # constraint is added after the tables exist.
        ForeignKey("ibo_question_elements.id", ondelete="SET NULL", use_alter=True,
                   name="fk_ibo_items_element_id")
    )
    points: Mapped[float | None] = mapped_column(Float)
    attrs: Mapped[dict] = mapped_column(JsonType, default=dict)

    question: Mapped[IboQuestion] = relationship(back_populates="items")
    options: Mapped[list["IboItemOption"]] = relationship(
        back_populates="item", cascade="all, delete-orphan", order_by="IboItemOption.position"
    )
    answers: Mapped[list["IboAnswer"]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )


class IboItemOption(Base):
    __tablename__ = "ibo_item_options"
    __table_args__ = (UniqueConstraint("item_id", "position", name="uq_ibo_option"),)

    item_id: Mapped[int] = mapped_column(ForeignKey("ibo_items.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(10))
    text: Mapped[str] = mapped_column(Text)
    lang: Mapped[str] = mapped_column(String(5))

    item: Mapped[IboItem] = relationship(back_populates="options")


class IboAnswer(Base):
    """Official answer for an item. Several rows are allowed (a corrected key,
    an answer accepted after appeal); `status` tells them apart."""

    __tablename__ = "ibo_answers"

    item_id: Mapped[int] = mapped_column(ForeignKey("ibo_items.id", ondelete="CASCADE"), index=True)
    # {"bool": true} | {"option": "C"} | {"number": 131, "unit": "bpm"} | {"text": "..."}
    value: Mapped[dict] = mapped_column(JsonType)
    value_text: Mapped[str] = mapped_column(String(200))  # human-readable: "True", "150 bpm"
    status: Mapped[str] = mapped_column(String(20), default="official")  # official|corrected|cancelled
    source_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("ibo_source_documents.id", ondelete="SET NULL")
    )
    note: Mapped[str | None] = mapped_column(Text)

    item: Mapped[IboItem] = relationship(back_populates="answers")


class IboExplanation(Base):
    __tablename__ = "ibo_explanations"

    question_id: Mapped[int] = mapped_column(
        ForeignKey("ibo_questions.id", ondelete="CASCADE"), index=True
    )
    item_id: Mapped[int | None] = mapped_column(
        ForeignKey("ibo_items.id", ondelete="CASCADE"), index=True
    )  # NULL = about the question as a whole
    position: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20), default="explanation")
    text: Mapped[str] = mapped_column(Text)
    text_plain: Mapped[str] = mapped_column(Text)
    lang: Mapped[str] = mapped_column(String(5))
    source_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("ibo_source_documents.id", ondelete="SET NULL")
    )
    text_source: Mapped[str] = mapped_column(String(20), default="native")

    question: Mapped[IboQuestion] = relationship(back_populates="explanations")


class IboTextVariant(Base):
    """A translation of one text field of any archive row, e.g. the English text
    of a Russian-only statement: (entity="ibo_items", entity_id=12, field="text", lang="en")."""

    __tablename__ = "ibo_text_variants"
    __table_args__ = (
        UniqueConstraint("entity", "entity_id", "field", "lang", name="uq_ibo_text_variant"),
    )

    entity: Mapped[str] = mapped_column(String(40), index=True)
    entity_id: Mapped[int] = mapped_column(Integer, index=True)
    field: Mapped[str] = mapped_column(String(40), default="text")
    lang: Mapped[str] = mapped_column(String(5))
    text: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(40), default="manual")  # manual|official|machine
