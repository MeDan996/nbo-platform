"""Turn an IBO archive question into an editable draft in the question bank.

The archive (app.models.archive) keeps a past paper decomposed into ordered
elements, items, answers and explanations. Authors work in the regular editor
(app.routers.creative), whose model is a stem in a Markdown subset, statements
with per-locale text/explanation, and figures with captions. This module maps
one onto the other:

    paragraphs, tables, lead-ins, appendices -> stem (in order, with "(Figure N)"
                                                markers where figures stood)
    instruction                               -> instructions
    figure + its legend                       -> Figure row (image bytes copied
                                                into media_files) + caption
    T/F items + official answers              -> statements (is_true)
    per-item explanations                     -> statement explanation
    question-level explanations + references  -> general explanation
    choice items (2017 numeric)               -> MCQ_SINGLE options

The draft records `archive_question_id`, so its provenance is never lost and
the original archive rows stay untouched: every edit happens on the copy.
"""
from __future__ import annotations

import html
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.archive.sections import GROUP_TOPICS
from app.config import settings
from app.models.archive import IboAsset, IboQuestion, IboQuestionElement
from app.models.content import (
    Figure,
    FigureTranslation,
    Question,
    QuestionTranslation,
    Statement,
    StatementTranslation,
    Topic,
)
from app.models.enums import Difficulty, QuestionStatus, QuestionType
from app.models.media import MediaFile

LABELS = "ABCDEFGH"
STEM_ROLES = ("paragraph", "heading", "statement_lead", "appendix", "equation", "note", "table")

# Section names as printed in the papers -> topic slugs (app.services.topics).
_SECTION_TOPICS = [
    (r"cell|molec|micro|биохим|клетк|молекул", "cell-molecular-biology"),
    (r"plant|растени|ботан", "plant-anatomy-physiology"),
    (r"animal|животн|физиолог", "animal-anatomy-physiology"),
    (r"etholog|behavio|этолог|поведен", "ethology"),
    (r"genet|evolu|генет|эволюц", "genetics-evolution"),
    (r"ecolog|экол", "ecology"),
    (r"systemat|систематик|taxonom", "biosystematics"),
]


def rich_to_markdown(rich: str | None) -> str:
    """Archive inline markup (<i> <b> <sub> <sup> <br>) -> the editor's Markdown subset."""
    if not rich:
        return ""
    s = rich.replace("<br>", "\n")
    s = re.sub(r"<sub>(.*?)</sub>", lambda m: f"~{m.group(1).strip()}~", s, flags=re.S)
    s = re.sub(r"<sup>(.*?)</sup>", lambda m: f"^{m.group(1).strip()}^", s, flags=re.S)
    # keep the spaces that sat inside a tag ("<b>A. </b>x") outside the markers
    for tag, mark in (("b", "**"), ("i", "*")):
        s = re.sub(rf"<{tag}>(\s*)(.*?)(\s*)</{tag}>",
                   lambda m, k=mark: f"{m.group(1)}{k}{m.group(2)}{k}{m.group(3)}" if m.group(2) else m.group(1),
                   s, flags=re.S)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


def table_to_markdown(rows: list[list[str]]) -> str:
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)

    def cell(c):
        return (c or "").replace("|", "/").replace("\n", " ").strip() or " "

    lines = ["| " + " | ".join(cell(c) for c in rows[0] + [""] * (width - len(rows[0]))) + " |",
             "|" + "---|" * width]
    for r in rows[1:]:
        lines.append("| " + " | ".join(cell(c) for c in r + [""] * (width - len(r))) + " |")
    return "\n".join(lines)


def topic_for_question(db: Session, archive_q: IboQuestion) -> Topic | None:
    """The app topic for a draft: from the common section, else the printed name."""
    slug = GROUP_TOPICS.get(archive_q.section_group or "")
    if slug:
        return db.scalars(select(Topic).where(Topic.slug == slug)).first()
    return topic_for_section(db, archive_q.section)


def topic_for_section(db: Session, section: str | None) -> Topic | None:
    if not section:
        return None
    for pattern, slug in _SECTION_TOPICS:
        if re.search(pattern, section, re.I):
            return db.scalars(select(Topic).where(Topic.slug == slug)).first()
    return None


def source_label(q: IboQuestion) -> str:
    return f"IBO {q.exam.year} {q.exam.paper_code} {q.label}".strip()


def _locale(lang: str) -> str:
    return lang if lang in settings.locales else settings.locales[-1]


def _official(item):
    rows = [a for a in item.answers if a.status == "official"] or list(item.answers)
    return rows[0] if rows else None


def _media_path_for(db: Session, asset: IboAsset) -> str:
    """Archive images are copied once into media_files, shared by every draft."""
    path = f"archive/{asset.sha256[:2]}/{asset.sha256}.png"
    exists = db.scalar(select(MediaFile.id).where(MediaFile.path == path))
    if exists is None:
        db.add(MediaFile(path=path, mime_type=asset.mime, size=len(asset.data), data=asset.data))
        db.flush()
    return path


def build_draft(db: Session, archive_q: IboQuestion, author_id: int) -> Question:
    """Create a DRAFT question pre-filled from `archive_q` and return it."""
    loc = _locale(archive_q.language)
    stem_elements = [e for e in archive_q.elements if e.explanation_id is None]
    children: dict[int, list[IboQuestionElement]] = {}
    for e in stem_elements:
        if e.parent_id is not None:
            children.setdefault(e.parent_id, []).append(e)

    choice_items = [i for i in archive_q.items if i.item_type == "single_choice"]
    tf_items = [i for i in archive_q.items if i.item_type == "true_false"]
    other_items = [i for i in archive_q.items if i.item_type not in ("single_choice", "true_false")]
    qtype = QuestionType.MCQ_SINGLE if choice_items and not tf_items else QuestionType.TF_BLOCK

    topic = topic_for_question(db, archive_q)
    question = Question(
        question_type=qtype,
        status=QuestionStatus.DRAFT,
        difficulty=Difficulty.MEDIUM,
        topic_id=topic.id if topic else None,
        author_id=author_id,
        source_ref=source_label(archive_q)[:200],
        source_year=archive_q.exam.year,
        is_official_ibo=False,
        archive_question_id=archive_q.id,
        max_points=1.0,
        tags=["ibo-archive", f"ibo-{archive_q.exam.year}"],
    )
    db.add(question)
    db.flush()

    # ---- stem, instructions, figures -------------------------------------
    stem_parts: list[str] = []
    instructions: list[str] = []
    fig_no = 0
    used_as_caption: set[int] = set()
    for e in stem_elements:
        if e.role == "figure":
            fig_no += 1
            legends = children.get(e.id, [])
            caption = "\n".join(rich_to_markdown(l.text) for l in legends if l.text)
            used_as_caption.update(l.id for l in legends)
            stem_parts.append(f"*(Figure {fig_no})*")
            if e.asset is not None:
                fig = Figure(question_id=question.id, order=fig_no - 1, path=_media_path_for(db, e.asset),
                             mime_type=e.asset.mime, width=e.asset.width, height=e.asset.height,
                             in_appendix=e.role == "appendix")
                db.add(fig)
                db.flush()
                for locale in settings.locales:
                    db.add(FigureTranslation(
                        figure_id=fig.id, locale=locale,
                        caption=caption or None if locale == loc else None,
                        alt_text=f"Figure {fig_no}" if locale == loc else None))
        elif e.role == "instruction":
            instructions.append(rich_to_markdown(e.text))
        elif e.role == "legend":
            continue  # placed after the loop if it had no figure
        elif e.role == "table":
            md = table_to_markdown((e.attrs or {}).get("rows") or [])
            if md:
                stem_parts.append(md)
        elif e.role in STEM_ROLES and e.text:
            text = rich_to_markdown(e.text)
            if e.role == "heading":
                text = f"### {text}"
            stem_parts.append(text)
    for e in stem_elements:  # legends not attached to a figure stay in the text
        if e.role == "legend" and e.id not in used_as_caption and e.text:
            stem_parts.append(f"*{rich_to_markdown(e.text)}*")

    # Items the editor cannot hold natively are kept, visibly, in the stem.
    for it in other_items:
        ans = _official(it)
        stem_parts.append(f"**{it.label}.** {rich_to_markdown(it.text)}"
                          + (f" — answer: {ans.value_text}" if ans else ""))
    if choice_items and tf_items:
        for it in choice_items:
            ans = _official(it)
            opts = ", ".join(o.text for o in it.options)
            stem_parts.append(f"**{it.label}.** {rich_to_markdown(it.text)} (options: {opts})"
                              + (f" — answer: {ans.value_text}" if ans else ""))

    # ---- explanations ------------------------------------------------------
    per_item: dict[int, list[str]] = {}
    general: list[str] = []
    references: list[str] = []
    for x in archive_q.explanations:
        text = rich_to_markdown(x.text)
        if not text:
            continue
        if x.kind == "reference":
            references.append(text)
        elif x.item_id is not None:
            per_item.setdefault(x.item_id, []).append(text)
        else:
            general.append(text)
    general_md = "\n\n".join(general)
    if references:
        general_md = (general_md + "\n\n" if general_md else "") + "**References:** " + "; ".join(references)

    q_trs: dict[str, QuestionTranslation] = {}
    for locale in settings.locales:
        mine = locale == loc
        q_trs[locale] = QuestionTranslation(
            question_id=question.id, locale=locale,
            title=(archive_q.title or source_label(archive_q))[:300] if mine else None,
            stem="\n\n".join(p for p in stem_parts if p) if mine else "",
            instructions="\n".join(instructions) or None if mine else None,
            general_explanation=general_md or None if mine else None,
        )
        db.add(q_trs[locale])

    # ---- statements --------------------------------------------------------
    if qtype is QuestionType.MCQ_SINGLE:
        item = choice_items[0]
        ans = _official(item)
        correct = (ans.value or {}).get("option") if ans else None
        rows = [(o.label, o.text, o.label == correct) for o in item.options]
        prompt = rich_to_markdown(item.text)
        if prompt:
            q_trs[loc].stem = (q_trs[loc].stem + "\n\n" + prompt).strip()
        explanations = {}
    else:
        rows = []
        for it in tf_items:
            ans = _official(it)
            rows.append((it.label, rich_to_markdown(it.text), bool(ans and (ans.value or {}).get("bool"))))
        explanations = {i: "\n".join(per_item.get(it.id, [])) for i, it in enumerate(tf_items)}

    for index, (_label, text, is_true) in enumerate(rows[: len(LABELS)]):
        st = Statement(question_id=question.id, order=index, label=LABELS[index], is_true=is_true)
        db.add(st)
        db.flush()
        for locale in settings.locales:
            mine = locale == loc
            db.add(StatementTranslation(
                statement_id=st.id, locale=locale, text=text if mine else "",
                explanation=(explanations.get(index) or None) if mine else None))
    db.flush()
    return question
