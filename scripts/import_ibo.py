"""Import a real IBO Theory Part A paper into the question bank.

Written against the 2024 Kyrgyz/Russian paper in `sample-questions/`, whose
layout is the standard IBO one:

    <question number on its own line>
    <stem, possibly spanning pages, with tables and figure references>
    "На листе ответов укажите T ... F ..."     <- the answer-sheet instruction
    A. <statement>
    B. <statement>
    C. <statement>
    D. <statement>
    1.0pt

The answer key PDF is a flat run of `Task #N. A) TRUE . checked <explanation>`.

Imported questions are marked `is_official_ibo`, which makes them read-only
templates in Creative mode: an author clones one rather than editing it, and the
clone keeps a `template_of_id` pointer back to the original.

Figure attribution is approximate. Images are taken from the pages a question
spans, which is right in most cases but can mis-assign a figure that spills onto
the page where the next question begins. The importer flags nothing as final -
an author fixes attribution in the editor - and page furniture (logos, banners)
is filtered out by size and by repetition across pages.

Usage:
    python scripts/import_ibo.py                      # import + seed topics
    python scripts/import_ibo.py --dry-run            # parse and report only
    python scripts/import_ibo.py --no-figures         # text only, much faster
"""
from __future__ import annotations

import argparse
import hashlib
import io
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pypdf import PdfReader  # noqa: E402

from app.config import UPLOAD_DIR  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models.content import (  # noqa: E402
    Figure,
    FigureTranslation,
    Question,
    QuestionTranslation,
    Statement,
    StatementTranslation,
    Topic,
    TopicTranslation,
)
from app.models.enums import (  # noqa: E402
    Difficulty,
    ExamMode,
    ExamStatus,
    QuestionStatus,
    QuestionType,
)
from app.models.exam import Exam, ExamQuestion, ExamSection  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
QUESTIONS_PDF = BASE_DIR / "sample-questions" / "theory_questions.pdf"
ANSWERS_PDF = BASE_DIR / "sample-questions" / "theory_answers.pdf"

# Official IBO Theory Part A section ranges and syllabus weights, taken from the
# paper's own front matter.
SECTIONS: list[tuple[str, int, int, float, str, dict[str, str]]] = [
    ("biosystematics", 1, 3, 5.0, "#9059ff",
     {"en": "Biosystematics", "ru": "Биосистематика", "ky": "Биосистематика"}),
    ("cell-molecular-biology", 4, 12, 20.0, "#1865f2",
     {"en": "Cell and Molecular Biology", "ru": "Клеточная и молекулярная биология",
      "ky": "Клетка жана молекулалык биология"}),
    ("ecology", 13, 17, 10.0, "#0d923f",
     {"en": "Ecology", "ru": "Экология", "ky": "Экология"}),
    ("ethology", 18, 19, 5.0, "#f5a623",
     {"en": "Ethology", "ru": "Этология", "ky": "Этология"}),
    ("genetics-evolution", 20, 29, 20.0, "#00a5a2",
     {"en": "Genetics and Evolution", "ru": "Генетика и эволюция",
      "ky": "Генетика жана эволюция"}),
    ("plant-anatomy-physiology", 30, 37, 15.0, "#5a8f29",
     {"en": "Plant Anatomy and Physiology", "ru": "Анатомия и физиология растений",
      "ky": "Өсүмдүктөрдүн анатомиясы жана физиологиясы"}),
    ("animal-anatomy-physiology", 38, 50, 25.0, "#d92916",
     {"en": "Animal Anatomy and Physiology", "ru": "Анатомия и физиология животных",
      "ky": "Жаныбарлардын анатомиясы жана физиологиясы"}),
]

# Lines that are page furniture rather than content.
FURNITURE = re.compile(
    r"^(Theoretical Exam - Part A|Q\d+-\d+|Kyrgyz \(Kyrgyzstan\)|[\d.]+pt|\s*)$"
)
# The space after the label is not reliable in the source ("A.Наиболее"), so it
# is optional. What keeps this from matching ordinary prose is the sequence
# check in `parse_questions`: a line only opens a statement if its label is the
# next one expected for that question.
STATEMENT_RE = re.compile(r"^\s*([A-D])\.\s*(\S.*)$")
# The answer-sheet instruction is a rubric for the paper form, not part of the
# question, so it is dropped rather than carried into the stem.
INSTRUCTION_RE = re.compile(
    r"^\s*На листе ответов укажите|^\s*[”\"]?[TF][”\"]?\s*для", re.I
)


# Every answer-key entry opens with a marker the markers themselves ticked off.
# It appears as ". Checked", ". checked", ". (Checked)" and "(Checked) .", so the
# pattern allows the word in either bracket style with punctuation on either side.
_CHECKED_RE = re.compile(r"^[\s.·]*\(?\s*checked\s*\)?[\s.·]*", re.IGNORECASE)


def clean_explanation(text: str) -> str:
    """Strip the marking-sheet bookkeeping off the front of an explanation."""
    return _CHECKED_RE.sub("", (text or "").strip()).strip()


@dataclass
class ParsedQuestion:
    number: int
    stem_lines: list[str] = field(default_factory=list)
    statements: dict[str, str] = field(default_factory=dict)
    first_page: int = 0
    last_page: int = 0

    @property
    def stem(self) -> str:
        return "\n".join(self.stem_lines).strip()


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def parse_questions(path: Path) -> list[ParsedQuestion]:
    reader = PdfReader(str(path))
    # (line, page_number) pairs, so a question knows which pages it spans.
    lines: list[tuple[str, int]] = []
    for index, page in enumerate(reader.pages, start=1):
        for raw in (page.extract_text() or "").split("\n"):
            lines.append((raw.rstrip(), index))

    questions: list[ParsedQuestion] = []
    expected = 1
    current: ParsedQuestion | None = None
    statement_label: str | None = None

    for text, page in lines:
        stripped = text.strip()

        # A line holding nothing but the next expected question number starts a
        # new question. Matching against the expected number - rather than any
        # bare integer - keeps stray numbers from tables and figures out.
        if stripped == str(expected) and expected <= 50:
            current = ParsedQuestion(number=expected, first_page=page, last_page=page)
            questions.append(current)
            expected += 1
            statement_label = None
            continue

        if current is None:
            continue
        current.last_page = max(current.last_page, page)

        if FURNITURE.match(stripped):
            continue

        match = STATEMENT_RE.match(text)
        next_label = "ABCD"[len(current.statements)] if len(current.statements) < 4 else None
        if match and match.group(1) == next_label:
            statement_label = match.group(1)
            current.statements[statement_label] = match.group(2).strip()
            continue

        if statement_label:
            # Continuation of the statement currently being read. The PDF
            # hyphenates across line breaks, so rejoin those.
            existing = current.statements[statement_label]
            if existing.endswith("-"):
                current.statements[statement_label] = existing[:-1] + stripped
            else:
                current.statements[statement_label] = existing + " " + stripped
            continue

        if INSTRUCTION_RE.match(stripped):
            continue
        current.stem_lines.append(stripped)

    return questions


def parse_answers(path: Path) -> dict[int, dict[str, tuple[bool, str]]]:
    """Return {question_number: {"A": (is_true, explanation), ...}}."""
    reader = PdfReader(str(path))
    text = " ".join(
        " ".join((page.extract_text() or "").split()) for page in reader.pages
    )

    # Task headers are inconsistently punctuated ("Task #21" vs "Task #21.").
    task_positions = [
        (int(m.group(1)), m.end()) for m in re.finditer(r"Task\s*#\s*(\d+)\.?", text)
    ]
    answers: dict[int, dict[str, tuple[bool, str]]] = {}

    for index, (number, start) in enumerate(task_positions):
        end = task_positions[index + 1][1] if index + 1 < len(task_positions) else len(text)
        # Trim the trailing "Task #N" header itself off the slice.
        block = text[start : end - 12 if end < len(text) else end]

        found: dict[str, tuple[bool, str]] = {}
        verdicts = list(re.finditer(r"\b([A-D])\)\s*(TRUE|FALSE)\s*\.?", block))
        for position, verdict in enumerate(verdicts):
            label = verdict.group(1)
            is_true = verdict.group(2) == "TRUE"
            tail_start = verdict.end()
            tail_end = (
                verdicts[position + 1].start() if position + 1 < len(verdicts) else len(block)
            )
            explanation = clean_explanation(block[tail_start:tail_end])
            found[label] = (is_true, explanation)
        if found:
            answers[number] = found
    return answers


# --------------------------------------------------------------------------- #
# Figures
# --------------------------------------------------------------------------- #
MIN_FIGURE_BYTES = 12_000
# An image appearing on more than this many pages is a logo or banner.
FURNITURE_PAGE_LIMIT = 4


def collect_images(path: Path) -> dict[int, list[tuple[str, bytes]]]:
    """{page_number: [(filename, data), ...]} with page furniture removed."""
    reader = PdfReader(str(path))
    per_page: dict[int, list[tuple[str, bytes, str]]] = {}
    digest_pages: Counter = Counter()

    for index, page in enumerate(reader.pages, start=1):
        try:
            images = list(page.images)
        except Exception:  # pragma: no cover - malformed embedded image
            continue
        for image in images:
            data = image.data
            if len(data) < MIN_FIGURE_BYTES:
                continue
            digest = hashlib.sha1(data).hexdigest()
            digest_pages[digest] += 1
            per_page.setdefault(index, []).append((image.name, data, digest))

    repeated = {d for d, count in digest_pages.items() if count > FURNITURE_PAGE_LIMIT}
    cleaned: dict[int, list[tuple[str, bytes]]] = {}
    for page_number, entries in per_page.items():
        keep = [(name, data) for name, data, digest in entries if digest not in repeated]
        if keep:
            cleaned[page_number] = keep
    return cleaned


def save_figure(data: bytes, name: str, question_id: int) -> tuple[str, str]:
    """Write the image under data/uploads and return (relative path, mime)."""
    suffix = Path(name).suffix.lower() or ".png"
    if suffix not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        suffix = ".png"
    mime = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp",
    }[suffix]

    folder = UPLOAD_DIR / "figures" / str(question_id)
    folder.mkdir(parents=True, exist_ok=True)
    filename = hashlib.sha1(data).hexdigest()[:16] + suffix
    (folder / filename).write_bytes(data)
    return f"figures/{question_id}/{filename}", mime


# --------------------------------------------------------------------------- #
# Seeding
# --------------------------------------------------------------------------- #
def seed_topics(db) -> dict[str, Topic]:
    topics: dict[str, Topic] = {}
    for order, (slug, _lo, _hi, weight, color, names) in enumerate(SECTIONS):
        topic = db.query(Topic).filter(Topic.slug == slug).one_or_none()
        if topic is None:
            topic = Topic(slug=slug)
            db.add(topic)
            db.flush()
        topic.ibo_weight = weight
        topic.color = color
        topic.order = order
        existing = {tr.locale for tr in topic.translations}
        for locale, name in names.items():
            if locale not in existing:
                db.add(TopicTranslation(topic_id=topic.id, locale=locale, name=name))
        topics[slug] = topic
    db.flush()
    return topics


def topic_for(number: int, topics: dict[str, Topic]) -> Topic | None:
    for slug, low, high, *_ in SECTIONS:
        if low <= number <= high:
            return topics.get(slug)
    return None


def difficulty_for(number: int) -> Difficulty:
    """The paper ramps up within each section; later questions skew harder."""
    for _slug, low, high, *_ in SECTIONS:
        if low <= number <= high:
            span = max(1, high - low)
            position = (number - low) / span
            if position < 0.34:
                return Difficulty.MEDIUM
            if position < 0.67:
                return Difficulty.HARD
            return Difficulty.OLYMPIAD
    return Difficulty.HARD


def import_paper(dry_run: bool = False, with_figures: bool = True) -> int:
    if not QUESTIONS_PDF.exists():
        print(f"missing {QUESTIONS_PDF}", file=sys.stderr)
        return 1

    parsed = parse_questions(QUESTIONS_PDF)
    answers = parse_answers(ANSWERS_PDF) if ANSWERS_PDF.exists() else {}

    print(f"parsed {len(parsed)} questions, {len(answers)} answer blocks")
    incomplete = [q.number for q in parsed if len(q.statements) != 4]
    if incomplete:
        print(f"  questions without exactly 4 statements: {incomplete}")
    missing_key = [q.number for q in parsed if q.number not in answers]
    if missing_key:
        print(f"  questions with no answer key: {missing_key}")

    if dry_run:
        for q in parsed[:2]:
            print(f"\n--- Q{q.number} (pages {q.first_page}-{q.last_page}) ---")
            print(q.stem[:400])
            for label, text in sorted(q.statements.items()):
                key = answers.get(q.number, {}).get(label)
                mark = ("TRUE" if key[0] else "FALSE") if key else "?"
                print(f"  {label}. [{mark}] {text[:110]}")
        return 0

    images = collect_images(QUESTIONS_PDF) if with_figures else {}
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    created = 0

    try:
        topics = seed_topics(db)

        exam = db.query(Exam).filter(Exam.slug == "ibo-2024-theory-a").one_or_none()
        if exam is None:
            exam = Exam(
                slug="ibo-2024-theory-a",
                title="IBO 2024 — Theory Exam, Part A",
                title_i18n={
                    "en": "IBO 2024 — Theory Exam, Part A",
                    "ru": "IBO 2024 — Теоретический экзамен, часть A",
                    "ky": "IBO 2024 — Теориялык экзамен, А бөлүгү",
                },
                subtitle="50 questions · 3 h 15 min · the real paper",
                mode=ExamMode.MOCK,
                status=ExamStatus.OPEN,
                duration_minutes=195,
                max_attempts=1,
                is_rated=False,
                show_feedback_immediately=True,
            )
            db.add(exam)
            db.flush()

        sections: dict[str, ExamSection] = {}
        for order, (slug, _lo, _hi, _w, _c, names) in enumerate(SECTIONS):
            topic = topics[slug]
            section = (
                db.query(ExamSection)
                .filter(ExamSection.exam_id == exam.id, ExamSection.topic_id == topic.id)
                .one_or_none()
            )
            if section is None:
                section = ExamSection(
                    exam_id=exam.id,
                    topic_id=topic.id,
                    title=names["en"],
                    title_i18n=names,
                    order=order,
                )
                db.add(section)
                db.flush()
            sections[slug] = section

        for item in parsed:
            source_ref = f"IBO 2024 Theory A Q{item.number}"
            existing = (
                db.query(Question).filter(Question.source_ref == source_ref).one_or_none()
            )
            if existing is not None:
                continue

            topic = topic_for(item.number, topics)
            question = Question(
                question_type=QuestionType.TF_BLOCK,
                status=QuestionStatus.APPROVED,
                difficulty=difficulty_for(item.number),
                topic_id=topic.id if topic else None,
                source_ref=source_ref,
                source_year=2024,
                is_official_ibo=True,
                max_points=1.0,
                estimated_seconds=195 * 60 // 50,
                tags=["ibo", "theory-a", "2024"],
            )
            db.add(question)
            db.flush()

            db.add(
                QuestionTranslation(
                    question_id=question.id,
                    locale="ru",
                    title=f"IBO 2024 · Q{item.number}",
                    stem=item.stem,
                )
            )
            # The stem exists only in Russian on this paper; an English row is
            # created empty so a translator has somewhere to work.
            for locale in ("en", "ky"):
                db.add(
                    QuestionTranslation(question_id=question.id, locale=locale, stem="")
                )

            key = answers.get(item.number, {})
            for order, label in enumerate("ABCD"):
                text = item.statements.get(label)
                if text is None:
                    continue
                is_true, explanation = key.get(label, (False, ""))
                statement = Statement(
                    question_id=question.id,
                    order=order,
                    label=label,
                    is_true=is_true,
                )
                db.add(statement)
                db.flush()
                db.add(
                    StatementTranslation(
                        statement_id=statement.id, locale="ru", text=text
                    )
                )
                db.add(
                    StatementTranslation(
                        statement_id=statement.id,
                        locale="en",
                        text="",
                        explanation=explanation or None,
                    )
                )
                db.add(StatementTranslation(statement_id=statement.id, locale="ky", text=""))

            if with_figures:
                order = 0
                for page in range(item.first_page, item.last_page + 1):
                    for name, data in images.get(page, []):
                        path, mime = save_figure(data, name, question.id)
                        figure = Figure(
                            question_id=question.id,
                            order=order,
                            path=path,
                            mime_type=mime,
                        )
                        db.add(figure)
                        db.flush()
                        db.add(
                            FigureTranslation(
                                figure_id=figure.id,
                                locale="ru",
                                alt_text=f"Рисунок к заданию {item.number}",
                            )
                        )
                        order += 1

            slug = next(
                (s for s, lo, hi, *_ in SECTIONS if lo <= item.number <= hi), None
            )
            db.add(
                ExamQuestion(
                    exam_id=exam.id,
                    section_id=sections[slug].id if slug else None,
                    question_id=question.id,
                    order=item.number,
                    points=1.0,
                )
            )
            created += 1

        db.commit()
        print(f"imported {created} questions into exam '{exam.slug}'")
    finally:
        db.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="parse and report only")
    parser.add_argument("--no-figures", action="store_true", help="skip image extraction")
    args = parser.parse_args()
    return import_paper(dry_run=args.dry_run, with_figures=not args.no_figures)


if __name__ == "__main__":
    raise SystemExit(main())
