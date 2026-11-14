"""Generic question parser over a page-layout stream.

A paper is flattened into one reading-order stream of `Line`s and `Graphic`s.
A `PaperSpec` (one per source, see `sources.py`) says how to recognise
question starts, section headings, instructions, statement labels, inline
answers and explanation markers; this module runs the state machine that
turns the stream into `PQuestion`s:

    stem  ->  items  ->  (answers)  ->  explanation  ->  references

Stems become ordered elements (paragraph / figure / legend / table /
instruction / statement_lead / appendix). Items are the answerable units.
Everything the parser was unsure about is written to `PQuestion.notes` and sets
`needs_review`.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

import pymupdf

from app.archive.layout import (
    CAPTION_RE,
    Graphic,
    Line,
    PageLayout,
    body_size_of,
    extract_page,
    normalize_for_repeat,
    strip_markup,
)

LETTERS = "ABCDEFGH"
TRUE_WORDS = {"true", "t", "верно", "да", "✓", "v", "√", "right", "correct"}
FALSE_WORDS = {"false", "f", "неверно", "нет", "✕", "x", "×", "wrong", "incorrect"}


def to_bool(word: str) -> bool | None:
    w = word.strip().strip(".,;:").lower()
    if w in TRUE_WORDS:
        return True
    if w in FALSE_WORDS:
        return False
    return None


# ------------------------------------------------------------- data model ---

@dataclass
class PElement:
    role: str
    rich: str = ""
    label: str | None = None
    page: int | None = None
    bbox: tuple | None = None
    graphic: Graphic | None = None
    attrs: dict = field(default_factory=dict)
    parent: int | None = None  # index into the same element list
    size: float = 0.0
    last_line: Line | None = None

    @property
    def plain(self):
        return strip_markup(self.rich)


@dataclass
class PExplanation:
    item_label: str | None
    kind: str
    rich: str
    elements: list[PElement] = field(default_factory=list)

    @property
    def plain(self):
        return strip_markup(self.rich)


@dataclass
class PItem:
    label: str
    item_type: str = "true_false"
    rich: str = ""
    options: list[tuple[str, str]] = field(default_factory=list)
    answer: dict | None = None
    answer_text: str | None = None
    answer_source: str | None = None
    text_x0: float | None = None
    last_line: Line | None = None
    attrs: dict = field(default_factory=dict)

    @property
    def plain(self):
        return strip_markup(self.rich)


@dataclass
class PQuestion:
    number: int
    label: str
    title: str | None = None
    section: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    elements: list[PElement] = field(default_factory=list)
    items: list[PItem] = field(default_factory=list)
    explanations: list[PExplanation] = field(default_factory=list)
    status: str = "active"
    notes: list[str] = field(default_factory=list)
    lines: list = field(default_factory=list)  # raw tokens, for debugging
    pending_answers: dict = field(default_factory=dict)  # answers seen before their statements

    @property
    def format(self) -> str:
        kinds = {i.item_type for i in self.items}
        if not kinds:
            return "other"
        if kinds == {"true_false"}:
            return "true_false"
        if kinds == {"single_choice"}:
            return "numeric_choice" if all(i.attrs.get("numeric") for i in self.items) else "single_choice"
        if kinds == {"gap"}:
            return "gap_fill"
        return "mixed"

    @property
    def needs_review(self) -> bool:
        return bool(self.notes)


@dataclass
class ParsedPaper:
    spec: "PaperSpec"
    questions: list[PQuestion]
    preamble: str
    pages: list[PageLayout]
    body_size: float


# ------------------------------------------------------------------- spec ---

@dataclass
class PaperSpec:
    year: int
    paper_code: str
    title: str
    lang: str
    path: str  # questions PDF
    host_country: str | None = None
    page_range: tuple[int, int] | None = None  # 1-based inclusive
    text_layer: str = "native"
    doc_role: str = "questions"
    # recognisers -----------------------------------------------------------
    question_start: Callable[[Line, dict], tuple | None] | None = None
    section: Callable[[Line], str | None] | None = None
    drop: list[str] = field(default_factory=list)  # regexes of lines to discard
    instruction: str | None = r"(\bindicate\b.{0,60}\b(if|whether|for) (each|the following)|Для каждого утверждения|^Укажите)"
    statement_label: str = r"^([A-H])\s*[\.\)]\s*(.*)$"
    statement_label_max_x: float | None = None  # label must start left of this
    statements_without_instruction: bool = False  # "A." in the stem opens the statements
    statements_need_header: bool = False  # statements only after a "True False" header
    infer_missing_markers: bool = False  # split on an instruction found inside an explanation
    tf_header: str | None = None  # line that opens an unlabeled T/F list ("TRUE FALSE")
    inline_answers: str | None = None  # a line holding all answers "A. True B. False ..."
    answer_key_start: str | None = None  # "Answer key:" block
    explanation_start: str | None = None
    item_explanation: str | None = None  # groups: label, verdict, rest
    item_expl_numeric: bool = False  # item explanation labels are 1,2,3 instead of A,B,C
    reference_start: str = r"^(References?|Reference:|Источник)\b\s*:?\s*(.*)$"
    note_start: str | None = r"^(Notes)\s*$"
    choice_prompt: str | None = None  # "Choose the nearest ... " (2017)
    gap_instruction: str | None = None
    scoring: dict | None = None
    duration_minutes: int | None = None
    total_points: float | None = None
    # hooks ------------------------------------------------------------------
    postprocess: Callable[["ParsedPaper"], None] | None = None
    answer_keys: list[Callable[[ParsedPaper], None]] = field(default_factory=list)
    extra_docs: list[tuple[str, str]] = field(default_factory=list)  # (path, role)


# ------------------------------------------------------------- utilities ---

def load_pages(spec: PaperSpec, base_dir) -> tuple[pymupdf.Document, list[PageLayout], float]:
    doc = pymupdf.open(str(base_dir / spec.path))
    bs = body_size_of(doc)
    first, last = spec.page_range or (1, None)
    last = last or len(doc)
    pages = [extract_page(doc[i - 1], i, bs, protect=lambda ln: _structural(spec, ln))
             for i in range(first, last + 1)]
    _strip_furniture(pages, spec)
    return doc, pages, bs


def _strip_furniture(pages: list[PageLayout], spec: PaperSpec | None = None):
    """Remove running headers/footers: text repeated in the top/bottom margin."""
    c = Counter()
    for p in pages:
        seen = set()
        for ln in p.lines:
            if ln.y1 < 0.11 * p.height or ln.y0 > 0.9 * p.height:
                seen.add(normalize_for_repeat(ln.text))
        c.update(seen)
    threshold = max(3, int(0.3 * len(pages)))
    repeated = {k for k, v in c.items() if v >= threshold}
    for p in pages:
        keep = []
        for ln in p.lines:
            margin = ln.y1 < 0.11 * p.height or ln.y0 > 0.9 * p.height
            norm = normalize_for_repeat(ln.text)
            if margin and spec is not None and _structural(spec, ln):
                keep.append(ln)
                continue
            if margin and (norm in repeated or re.fullmatch(r"[#\s/\-–]*(page|стр\.?)?[#\s/]*", norm)):
                continue
            keep.append(ln)
        p.lines = keep


def _structural(spec: PaperSpec, ln: Line) -> bool:
    try:
        if spec.question_start and spec.question_start(ln, {"next": None, "section": None, "probe": True}):
            return True
    except Exception:  # noqa: BLE001 - probes only
        pass
    if spec.section and spec.section(ln):
        return True
    t = ln.text.strip()
    for rx in (spec.instruction, spec.explanation_start, spec.answer_key_start, spec.choice_prompt,
               spec.inline_answers, spec.item_explanation):
        if rx and re.search(rx, t, re.I):
            return True
    return bool(spec.tf_header and re.fullmatch(spec.tf_header, t, re.I))


def stream(pages: list[PageLayout]):
    for p in pages:
        toks = [(ln.y0, ln.x0, ln) for ln in p.lines] + [(g.y0, g.x0, g) for g in p.graphics]
        toks.sort(key=lambda t: (t[0], t[1]))
        for _, _, tok in toks:
            yield tok


def join_rich(a: str, b: str) -> str:
    if not a:
        return b
    pa = strip_markup(a)
    if pa.endswith("-") and len(pa) > 1 and pa[-2].isalpha():
        return a + b
    return a + " " + b


# ----------------------------------------------------------------- parser ---

class _QState:
    def __init__(self, q: PQuestion, spec: PaperSpec, body_size: float):
        self.q = q
        self.spec = spec
        self.bs = body_size
        self.zone = "stem"
        self.cur_para: PElement | None = None
        self.cur_item: PItem | None = None
        self.cur_expl: PExplanation | None = None
        self.answer_buf: list[str] = []
        self.tf_unlabeled = False
        self.expl_bullet_idx = 0
        self.group = 1  # statement group (a question with two T/F blocks: 1A..1E, 2A..2D)
        self.group_start = 0
        self.maybe_new_group = False

    def new_group(self):
        """A second T/F block in the same question restarts at A."""
        if self.group == 1:
            for it in self.q.items:
                it.label = "1" + it.label
        self.group += 1
        self.group_start = len(self.q.items)

    def next_label(self, letter: str | None = None) -> str:
        k = len(self.q.items) - self.group_start
        letter = letter or (LETTERS[k] if k < len(LETTERS) else str(k + 1))
        return f"{self.group}{letter}" if self.group > 1 else letter

    def expected_letter(self) -> str | None:
        k = len(self.q.items) - self.group_start
        return LETTERS[k] if k < len(LETTERS) else None


def parse_paper(spec: PaperSpec, base_dir) -> ParsedPaper:
    doc, pages, bs = load_pages(spec, base_dir)
    ctx: dict = {"next": 1, "section": None}
    questions: list[PQuestion] = []
    preamble: list[str] = []
    st: _QState | None = None
    drops = [re.compile(r, re.I) for r in spec.drop]

    for tok in stream(pages):
        if isinstance(tok, Line):
            if any(d.search(tok.text) for d in drops):
                continue
            if spec.section:
                sec = spec.section(tok)
                if sec:
                    ctx["section"] = sec
                    if st is not None and not st.q.lines:
                        st.q.section = sec
                    continue
            qs = spec.question_start(tok, ctx) if spec.question_start else None
            if qs:
                number, label, title, rest = (list(qs) + [None, None, None])[:4]
                if st:
                    _finish(st)
                q = PQuestion(number=number, label=label, title=title, section=ctx["section"],
                              page_start=tok.page, page_end=tok.page)
                ctx["next"] = number + 1
                questions.append(q)
                st = _QState(q, spec, bs)
                if rest:
                    _feed_line(st, Line(tok.page, tok.x0 + 30, tok.y0, tok.x1, tok.y1, strip_markup(rest), rest,
                                        tok.size, tok.bold, tok.font))
                continue
            if st is None:
                preamble.append(tok.text)
                continue
            if (spec.infer_missing_markers and st.zone in ("explanation", "reference", "answer_key")
                    and spec.instruction and re.search(spec.instruction, tok.text, re.I) and st.q.items):
                # A new question's instruction inside an explanation: its number
                # was lost (OCR). Split here and move the trailing explanation
                # paragraphs (after the last reference) back into the new stem.
                _finish(st)
                n = ctx["next"]
                q = PQuestion(number=n, label=f"{n}", section=ctx["section"],
                              page_start=tok.page, page_end=tok.page)
                q.notes.append("question number not found in the text layer; start inferred")
                _move_trailing_explanations(st.q, q)
                ctx["next"] = n + 1
                questions.append(q)
                st = _QState(q, spec, bs)
            st.q.page_end = tok.page
            st.q.lines.append(tok)
            _feed_line(st, tok)
        else:
            if st is None:
                continue
            st.q.page_end = tok.page
            st.q.lines.append(tok)
            _feed_graphic(st, tok)
    if st:
        _finish(st)

    paper = ParsedPaper(spec, questions, "\n".join(preamble), pages, bs)
    paper.doc = doc  # type: ignore[attr-defined]
    for q in questions:
        _settle(q)
    for hook in spec.answer_keys:
        hook(paper)
    if spec.postprocess:
        spec.postprocess(paper)
    for q in questions:
        _validate(q)
    return paper


def _close_para(st: _QState):
    st.cur_para = None


def _add_element(st: _QState, el: PElement, to_expl: bool = False):
    if to_expl and st.cur_expl is not None:
        st.cur_expl.elements.append(el)
    else:
        st.q.elements.append(el)
    return el


def _last_figure_index(q: PQuestion, page: int) -> int | None:
    for i in range(len(q.elements) - 1, -1, -1):
        e = q.elements[i]
        if e.role == "figure" and e.page == page:
            return i
        if e.role in ("paragraph",) and e.page == page and len(e.plain) > 120:
            break
    return None


def _is_continuation(prev: Line | None, ln: Line, size_tol=0.15) -> bool:
    if prev is None or prev.page != ln.page:
        return prev is not None and prev.page + 1 == ln.page  # page break mid-paragraph
    gap = ln.y0 - prev.y1
    if gap > 0.75 * max(prev.h, 6):
        return False
    if abs(ln.size - prev.size) > size_tol * max(prev.size, 1):
        return False
    return True


def _feed_text(st: _QState, ln: Line, role="paragraph", to_expl=False):
    """Append a line to the open paragraph, or open a new one."""
    para = st.cur_para
    if para is not None and para.role == role and _is_continuation(para.last_line, ln):
        para.rich = join_rich(para.rich, ln.rich)
        para.last_line = ln
        para.bbox = (min(para.bbox[0], ln.x0), para.bbox[1], max(para.bbox[2], ln.x1), ln.y1) if para.page == ln.page else para.bbox
        return para
    el = PElement(role=role, rich=ln.rich, page=ln.page, bbox=ln.bbox, size=ln.size, last_line=ln)
    st.cur_para = _add_element(st, el, to_expl)
    if role == "legend":
        fi = _last_figure_index(st.q, ln.page)
        if fi is not None:
            el.parent = fi
    return el


def _new_item(st: _QState, label: str, rich: str, ln: Line, x0: float):
    st.cur_item = PItem(label=label, rich=rich, text_x0=x0, last_line=ln)
    st.q.items.append(st.cur_item)
    _close_para(st)


def _feed_line(st: _QState, ln: Line):
    spec, q = st.spec, st.q
    t = ln.text.strip()

    if spec.choice_prompt and t in ("X", "x"):
        return  # stray answer marks of choice items; read from geometry instead

    # ---- answers / explanations (any zone) --------------------------------
    if spec.inline_answers and re.search(spec.inline_answers, t, re.I):
        for lab, word in re.findall(r"\b([A-H])\s*[\.\):]?\s*(True|False|T|F|Верно|Неверно)\b", t, re.I):
            b = to_bool(word)
            it = next((i for i in q.items if i.label == lab.upper()), None)
            if it is not None and b is not None:
                it.answer, it.answer_text, it.answer_source = {"bool": b}, "True" if b else "False", "inline"
        st.zone = "answers"
        _close_para(st)
        return
    if spec.answer_key_start and re.search(spec.answer_key_start, t, re.I):
        st.zone = "answer_key"
        st.answer_buf = [t]
        _close_para(st)
        return
    if spec.explanation_start and re.search(spec.explanation_start, t, re.I):
        _flush_answer_key(st)
        st.zone = "explanation"
        st.cur_expl = None
        st.expl_bullet_idx = 0
        _close_para(st)
        rest = re.sub(spec.explanation_start, "", t, flags=re.I).strip(" :")
        if rest:
            _expl_text(st, ln, rest)
        return
    if st.zone == "answer_key":
        if parse_answer_run(t) and len(st.answer_buf) < 4:
            st.answer_buf.append(t)
            return
        _flush_answer_key(st)
        st.zone = "explanation"
    m = re.match(spec.reference_start, t, re.I)
    if m and st.zone in ("explanation", "answers", "items", "reference"):
        st.zone = "reference"
        st.cur_expl = PExplanation(None, "reference", m.group(2) or "")
        q.explanations.append(st.cur_expl)
        return
    if st.zone == "reference":
        st.cur_expl.rich = (st.cur_expl.rich + "\n" + ln.rich).strip()
        return

    if st.zone in ("explanation", "answers"):
        if st.zone == "answers":
            st.zone = "explanation"
        _expl_line(st, ln)
        return

    # ---- stem / items -------------------------------------------------------
    if spec.instruction and re.search(spec.instruction, t, re.I):
        st.zone = "items"
        st.cur_item = None
        _close_para(st)
        _add_element(st, PElement(role="instruction", rich=ln.rich, page=ln.page, bbox=ln.bbox))
        return
    if spec.tf_header and re.fullmatch(spec.tf_header, t, re.I):
        st.maybe_new_group = len(q.items) > st.group_start
        st.zone = "items"
        st.tf_unlabeled = True
        st.cur_item = None
        _close_para(st)
        return
    if spec.choice_prompt and re.search(spec.choice_prompt, t, re.I):
        _choice_item(st, ln)
        return

    m = re.match(spec.statement_label, t)
    left_ok = spec.statement_label_max_x is None or ln.x0 <= spec.statement_label_max_x
    if m and left_ok and (st.zone == "items" or _looks_like_statement_run(st, m.group(1))):
        lab = m.group(1).upper()
        if lab == "A" and st.maybe_new_group and len(q.items) > st.group_start:
            st.new_group()
        st.maybe_new_group = False
        expected = st.expected_letter()
        last = q.items[-1].label[-1] if len(q.items) > st.group_start else None
        if lab == expected or (last and lab != last and lab > last):
            st.zone = "items"
            rich = re.sub(r"^(<b>)?\s*[A-H]\s*[\.\)]\s*(</b>)?\s*", "", ln.rich)
            # x of the statement text, for hanging-indent continuation lines
            _new_item(st, st.next_label(lab), rich, ln, ln.x0 + 10)
            return

    if st.zone == "items":
        it = st.cur_item
        if st.tf_unlabeled:
            if it is not None and _is_continuation(it.last_line, ln) and abs(ln.x0 - it.text_x0) < 25:
                it.rich = join_rich(it.rich, ln.rich)
                it.last_line = ln
            else:
                _new_item(st, st.next_label(), ln.rich, ln, ln.x0)
            return
        if it is not None:
            if _is_continuation(it.last_line, ln) and ln.x0 >= it.last_line.x0 - 4:
                it.rich = join_rich(it.rich, ln.rich)
                it.last_line = ln
                return
            # non-continuation text after statements -> appendix
            _feed_text(st, ln, role="appendix")
            return
        # text between instruction and first statement -> lead-in
        _feed_text(st, ln, role="statement_lead")
        return

    # stem
    role = "paragraph"
    if CAPTION_RE.match(t):
        role = "legend"
        _close_para(st)
    elif st.cur_para is not None and st.cur_para.role == "legend" and _is_continuation(st.cur_para.last_line, ln, 0.3):
        role = "legend"
    elif _legend_like(st, ln):
        role = "legend"
    _feed_text(st, ln, role=role)


def _looks_like_statement_run(st: _QState, lab: str) -> bool:
    if st.spec.statements_need_header:
        return False
    # A bare "A." in the stem only opens statements when it is the first letter
    # and the paper has no explicit instruction line before statements.
    return lab.upper() == "A" and (st.spec.instruction is None or bool(st.spec.inline_answers)
                                   or st.spec.statements_without_instruction)


def _legend_like(st: _QState, ln: Line) -> bool:
    """Small-print text right after a figure, or a label glossary 'Matrix - Матрикс'."""
    q = st.q
    if not q.elements:
        return False
    last = q.elements[-1]
    near_fig = (last.role == "figure" and last.page == ln.page and ln.y0 - last.bbox[3] < 30) or (
        last.role == "legend")
    if not near_fig:
        return False
    if ln.size < 0.92 * st.bs:
        return True
    if re.match(r"^(Ось\s+[XYХУ]|Внутри рисунка|Обозначения|Условные обозначения)", ln.text):
        return True
    return bool(re.match(r"^[A-Za-z][\w\s,()'-]{0,40}\s[-–—]\s+[А-Яа-яЁё]", ln.text))


_OCR_TRUE = r"T\s*r?\s*[uün]?\s*[eo]|Tme|Tnie|Trne"
_OCR_FALSE = r"F\s*a\s*[l1I]\s*s\s*e"


def parse_answer_run(txt: str) -> dict[str, bool]:
    """'A.TrueB.False; C- True D) False' -> {'A': True, ...}; tolerant of OCR."""
    txt = re.sub(r"(?<=[a-z])(?=[A-H]\s*[\.,:;\)\-–])", " ", txt)
    out = {}
    for lab, word in re.findall(
            rf"(?<![A-Za-z])([A-H])\s*[\.,:;\)\-–]?\s*({_OCR_TRUE}|{_OCR_FALSE}|True|False)\b", txt):
        out[lab] = not re.match(_OCR_FALSE + "|False", word)
    return out


def _flush_answer_key(st: _QState):
    if not st.answer_buf:
        return
    txt = " ".join(st.answer_buf)
    found = 0
    for lab, b in parse_answer_run(txt).items():
        it = next((i for i in st.q.items if i.label == lab), None)
        if it is not None:
            if it.answer is not None and it.answer.get("bool") != b and it.answer_source == "inline":
                st.q.notes.append(f"statement {lab}: two answer keys disagree")
            it.answer, it.answer_text, it.answer_source = {"bool": b}, "True" if b else "False", "inline"
            found += 1
        else:
            st.q.pending_answers.setdefault(lab, (b, "inline"))
    if not found and st.q.items:
        st.q.notes.append(f"answer key line not understood: {txt[:80]!r}")
    st.answer_buf = []


def _expl_text(st: _QState, ln: Line, rich: str):
    e = st.cur_expl
    if e is not None and e.kind != "reference" and st.cur_para is None:
        e.rich = join_rich(e.rich, rich) if not _paragraph_break(e, ln) else e.rich + "\n" + rich
    else:
        st.cur_expl = PExplanation(None, "explanation", rich)
        st.q.explanations.append(st.cur_expl)
    st.cur_expl._last = ln  # type: ignore[attr-defined]
    _close_para(st)


def _paragraph_break(e: PExplanation, ln: Line) -> bool:
    last = getattr(e, "_last", None)
    return last is not None and not _is_continuation(last, ln, 0.3)


def _expl_line(st: _QState, ln: Line):
    spec, q = st.spec, st.q
    t = ln.text.strip()
    if spec.note_start and re.match(spec.note_start, t, re.I):
        st.cur_expl = PExplanation(None, "note", "")
        q.explanations.append(st.cur_expl)
        _close_para(st)
        return
    if re.fullmatch(r"(Correct answers|Original commentary)\s*", t, re.I):
        return
    if spec.item_explanation:
        m = re.match(spec.item_explanation, t, re.I)
        if m:
            lab, verdict, rest = m.group(1), m.group(2), (m.group(3) or "").strip()
            if spec.item_expl_numeric or lab.isdigit():
                idx = int(lab) - 1
                lab = q.items[idx].label if 0 <= idx < len(q.items) else LETTERS[idx] if idx < 8 else lab
            lab = lab.upper()
            it = next((i for i in q.items if i.label == lab), None)
            b = to_bool(verdict) if verdict else None
            if it is not None and b is not None:
                if it.answer is None:
                    it.answer, it.answer_text, it.answer_source = {"bool": b}, "True" if b else "False", "explanation"
                elif it.answer.get("bool") != b:
                    q.notes.append(f"statement {lab}: key says {it.answer_text}, explanation says {verdict}")
            elif it is None and b is not None:
                q.pending_answers.setdefault(lab, (b, "explanation"))
            st.cur_expl = PExplanation(lab, "explanation", _rest_rich(ln.rich, rest))
            st.cur_expl._last = ln  # type: ignore[attr-defined]
            q.explanations.append(st.cur_expl)
            _close_para(st)
            return
    if t.startswith("•"):
        # bulleted explanations map to statements by position (2017 papers)
        idx = st.expl_bullet_idx
        st.expl_bullet_idx += 1
        lab = q.items[idx].label if idx < len(q.items) else None
        st.cur_expl = PExplanation(lab, "explanation", ln.rich.lstrip("• ").strip())
        st.cur_expl._last = ln  # type: ignore[attr-defined]
        q.explanations.append(st.cur_expl)
        _close_para(st)
        return
    e = st.cur_expl
    if e is not None and e.kind in ("explanation", "note"):
        last = getattr(e, "_last", None)
        if last is not None and _is_continuation(last, ln, 0.3):
            e.rich = join_rich(e.rich, ln.rich)
        elif e.item_label is not None and last is not None and ln.x0 >= last.x0 - 3 and ln.y0 - last.y1 < 2.5 * max(last.h, 6):
            e.rich = e.rich + "\n" + ln.rich
        elif e.item_label is None:
            e.rich = (e.rich + "\n" + ln.rich).strip()
        else:
            st.cur_expl = PExplanation(None, "explanation", ln.rich)
            q.explanations.append(st.cur_expl)
        st.cur_expl._last = ln  # type: ignore[attr-defined]
        return
    st.cur_expl = PExplanation(None, "explanation", ln.rich)
    st.cur_expl._last = ln  # type: ignore[attr-defined]
    q.explanations.append(st.cur_expl)


def _rest_rich(rich: str, rest_plain: str) -> str:
    """Best effort: keep markup of the remainder of a line after its prefix."""
    if not rest_plain:
        return ""
    plain = strip_markup(rich)
    cut = plain.find(rest_plain[:12])
    if cut <= 0:
        return rest_plain
    # walk the rich string until `cut` visible characters are consumed
    vis = 0
    i = 0
    while i < len(rich) and vis < cut:
        if rich[i] == "<":
            i = rich.index(">", i) + 1
            continue
        if rich[i] == "&":
            i = rich.index(";", i) + 1
        else:
            i += 1
        vis += 1
    return re.sub(r"^(</[ib]>)+", "", rich[i:]).strip()


def _choice_item(st: _QState, ln: Line):
    """Numeric single choice (2017): options row above the 'Choose the nearest' line,
    the correct option marked by an X under it on the same row as the prompt."""
    q = st.q
    st.zone = "items"
    st.tf_unlabeled = False
    item = PItem(label=str(len(q.items) + 1), item_type="single_choice", rich=ln.rich, last_line=ln)
    item.attrs["numeric"] = True
    item.attrs["pending_options"] = True
    item.attrs["prompt_line"] = (ln.page, ln.y0, ln.y1)
    q.items.append(item)
    st.cur_item = None
    _close_para(st)


def _feed_graphic(st: _QState, g: Graphic):
    q = st.q
    if g.kind == "table":
        rows = g.rows or []
        hdr = [c.strip().lower() for c in rows[0]] if rows else []
        if len(hdr) == 2 and hdr[-1] == "true":
            # the table finder lost the False column: an empty True cell means False
            rows = [r + [""] if i else r + ["false"] for i, r in enumerate(rows)]
            rows = [r[:-1] + ([("X" if not r[-2].strip() else "")] if i else ["false"]) for i, r in enumerate(rows)]
            hdr = hdr + ["false"]
            q.notes.append("T/F table had no False column; empty True cells read as False")
        if len(hdr) >= 3 and hdr[-2] == "true" and hdr[-1][:1] == "f":  # "Fasle" occurs
            # T/F statement table with X marks (2017)
            st.zone = "items"
            first_lab = next((re.match(r"^([A-H])\s*[\.\)]", " ".join(r[:-2]).strip()) for r in rows[1:]
                              if re.match(r"^[A-H]\s*[\.\)]", " ".join(r[:-2]).strip())), None)
            if first_lab and first_lab.group(1) == "A" and len(q.items) > st.group_start:
                st.new_group()
            first_new = len(q.items)
            has_marks = any(c.strip().upper().startswith("X") for r in rows[1:] for c in r[-2:])
            has_labels = any(re.match(r"^[A-H]\s*[\.\)]", " ".join(r[:-2]).strip()) for r in rows[1:])
            for r in rows[1:]:
                text = " ".join(c.strip() for c in r[:-2] if c.strip())
                printed = re.match(r"^([A-H])\s*[\.\)]\s*(.*)$", text, re.S)
                if printed:  # 2018: a label column "A." before the statement column
                    text = printed.group(2)
                    if printed.group(1) == "A" and len(q.items) > max(st.group_start, first_new - 0)                             and len(q.items) > st.group_start:
                        st.new_group()  # a second block of statements in the same table
                tv, fv = r[-2].strip().upper(), r[-1].strip().upper()
                if tv.lower() == "true" and fv.lower().startswith("f"):
                    continue  # repeated header row
                if not text and not (tv or fv):
                    continue
                if (not (tv or fv) and len(q.items) > first_new and not printed
                        and (has_marks or has_labels)):
                    # a statement wrapped into the next table row
                    prev = q.items[-1]
                    prev.rich = join_rich(prev.rich, text)
                    continue
                if not text and len(q.items) > first_new and q.items[-1].answer is None:
                    it = q.items[-1]  # the mark sits on the wrapped row
                else:
                    it = PItem(label=st.next_label(), rich=text)
                    q.items.append(it)
                if tv.startswith("X") != fv.startswith("X"):
                    b = tv.startswith("X")
                    it.answer, it.answer_text, it.answer_source = {"bool": b}, "True" if b else "False", "x_mark"
            st.cur_item = None
            _close_para(st)
            return
        el = PElement(role="table", page=g.page, bbox=g.bbox, graphic=g,
                      attrs={"rows": rows})
        el.rich = "\n".join(" | ".join(r) for r in rows)
        _add_element(st, el, to_expl=st.zone in ("explanation", "answers", "reference"))
        _close_para(st)
        return
    el = PElement(role="figure", page=g.page, bbox=g.bbox, graphic=g,
                  attrs={"labels": g.labels, "detected_by": g.source})
    in_expl = st.zone in ("explanation", "answers", "reference")
    if in_expl and st.cur_expl is None:
        st.cur_expl = PExplanation(None, "explanation", "")
        q.explanations.append(st.cur_expl)
    if st.zone == "items" and st.cur_item is not None:
        el.role = "figure"
        el.attrs["after_item"] = st.cur_item.label
    _add_element(st, el, to_expl=in_expl)
    _close_para(st)


def _finish(st: _QState):
    _flush_answer_key(st)
    q = st.q
    # attach captions that precede their figure (caption above figure)
    for i, el in enumerate(q.elements):
        if el.role == "legend" and el.parent is None:
            nxt = q.elements[i + 1] if i + 1 < len(q.elements) else None
            if nxt is not None and nxt.role == "figure":
                el.parent = i + 1
            elif CAPTION_RE.match(el.plain) and re.match(r"^\s*(Table|Таблица)", el.plain, re.I):
                if nxt is not None and nxt.role == "table":
                    el.parent = i + 1
    for e in q.explanations:
        e.rich = e.rich.strip()
    q.explanations = [e for e in q.explanations if e.rich or e.elements]


def _move_trailing_explanations(prev: PQuestion, new: PQuestion):
    last_ref = max((i for i, e in enumerate(prev.explanations) if e.kind == "reference"), default=None)
    if last_ref is None:
        new.notes.append("stem may still contain the end of the previous explanation")
        return
    moved = prev.explanations[last_ref + 1:]
    prev.explanations = prev.explanations[: last_ref + 1]
    for e in moved:
        for el in e.elements:
            new.elements.append(el)
        if e.rich:
            new.elements.append(PElement(role="paragraph", rich=e.rich))
    if prev.page_end and new.page_start:
        new.page_start = min(new.page_start, prev.page_end)


_INLINE_STMTS = re.compile(
    r"^(?P<lead>.*?)(?:(?<=\s)|^)A\s*[\.\)]\s+(?P<A>.+?)\s+B\s*[\.\)]\s+(?P<B>.+?)\s+C\s*[\.\)]\s+(?P<C>.+?)"
    r"\s+D\s*[\.\)]\s+(?P<D>.+?)(?:\s+E\s*[\.\)]\s+(?P<E>.+?))?\s*$", re.S)


def _recover_inline_statements(q: PQuestion):
    """OCR sometimes glues statements into running text ('... true or false. A. x
    B. y'). Look for an A-B-C-D run in the trailing text blocks of the stem."""
    tail = []
    for i in range(len(q.elements) - 1, -1, -1):
        if q.elements[i].role in ("figure", "table", "legend"):
            break
        tail.insert(0, i)
    if not tail:
        return
    text = " ".join(q.elements[i].plain for i in tail)
    m = _INLINE_STMTS.match(text)
    if not m:
        return
    keep = [e for i, e in enumerate(q.elements) if i not in tail]
    lead = m.group("lead").strip()
    if lead:
        keep.append(PElement(role="paragraph", rich=lead, attrs={"recovered": True}))
    q.elements = keep
    for lab in "ABCDE":
        if m.group(lab):
            it = PItem(label=lab, rich=m.group(lab).strip())
            q.items.append(it)
    q.notes.append("statements recovered from running text (OCR merged lines)")


def _settle(q: PQuestion):
    """Recover glued statements, then apply answers that arrived before them."""
    if not q.items:
        _recover_inline_statements(q)
    for lab, (b, src) in q.pending_answers.items():
        it = next((i for i in q.items if i.label == lab), None)
        if it is not None and it.answer is None:
            it.answer, it.answer_text, it.answer_source = {"bool": b}, "True" if b else "False", src


def _validate(q: PQuestion):
    if not q.items and q.status != "cancelled":
        q.notes.append("no answerable items found")
    tf = [i for i in q.items if i.item_type == "true_false"]
    if tf and q.status == "active":
        missing = [i.label for i in tf if i.answer is None]
        if missing:
            q.notes.append("no answer for statements " + ",".join(missing))
    groups: dict[str, int] = {}
    for i in tf:
        groups[i.label[:-1]] = groups.get(i.label[:-1], 0) + 1
    for g, n in groups.items():
        if not (3 <= n <= 6):
            q.notes.append(f"unusual statement count: {n}" + (f" in block {g}" if g else ""))
    for i in q.items:
        if i.item_type == "single_choice" and not i.options:
            q.notes.append(f"item {i.label}: options not found")
        if not i.plain.strip():
            q.notes.append(f"item {i.label}: empty text")
