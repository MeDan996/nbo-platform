"""Answer keys that live outside the question PDF, read by word geometry."""
from __future__ import annotations

import re
import statistics
from pathlib import Path

import pymupdf

from app.archive.layout import strip_markup
from app.archive.parse import LETTERS, ParsedPaper, to_bool

BASE = Path(__file__).resolve().parents[2]


def _words(page):
    return [(w[0], w[1], w[2], w[3], w[4]) for w in page.get_text("words")]


def _xc(w):
    return (w[0] + w[2]) / 2


def _yc(w):
    return (w[1] + w[3]) / 2


def _apply(paper: ParsedPaper, key: dict[int, dict[str, bool]], source: str):
    by_num = {q.number: q for q in paper.questions}
    for n, answers in key.items():
        q = by_num.get(n)
        if q is None:
            continue
        for it in q.items:
            if it.label in answers:
                b = answers[it.label]
                if it.answer is not None and it.answer.get("bool") != b:
                    q.notes.append(f"statement {it.label}: {it.answer_source} says {it.answer_text}, "
                                   f"{source} says {'True' if b else 'False'}; {source} kept as official")
                    # keep the losing answer: it becomes a 'superseded' answer row
                    it.attrs.setdefault("alt_answers", []).append(
                        {"value": it.answer, "text": it.answer_text, "source": it.answer_source})
                it.answer, it.answer_text, it.answer_source = {"bool": b}, "True" if b else "False", source
        extra = set(answers) - {i.label for i in q.items}
        if extra:
            q.notes.append(f"answer key has statements {sorted(extra)} not found in the question")


# ------------------------------------------------------------------ 2014 ---

def key_2014(paper: ParsedPaper, part: str):
    """Answer sheet grid: four column groups of 13 questions, each 4 rows A-D,
    T or F in its column. Question numbers are not in the text layer, so they
    are derived from the row slot; blacked-out (cancelled) questions leave an
    empty slot."""
    doc = pymupdf.open(BASE / paper.spec.extra_docs[0][0])
    page = doc[1 if part == "A" else 2]
    words = _words(page)
    heads = sorted([w for w in words if w[4] == "Statement"], key=_xc)
    groups = []
    for h in heads:
        tf = [w for w in words if w[4] in ("T", "F") and abs(_yc(w) - _yc(h)) < 3 and w[0] > h[2]]
        tf.sort(key=_xc)
        t_head = next(w for w in tf if w[4] == "T")
        f_head = next(w for w in tf if w[4] == "F")
        groups.append((h, t_head, f_head))
    rows_by_group = []
    for h, th, fh in groups:
        letters = [w for w in words if w[4] in "ABCD" and len(w[4]) == 1 and abs(_xc(w) - _xc(h)) < 18 and _yc(w) > _yc(h) + 5]
        letters.sort(key=_yc)
        rows = []
        for lw in letters:
            marks = [w for w in words if w[4] in ("T", "F") and abs(_yc(w) - _yc(lw)) < 3
                     and min(abs(_xc(w) - _xc(th)), abs(_xc(w) - _xc(fh))) < 8]
            val = None
            if marks:
                m = min(marks, key=lambda w: min(abs(_xc(w) - _xc(th)), abs(_xc(w) - _xc(fh))))
                val = abs(_xc(m) - _xc(th)) < abs(_xc(m) - _xc(fh))
            rows.append((_yc(lw), lw[4], val))
        rows_by_group.append(rows)
    a_rows = [r for rows in rows_by_group for r in rows if r[1] == "A"]
    y_first = min(r[0] for r in a_rows)
    a_ys = sorted({round(r[0]) for r in a_rows})
    pitch = statistics.median([b - a for a, b in zip(a_ys, a_ys[1:]) if b - a > 5])
    per_col = 13
    numbers = [w for w in words if re.fullmatch(r"\d{1,2}\.", w[4])]
    key: dict[int, dict[str, bool]] = {}
    for gi, rows in enumerate(rows_by_group):
        h = groups[gi][0]
        cur = None
        for idx, (yc, lab, val) in enumerate(rows):
            if lab == "A":
                block = [r for r in rows[idx: idx + 4]]
                y_lo, y_hi = block[0][0] - 3, block[-1][0] + 3
                printed = [w for w in numbers if y_lo <= _yc(w) <= y_hi and w[2] <= h[0] + 2
                           and (gi == 0 or w[0] > groups[gi - 1][2][2])]
                if printed:
                    cur = int(printed[0][4].rstrip("."))
                else:  # number not in the text layer: derive it from the row slot
                    cur = gi * per_col + round((yc - y_first) / pitch) + 1
                key[cur] = {}
            if cur is not None and val is not None:
                key[cur][lab] = val
    cancelled = {n for n, v in key.items() if not v}
    key = {n: v for n, v in key.items() if v}
    _apply(paper, key, "answer_key")
    for q in paper.questions:
        if q.number in cancelled or q.number not in key:
            q.status = "cancelled"
            q.notes.append("blacked out on the official answer sheet (cancelled)")
    paper.key_numbers = sorted(key)  # type: ignore[attr-defined]


# ------------------------------------------------------------------ 2016 ---

def key_2016(paper: ParsedPaper):
    """Page 2 of each 2016 PDF is a typed key: 'Q. A B C D' columns, ✓ / ✕.
    Gap-filling questions have numbers/codes instead of marks; those cells are
    kept as text answers."""
    doc = paper.doc  # type: ignore[attr-defined]
    words = _words(doc[1])
    heads = [w for w in words if w[4] in ("A", "B", "C", "D")]
    if not heads:
        return
    hy = statistics.mode(round(_yc(w)) for w in heads)
    heads = sorted([w for w in heads if abs(_yc(w) - hy) < 3], key=_xc)
    # two side-by-side tables -> two sets of A-D columns
    col_sets = [heads[i:i + 4] for i in range(0, len(heads), 4)]
    key: dict[int, dict[str, bool]] = {}
    text_key: dict[int, dict[str, str]] = {}
    for cols in col_sets:
        left = cols[0][0] - 60
        nums = [w for w in words if re.fullmatch(r"\d{1,3}", w[4]) and left < w[0] < cols[0][0] - 3
                and _yc(w) > hy + 5]
        for nw in nums:
            n = int(nw[4])
            for c, lab in zip(cols, "ABCD"):
                cell = [w for w in words if abs(_yc(w) - _yc(nw)) < 4 and abs(_xc(w) - _xc(c)) < 12]
                if not cell:
                    continue
                t = " ".join(w[4] for w in cell)
                if t in ("✓", "✔", "√"):
                    key.setdefault(n, {})[lab] = True
                elif t in ("✕", "✗", "×", "x", "X"):
                    key.setdefault(n, {})[lab] = False
                else:
                    text_key.setdefault(n, {})[lab] = t
    _apply(paper, key, "answer_key")
    by_num = {q.number: q for q in paper.questions}
    for n, cells in text_key.items():
        q = by_num.get(n)
        if q is None:
            continue
        q.notes.append(f"answer key has non-T/F entries {cells} (gap-filling question?)")
        for it in q.items:
            if it.label in cells:
                it.item_type = "gap"
                it.answer, it.answer_text, it.answer_source = {"text": cells[it.label]}, cells[it.label], "answer_key"
    parsed = set(by_num)
    for n in sorted(set(key) | set(text_key)):
        if n not in parsed:
            prev = max((m for m in parsed if m < n), default=None)
            if prev is not None:
                by_num[prev].notes.append(f"question {n} is in the answer key but its start was not found; "
                                          f"it is probably merged into this one")


# ------------------------------------------------------------------ 2018 ---

def key_2018(paper: ParsedPaper, part: str):
    doc = pymupdf.open(BASE / paper.spec.extra_docs[0][0])
    key: dict[int, dict[str, bool]] = {}
    half = 0 if part == "1" else 1
    cols: dict[str, float] = {}
    for page in doc:
        words = _words(page)
        mid = page.rect.width / 2 + 10
        hdr = [w for w in words if w[4] in "ABCDE" and len(w[4]) == 1]
        ys = -1.0
        if hdr:
            ys = statistics.mode(round(_yc(w)) for w in hdr)
            row_hdr = [w for w in hdr if abs(_yc(w) - ys) < 3 and ((_xc(w) < mid) == (half == 0))]
            if len(row_hdr) >= 4:
                cols = {w[4]: _xc(w) for w in row_hdr}
            else:
                ys = -1.0
        if not cols:
            continue  # continuation pages reuse the header columns of page 1
        side = [w for w in words if (_xc(w) < mid) == (half == 0) and _yc(w) > ys + 5]
        nums = [w for w in side if re.fullmatch(r"\d{1,2}(-\d)?", w[4]) and _xc(w) < min(cols.values()) - 5]
        for nw in nums:
            n, _, group = nw[4].partition("-")  # "51-2": second statement block of Q51
            n = int(n)
            row = key.setdefault(n, {})
            for w in side:
                if w[4] in ("T", "F") and abs(_yc(w) - _yc(nw)) < 3:
                    lab = min(cols, key=lambda c: abs(cols[c] - _xc(w)))
                    row[f"{group}{lab}"] = w[4] == "T"
    key = {n: v for n, v in key.items() if v}
    _apply(paper, key, "answer_key")


# ------------------------------------------------------------------ 2017 ---

_NUM = re.compile(r"^[-–−]?\d")


def key_2017_choices(paper: ParsedPaper):
    """Numeric choice items: options are on the row above the 'Choose the
    nearest ...' prompt; the correct one has an X under it on the prompt row."""
    doc = paper.doc  # type: ignore[attr-defined]
    for q in paper.questions:
        for it in q.items:
            if not it.attrs.pop("pending_options", False):
                continue
            pno, py0, py1 = it.attrs.pop("prompt_line")
            page = doc[pno - 1]
            words = _words(page)
            above = [w for w in words if py0 - 22 < _yc(w) < py0 + 1]
            if not above:
                q.notes.append(f"item {it.label}: options row not found")
                continue
            row_y = max(_yc(w) for w in above)
            opt_words = sorted([w for w in above if abs(_yc(w) - row_y) < 4], key=lambda w: w[0])
            options: list[list] = []
            for w in opt_words:
                t = w[4]
                starts = bool(_NUM.match(t)) or t in ("-", "–", "−")
                if options and options[-1][0][-1] in ("-", "–", "−") and _NUM.match(t):
                    starts = False
                if starts or not options:
                    options.append([t, w[0], w[2]])
                else:
                    options[-1][0] += ("" if options[-1][0] in ("-", "–", "−") else " ") + t
                    options[-1][2] = w[2]
            xs = [w for w in words if w[4] == "X" and py0 - 2 < _yc(w) < py1 + 2]
            it.options = [(LETTERS[i], o[0]) for i, o in enumerate(options)]
            if xs and options:
                xw = xs[0]
                best = min(range(len(options)), key=lambda i: abs((options[i][1] + options[i][2]) / 2 - _xc(xw)))
                it.answer = {"option": LETTERS[best], "value": options[best][0]}
                it.answer_text = options[best][0]
                it.answer_source = "x_mark"
            # The options row was read as text of the preceding block; take it out.
            opt_text = " ".join(o[0] for o in options)
            _remove_text(q, [o[0] for o in options])
            it.attrs["options_row"] = opt_text
            # The question asked is the paragraph right before the options.
            prompt = _pop_prompt(q, it)
            if prompt:
                it.rich = prompt + "<br>" + it.rich
            else:
                q.notes.append(f"item {it.label}: question text for the numeric item not separated")


def _remove_text(q, tokens: list[str]):
    pat = r"\s*".join(re.escape(t) for t in tokens)
    for el in q.elements:
        if el.rich and re.search(pat, strip_markup(el.rich)):
            el.rich = re.sub(pat + r"\s*$", "", strip_markup(el.rich)).strip()
    for ex in q.explanations:
        if re.search(pat, strip_markup(ex.rich)):
            ex.rich = re.sub(pat + r"\s*$", "", strip_markup(ex.rich)).strip()
    q.elements = [e for e in q.elements if e.role in ("figure", "table") or e.rich]


def _pop_prompt(q, item) -> str | None:
    """The last paragraph before the item that ends with '?' or reads as a task."""
    for i in range(len(q.elements) - 1, -1, -1):
        el = q.elements[i]
        if el.role in ("paragraph", "statement_lead", "appendix") and el.rich:
            txt = strip_markup(el.rich)
            if txt.endswith("?") or re.search(r"\b(calculate|what|how|estimate|predict)\b", txt, re.I):
                item.attrs["prompt_from_element"] = True
                return q.elements.pop(i).rich
            return None
    return None
