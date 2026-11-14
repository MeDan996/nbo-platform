"""Write a parsed paper into the archive tables (idempotent per exam)."""
from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.archive.layout import render_crop, strip_markup
from app.archive.parse import ParsedPaper, PElement
from app.models.archive import (
    IboAnswer,
    IboAsset,
    IboExam,
    IboExplanation,
    IboItem,
    IboItemOption,
    IboQuestion,
    IboQuestionElement,
    IboSourceDocument,
    IboSourcePage,
)

INFO_NOTES = ("blacked out on the official answer sheet",)


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _document(db: Session, base: Path, rel: str, *, year: int, role: str, lang: str,
              text_layer: str, exam: IboExam | None, page_count: int | None = None) -> IboSourceDocument:
    doc = db.scalar(select(IboSourceDocument).where(IboSourceDocument.path == rel))
    if doc is None:
        doc = IboSourceDocument(path=rel, year=year)
        db.add(doc)
    doc.sha256 = _sha(base / rel)
    doc.role, doc.language, doc.text_layer = role, lang, text_layer
    if exam is not None and doc.exam_id is None:
        doc.exam_id = exam.id
    if page_count is not None:
        doc.page_count = page_count
    db.flush()
    return doc


def _asset(db: Session, cache: dict, png: bytes, w: int, h: int, doc_id: int, page: int, bbox) -> IboAsset:
    sha = hashlib.sha256(png).hexdigest()
    if sha in cache:
        return cache[sha]
    a = db.scalar(select(IboAsset).where(IboAsset.sha256 == sha))
    if a is None:
        a = IboAsset(sha256=sha, mime="image/png", width=w, height=h, data=png, origin="page_crop",
                     source_document_id=doc_id, page_no=page, bbox=[round(v, 1) for v in bbox])
        db.add(a)
        db.flush()
    cache[sha] = a
    return a


def load_paper(db: Session, paper: ParsedPaper, base: Path, *, with_assets: bool = True, dpi: int = 150) -> IboExam:
    spec = paper.spec
    old = db.scalar(select(IboExam).where(IboExam.year == spec.year, IboExam.paper_code == spec.paper_code))
    if old is not None:
        db.delete(old)
        db.flush()

    exam = IboExam(
        year=spec.year, paper_code=spec.paper_code, title=spec.title, host_country=spec.host_country,
        language=spec.lang, total_points=spec.total_points, duration_minutes=spec.duration_minutes,
        instructions=paper.preamble.strip() or None, scoring=spec.scoring,
        extra={"parser": "app.archive", "body_font_size": paper.body_size},
    )
    db.add(exam)
    db.flush()

    pdf = paper.doc  # type: ignore[attr-defined]
    qdoc = _document(db, base, spec.path, year=spec.year, role=spec.doc_role, lang=spec.lang,
                     text_layer=spec.text_layer, exam=exam, page_count=len(pdf))
    qdoc.exam_id = exam.id
    db.execute(delete(IboSourcePage).where(IboSourcePage.document_id == qdoc.id))
    for p in paper.pages:
        db.add(IboSourcePage(document_id=qdoc.id, page_no=p.no, text=p.raw_text, width=p.width,
                             height=p.height, is_scanned=p.scanned))
    extra_docs = {}
    for rel, role in spec.extra_docs:
        extra_docs[role] = _document(db, base, rel, year=spec.year, role=role,
                                     lang="en", text_layer="native", exam=None)

    key_doc = extra_docs.get("answer_key")
    cache: dict = {}

    for pq in paper.questions:
        notes = list(pq.notes)
        q = IboQuestion(
            exam_id=exam.id, number=pq.number, label=pq.label, title=pq.title, section=pq.section,
            format=pq.format, status=pq.status, language=spec.lang,
            max_points=1.0 if spec.scoring and spec.scoring.get("type") == "ibo_curve" else float(len(pq.items) or 1),
            source_document_id=qdoc.id, page_start=pq.page_start, page_end=pq.page_end,
            needs_review=any(not n.startswith(INFO_NOTES) for n in notes), parse_notes=notes,
            extra={"text_layer": spec.text_layer},
        )
        db.add(q)
        db.flush()

        def add_elements(elements: list[PElement], explanation_id: int | None = None):
            ids: list[int | None] = []
            for pos, el in enumerate(elements):
                row = IboQuestionElement(
                    question_id=q.id, explanation_id=explanation_id, position=pos, role=el.role,
                    label=el.label, text=el.rich or None, text_plain=strip_markup(el.rich) or None,
                    lang=spec.lang, attrs=dict(el.attrs), page_no=el.page,
                    bbox=[round(v, 1) for v in el.bbox] if el.bbox else None,
                    text_source=spec.text_layer,
                )
                if el.role == "figure" and el.graphic is not None and with_assets:
                    png, w, h = render_crop(pdf, el.page, el.bbox, dpi=dpi)
                    row.asset_id = _asset(db, cache, png, w, h, qdoc.id, el.page, el.bbox).id
                if el.role == "table" and el.graphic is not None and with_assets:
                    # keep a picture of the table too: merged cells and shading do not survive as rows
                    png, w, h = render_crop(pdf, el.page, el.bbox, dpi=dpi)
                    row.asset_id = _asset(db, cache, png, w, h, qdoc.id, el.page, el.bbox).id
                db.add(row)
                db.flush()
                ids.append(row.id)
            for el, eid in zip(elements, ids):
                if el.parent is not None and 0 <= el.parent < len(ids):
                    db.get(IboQuestionElement, eid).parent_id = ids[el.parent]

        add_elements(pq.elements)

        items_by_label: dict[str, IboItem] = {}
        for pos, pi in enumerate(pq.items):
            attrs = {k: v for k, v in pi.attrs.items() if k != "alt_answers"}
            it = IboItem(question_id=q.id, position=pos, label=pi.label, item_type=pi.item_type,
                         text=pi.rich, text_plain=strip_markup(pi.rich), lang=spec.lang, attrs=attrs)
            db.add(it)
            db.flush()
            items_by_label[pi.label] = it
            for opos, (olab, otext) in enumerate(pi.options):
                db.add(IboItemOption(item_id=it.id, position=opos, label=olab, text=otext, lang=spec.lang))
            src_doc = key_doc.id if (pi.answer_source == "answer_key" and key_doc) else qdoc.id
            if pi.answer is not None:
                db.add(IboAnswer(item_id=it.id, value=pi.answer, value_text=pi.answer_text or "",
                                 status="official", source_document_id=src_doc,
                                 note=f"read from: {pi.answer_source}"))
            for alt in pi.attrs.get("alt_answers", []):
                db.add(IboAnswer(item_id=it.id, value=alt["value"], value_text=alt["text"] or "",
                                 status="superseded", source_document_id=qdoc.id,
                                 note=f"read from: {alt['source']}; differs from the official key"))

        for pos, pe in enumerate(pq.explanations):
            item = items_by_label.get(pe.item_label) if pe.item_label else None
            ex = IboExplanation(question_id=q.id, item_id=item.id if item else None, position=pos,
                                kind=pe.kind, text=pe.rich, text_plain=strip_markup(pe.rich),
                                lang=spec.lang, source_document_id=qdoc.id, text_source=spec.text_layer)
            db.add(ex)
            db.flush()
            if pe.elements:
                add_elements(pe.elements, explanation_id=ex.id)
    db.flush()
    return exam
