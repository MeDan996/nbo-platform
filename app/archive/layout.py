"""Page layout extraction for archive PDFs.

Turns a PDF page into:
- `Line`s: visual text rows with inline markup (<i>, <b>, <sub>, <sup>) rebuilt
  from span geometry, so "tRNA^Trp" and "O_2" survive extraction;
- `Graphic`s: figure regions (clusters of images + vector drawings + the short
  labels printed inside them) and tables (cell text rebuilt from spans).

Text inside a figure is not body text: it is moved into `Graphic.labels`, and
the figure itself is later stored as a rendered crop of the page.

Scanned pages (one image covering the page, with an OCR text layer) have no
usable image/drawing geometry, so their figures are found as vertical bands
between prose lines that hold only OCR junk or nothing at all.
"""
from __future__ import annotations

import re
import statistics
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

import pymupdf

LIGATURES = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
    "ﬅ": "st", "ﬆ": "st", "\xa0": " ", " ": " ", " ": " ", " ": " ",
}
_LIG_RE = re.compile("|".join(map(re.escape, LIGATURES)))

CAPTION_RE = re.compile(
    r"^\s*(Fig|FIG|Figure|FIGURE|Рис|Рисунок|РИСУНОК|Table|TABLE|Таблица|ТАБЛИЦА)\.?\s*"
    r"(\d+|[A-Z]\b|Q\s*\.?\s*[\dlIi]+|[IVX]+\b|\(|\.)"
)


def clean(s: str) -> str:
    s = _LIG_RE.sub(lambda m: LIGATURES[m.group(0)], s)
    # Private-use glyphs (icon fonts) carry no text.
    return "".join(ch for ch in s if not ("" <= ch <= ""))


def strip_markup(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@dataclass
class Line:
    page: int  # 1-based
    x0: float
    y0: float
    x1: float
    y1: float
    text: str  # plain
    rich: str  # with inline markup
    size: float
    bold: bool
    font: str

    @property
    def bbox(self):
        return (self.x0, self.y0, self.x1, self.y1)

    @property
    def h(self):
        return self.y1 - self.y0

    def __repr__(self):  # compact, for debugging
        return f"<L p{self.page} y{self.y0:.0f} x{self.x0:.0f} {self.text[:50]!r}>"


@dataclass
class Graphic:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    kind: str  # "figure" | "table"
    labels: list[str] = field(default_factory=list)
    rows: list[list[str]] | None = None  # tables
    cells: list | None = None  # tables: per row, list of cell bboxes (or None)
    source: str = "vector"  # vector | image | scan_gap

    @property
    def bbox(self):
        return (self.x0, self.y0, self.x1, self.y1)

    def __repr__(self):
        return f"<G {self.kind} p{self.page} y{self.y0:.0f}-{self.y1:.0f} x{self.x0:.0f}-{self.x1:.0f} labels={len(self.labels)}>"


@dataclass
class PageLayout:
    no: int
    width: float
    height: float
    lines: list[Line]
    graphics: list[Graphic]
    scanned: bool
    raw_text: str


# ---------------------------------------------------------------- geometry ---

def _area(b):
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _inter(a, b):
    return (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))


def _overlap_frac(inner, outer):
    a = _area(inner)
    return _area(_inter(inner, outer)) / a if a else 0.0


def _expand(b, d):
    return (b[0] - d, b[1] - d, b[2] + d, b[3] + d)


def _union(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _intersects(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _merge_boxes(boxes, pad):
    boxes = [tuple(b) for b in boxes]
    changed = True
    while changed:
        changed = False
        out = []
        while boxes:
            cur = boxes.pop()
            i = 0
            while i < len(boxes):
                if _intersects(_expand(cur, pad), boxes[i]):
                    cur = _union(cur, boxes.pop(i))
                    changed = True
                else:
                    i += 1
            out.append(cur)
        boxes = out
    return boxes


# ------------------------------------------------------------------- spans ---

def _spans(page) -> list[dict]:
    out = []
    d = page.get_text("dict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP)
    for b in d["blocks"]:
        if b.get("type") != 0:
            continue
        for ln in b["lines"]:
            # Rotated text (axis titles) is figure furniture.
            rotated = abs(ln.get("dir", (1, 0))[1]) > 0.2
            for sp in ln["spans"]:
                t = clean(sp["text"])
                if not t.strip():
                    continue
                out.append(dict(
                    text=t, bbox=tuple(sp["bbox"]), size=sp["size"], font=sp["font"],
                    flags=sp["flags"], origin=sp["origin"], rotated=rotated,
                ))
    return out


def _bullets(page) -> list[dict]:
    """Small filled circles drawn as list bullets -> synthetic '•' spans."""
    out = []
    for g in page.get_drawings():
        r = g["rect"]
        if 2 <= r.width <= 7 and 2 <= r.height <= 7 and g.get("fill") is not None:
            kinds = {it[0] for it in g["items"]}
            if kinds == {"c"}:
                out.append(dict(text="• ", bbox=(r.x0, r.y0 - 3, r.x1 + 2, r.y1 + 3), size=0.0,
                                font="bullet", flags=0, origin=(r.x0, r.y1 + 2), rotated=False))
    return out


def _rows(spans: list[dict]) -> list[list[dict]]:
    """Group spans into visual rows by vertical overlap, splitting on wide gaps.

    Larger spans are placed first so rows anchor on the main text; sub- and
    superscripts then join the row they overlap most."""
    rows: list[list[dict]] = []
    boxes: list[list[float]] = []  # [y0, y1] of each row's anchor text
    for sp in sorted(spans, key=lambda s: -s["size"]):
        y0, y1 = sp["bbox"][1], sp["bbox"][3]
        h = max(0.1, y1 - y0)
        best, best_ov = None, 0.0
        if not sp["rotated"]:
            for i, (ry0, ry1) in enumerate(boxes):
                ov = (min(y1, ry1) - max(y0, ry0)) / min(h, max(0.1, ry1 - ry0))
                if ov > 0.5 and ov > best_ov:
                    best, best_ov = i, ov
        if best is None:
            rows.append([sp])
            boxes.append([y0, y1])
        else:
            rows[best].append(sp)
    rows.sort(key=lambda r: min(s["bbox"][1] for s in r))
    # split on large horizontal gaps (separate columns / table cells / figure labels)
    out = []
    for row in rows:
        row.sort(key=lambda s: s["bbox"][0])
        cur = [row[0]]
        for sp in row[1:]:
            gap = sp["bbox"][0] - max(s["bbox"][2] for s in cur)
            if gap > max(28.0, 2.5 * max(s["size"] for s in cur)):
                out.append(cur)
                cur = [sp]
            else:
                cur.append(sp)
        out.append(cur)
    return out


def _row_to_line(page_no: int, row: list[dict]) -> Line:
    main = max(row, key=lambda s: (s["size"], len(s["text"])))
    base_size = main["size"] or 10.0
    baseline = main["origin"][1]
    parts_plain, parts_rich = [], []
    prev_x1 = None
    for sp in row:
        t = sp["text"]
        if prev_x1 is not None:
            gap = sp["bbox"][0] - prev_x1
            joined = "".join(parts_plain)
            if gap > 0.22 * base_size and not joined.endswith(" ") and not t.startswith(" "):
                parts_plain.append(" ")
                parts_rich.append(" ")
        prev_x1 = sp["bbox"][2]
        r = _esc(t)
        small = sp["size"] and sp["size"] < 0.85 * base_size
        if sp["flags"] & 1 or (small and sp["origin"][1] < baseline - 0.15 * base_size):
            r = f"<sup>{r.strip()}</sup>"
        elif small and sp["origin"][1] > baseline + 0.08 * base_size:
            r = f"<sub>{r.strip()}</sub>"
        else:
            if sp["flags"] & 2:
                r = f"<i>{r}</i>"
            if sp["flags"] & 16 or "Bold" in sp["font"] or "-Bo" in sp["font"]:
                r = f"<b>{r}</b>"
        parts_plain.append(t)
        parts_rich.append(r)
    plain = re.sub(r"\s+", " ", "".join(parts_plain)).strip()
    rich = re.sub(r"\s+", " ", "".join(parts_rich)).strip()
    rich = re.sub(r"</(i|b)>\s*<\1>", " ", rich)  # merge adjacent runs
    rich = re.sub(r"</(i|b)><\1>", "", rich)
    x0 = min(s["bbox"][0] for s in row)
    x1 = max(s["bbox"][2] for s in row)
    body = [s for s in row if s["size"] >= 0.85 * base_size] or row
    y0 = min(s["bbox"][1] for s in body)
    y1 = max(s["bbox"][3] for s in body)
    bold_chars = sum(len(s["text"]) for s in row if s["flags"] & 16 or "Bold" in s["font"] or "-Bo" in s["font"])
    total = sum(len(s["text"].strip()) for s in row) or 1
    return Line(page_no, x0, y0, x1, y1, plain, rich, round(base_size, 1),
                bold_chars / total > 0.6, main["font"])


def fix_spaced_letters(s: str) -> str:
    """OCR layers sometimes emit 'T H E O R E T I C A L'. Collapse such runs."""
    toks = s.split(" ")
    if len(toks) >= 5 and sum(len(t) == 1 for t in toks) / len(toks) > 0.7:
        return re.sub(r"(?<=\S) (?=\S(\s|$))", "", s)
    return s


# ---------------------------------------------------------------- figures ---

def _is_label_like(line: Line, body_size: float) -> bool:
    if CAPTION_RE.match(line.text) or (STATEMENT_START.match(line.text) and line.size >= 0.9 * body_size):
        return False
    if len(line.text.split()) >= 8:
        return False  # a sentence (caption, legend) is text, not a label inside the drawing
    return len(line.text) < 45 or line.size < 0.9 * body_size


STATEMENT_START = re.compile(r"^\s*[A-H]\s*[\.\)]\s+\S")


def _is_short_label(line: Line) -> bool:
    t = line.text.strip()
    if CAPTION_RE.match(t) or len(t) > 32 or STATEMENT_START.match(t):
        return False
    if re.search(r"[.:?]$", t) and len(t.split()) > 3:
        return False
    return True


def _continues_prose(lines: list[Line], i: int, absorbed: set[int]) -> bool:
    """Last short line of a paragraph (directly under a long body line)."""
    ln = lines[i]
    for j in range(i - 1, max(-1, i - 4), -1):
        p = lines[j]
        if j in absorbed or p.y1 > ln.y0 + 1:
            continue
        hanging = 4 <= ln.x0 - p.x0 <= 30 and bool(STATEMENT_START.match(p.text))
        return (ln.y0 - p.y1 < 0.6 * max(p.h, 6) and (abs(p.x0 - ln.x0) < 4 or hanging)
                and len(p.text) > 50 and abs(p.size - ln.size) < 0.5)
    return False


def _tables(page, spans) -> list[Graphic]:
    out = []
    try:
        found = page.find_tables()
    except Exception:  # noqa: BLE001 - table finder is best effort
        return out
    words = page.get_text("words")
    for t in found.tables:
        if t.row_count < 2 or t.col_count < 2:
            continue
        rows, cells = [], []
        filled = 0

        def words_in(c, y_lo=None, y_hi=None):
            return [w for w in words if c[0] <= (w[0] + w[2]) / 2 <= c[2] and c[1] <= (w[1] + w[3]) / 2 <= c[3]
                    and (y_lo is None or y_lo <= (w[1] + w[3]) / 2 < y_hi)]

        def join(ws):
            return " ".join(clean(w[4]) for w in sorted(ws, key=lambda w: (round(w[1] / 3), w[0]))).strip()

        for r in t.rows:
            # A row without inner rules can hold several labelled statements
            # stacked in one cell ("A." "B." "C." ...): split it at the labels.
            first = next((c for c in r.cells if c is not None), None)
            labels = [w for w in words_in(first) if re.fullmatch(r"[A-H][\.\)]", w[4])] if first else []
            bands = [(None, None)]
            if len(labels) >= 2:
                ys = sorted((w[1] - 1) for w in labels)
                bands = list(zip(ys, ys[1:] + [r.bbox[3] + 1]))
            for y_lo, y_hi in bands:
                row_txt, row_cells = [], []
                for c in r.cells:
                    if c is None:
                        row_txt.append("")
                        row_cells.append(None)
                        continue
                    # words, not spans: one span may straddle two cells ("True False")
                    txt = join(words_in(c, y_lo, y_hi))
                    row_txt.append(txt)
                    row_cells.append(tuple(c))
                    filled += bool(txt)
                rows.append(row_txt)
                cells.append(row_cells)
        if filled < 0.3 * t.row_count * t.col_count:
            continue  # a grid inside a figure, not a table
        b = t.bbox
        out.append(Graphic(0, b[0], b[1], b[2], b[3], "table", rows=rows, cells=cells, source="table"))
    return out


def extract_page(page, page_no: int, body_size: float | None = None, protect=None) -> PageLayout:
    """`protect(line) -> bool` marks structural lines (question numbers, section
    headings) that must never be swallowed by a figure region."""
    W, H = page.rect.width, page.rect.height
    page_box = (0, 0, W, H)
    spans = _spans(page)
    raw_text = clean(page.get_text())

    # --- images
    img_boxes = []
    scanned = False
    for info in page.get_image_info(xrefs=True):
        b = _inter(tuple(info["bbox"]), page_box)
        if _area(b) < 300:
            continue
        if _area(b) > 0.8 * W * H:
            scanned = True
            continue
        img_boxes.append(b)

    # --- vector drawings
    draw_boxes = []
    for g in page.get_drawings():
        r = g["rect"]
        b = _inter((r.x0, r.y0, r.x1, r.y1), page_box)
        if b[2] - b[0] < 0.5 and b[3] - b[1] < 0.5:
            continue
        if _area(b) > 0.8 * W * H:
            continue
        if (b[2] - b[0]) > 0.6 * W and (b[3] - b[1]) < 3:
            continue  # horizontal rule
        if g.get("fill") in ((1.0, 1.0, 1.0),) and g.get("color") is None:
            continue  # white backgrounds
        draw_boxes.append(b)

    tables = [] if scanned else _tables(page, spans)
    for t in tables:
        t.page = page_no
    table_boxes = [t.bbox for t in tables]

    spans_out = [s for s in spans if not any(_overlap_frac(s["bbox"], tb) > 0.6 for tb in table_boxes)]
    lines = [_row_to_line(page_no, r) for r in _rows(spans_out)]
    for b in _bullets(page):
        bx1, byc = b["bbox"][2], (b["bbox"][1] + b["bbox"][3]) / 2
        for ln in lines:
            if abs((ln.y0 + ln.y1) / 2 - byc) < 4 and 0 < ln.x0 - bx1 < 18 and not ln.text.startswith("•"):
                ln.text, ln.rich = "• " + ln.text, "• " + ln.rich
                break
    if scanned:
        for ln in lines:
            ln.text = fix_spaced_letters(ln.text)
            ln.rich = _esc(ln.text)
    lines.sort(key=lambda l: (l.y0, l.x0))

    if body_size is None:
        c = Counter()
        for ln in lines:
            c[ln.size] += len(ln.text)
        body_size = c.most_common(1)[0][0] if c else 10.0

    graphics: list[Graphic] = list(tables)

    if not scanned:
        boxes = [b for b in draw_boxes + img_boxes
                 if not any(_overlap_frac(b, _expand(tb, 6)) > 0.5 for tb in table_boxes)]
        clusters = _merge_boxes(boxes, 6.0)
        # Thin strips (a gene map bar) count only if they carry labels; checked below.
        clusters = [c for c in clusters if (c[2] - c[0]) > 40 and (c[3] - c[1]) > 8]
        absorbed: set[int] = set()
        protected = {i for i, ln in enumerate(lines) if protect and protect(ln)}
        regions = []
        for c in clusters:
            reg = c
            for _ in range(3):
                grew = False
                for i, ln in enumerate(lines):
                    if i in absorbed or i in protected:
                        continue
                    inside = _overlap_frac(ln.bbox, _expand(reg, 2)) > 0.5
                    near = _intersects(ln.bbox, _expand(reg, 8)) and _is_label_like(ln, body_size)
                    if (inside and (_is_label_like(ln, body_size) or _overlap_frac(ln.bbox, reg) > 0.9)) or near:
                        if CAPTION_RE.match(ln.text):
                            continue
                        absorbed.add(i)
                        reg = _union(reg, ln.bbox)
                        grew = True
                if not grew:
                    break
            labelled = any(_overlap_frac(lines[i].bbox, _expand(reg, 2)) > 0.5 for i in absorbed)
            if (reg[3] - reg[1]) > 25 or ((reg[2] - reg[0]) > 100 and labelled):
                regions.append(reg)
        regions = _merge_boxes(regions, 4.0)
        # Second pass: stray short labels a little further out (axis names,
        # legends set beside the drawing) and labels between two regions.
        for _ in range(2):
            new_regions = []
            for reg in regions:
                for i, ln in enumerate(lines):
                    if i in absorbed or i in protected or not _is_short_label(ln):
                        continue
                    if _continues_prose(lines, i, absorbed):
                        continue
                    if _intersects(ln.bbox, _expand(reg, 28)):
                        absorbed.add(i)
                        reg = _union(reg, ln.bbox)
                new_regions.append(reg)
            regions = _merge_boxes(new_regions, 10.0)
        # A "figure" made only of one horizontal or vertical stroke is decoration.
        regions = [r for r in regions if (r[2] - r[0]) > 30 and (r[3] - r[1]) > 8]
        # A vector "figure" that encloses running prose is a frame or shaded box
        # around text, not a figure: drop it and give its labels back.
        framed = []
        for reg in regions:
            prose_inside = any(
                i not in absorbed and _overlap_frac(ln.bbox, reg) > 0.8 and len(ln.text) > 50
                and abs(ln.size - body_size) < 0.6 for i, ln in enumerate(lines))
            from_images = any(_overlap_frac(b, reg) > 0.3 for b in img_boxes)
            if prose_inside and not from_images:
                for i, ln in enumerate(lines):
                    if _overlap_frac(ln.bbox, reg) > 0.5:
                        absorbed.discard(i)
                continue
            framed.append(reg)
        regions = framed
        trimmed = []
        for reg in regions:
            for i in protected:
                pl = lines[i]
                if _intersects(pl.bbox, _expand(reg, 2)) and reg[3] - pl.y1 > 30:
                    reg = (reg[0], max(reg[1], pl.y1 + 2), reg[2], reg[3])
            trimmed.append(reg)
        # a figure never runs into a table below it (web prints layer images under tables)
        clipped = []
        for r in trimmed:
            for tb in table_boxes:
                if _intersects(r, tb) and r[1] < tb[1]:
                    r = (r[0], r[1], r[2], min(r[3], tb[1] - 2))
            clipped.append(r)
        regions = [r for r in clipped if (r[2] - r[0]) > 30 and (r[3] - r[1]) > 8]
        for reg in regions:
            labels = [lines[i].text for i in sorted(absorbed) if _overlap_frac(lines[i].bbox, _expand(reg, 2)) > 0.5]
            src = "image" if any(_overlap_frac(b, reg) > 0.5 for b in img_boxes) else "vector"
            graphics.append(Graphic(page_no, *reg, kind="figure", labels=labels, source=src))
        lines = [ln for i, ln in enumerate(lines) if i not in absorbed]
    else:
        figs, lines = _scan_figures(page_no, lines, W, H, body_size, protect)
        graphics.extend(figs)

    graphics.sort(key=lambda g: (g.y0, g.x0))
    return PageLayout(page_no, W, H, lines, graphics, scanned, raw_text)


_WHITELIST = re.compile(
    r"^\s*(Q\s*\.|Fig|Figure|Table|Answer|Explanation|Reference|Solution|Note|[A-F]\s*[\.\)]|\d{1,3}\s*\.|•)", re.I
)


def _prose(ln: Line) -> bool:
    t = ln.text.strip()
    letters = sum(ch.isalpha() for ch in t)
    if len(t) >= 25 and letters / max(1, len(t)) > 0.6:
        words = [w for w in t.split() if w.isalpha()]
        return bool(words) and statistics.mean(len(w) for w in words) > 2.6
    return bool(_WHITELIST.match(t)) and letters >= 2


def _scan_figures(page_no, lines, W, H, body_size, protect=None):
    """Figures on a scanned page: bands between prose lines holding junk or nothing."""
    if not lines:
        return [], lines
    prose_flags = [_prose(ln) or bool(protect and protect(ln)) for ln in lines]
    # A short last line of a paragraph is prose too.
    for i in range(1, len(lines)):
        if not prose_flags[i] and prose_flags[i - 1]:
            a, b = lines[i - 1], lines[i]
            if b.y0 - a.y1 < 0.8 * max(a.h, 8) and abs(b.x0 - a.x0) < 12 and sum(c.isalpha() for c in b.text) >= 3:
                prose_flags[i] = True
    prose = [ln for ln, p in zip(lines, prose_flags) if p]
    junk = [ln for ln, p in zip(lines, prose_flags) if not p]
    if prose:
        left = min(ln.x0 for ln in prose)
        right = max(ln.x1 for ln in prose)
    else:
        left, right = 0.08 * W, 0.92 * W
    figs = []
    top_limit, bottom_limit = 0.07 * H, 0.95 * H
    anchors = [(-1.0, top_limit)] + [(ln.y0, ln.y1) for ln in prose] + [(bottom_limit, H)]
    anchors.sort()
    for (a0, a1), (b0, b1) in zip(anchors, anchors[1:]):
        gap_top, gap_bot = a1 + 1, b0 - 1
        if gap_bot - gap_top < 25:
            continue
        inside = [ln for ln in junk if ln.y0 >= gap_top - 2 and ln.y1 <= gap_bot + 2]
        if len(inside) >= 2 or gap_bot - gap_top > 60:
            figs.append(Graphic(page_no, max(0, left - 15), gap_top, min(W, right + 15), gap_bot,
                                kind="figure", labels=[ln.text for ln in inside], source="scan_gap"))
    # One figure split by a stray OCR line that looked like prose: merge back.
    merged: list[Graphic] = []
    for f in figs:
        if merged:
            prev = merged[-1]
            between = [ln for ln in prose if ln.y0 >= prev.y1 - 1 and ln.y1 <= f.y0 + 1]
            if (f.y0 - prev.y1 < 30 and len(between) <= 2
                    and not any(CAPTION_RE.match(ln.text) for ln in between)):
                prev.y1 = f.y1
                prev.labels += [ln.text for ln in between] + f.labels
                for ln in between:
                    ln.text = ""  # absorbed below
                continue
        merged.append(f)
    figs = merged
    in_fig = set()
    for f in figs:
        for i, ln in enumerate(lines):
            if not prose_flags[i] and ln.y0 >= f.y0 - 2 and ln.y1 <= f.y1 + 2:
                in_fig.add(i)
    kept = [ln for i, ln in enumerate(lines) if i not in in_fig and ln.text]
    return figs, kept


def body_size_of(doc) -> float:
    c = Counter()
    for page in doc:
        for s in _spans(page):
            c[round(s["size"], 1)] += len(s["text"])
    return c.most_common(1)[0][0] if c else 10.0


def render_crop(doc, page_no: int, bbox, dpi: int = 160) -> tuple[bytes, int, int]:
    page = doc[page_no - 1]
    clip = pymupdf.Rect(*_expand(bbox, 6)) & page.rect
    pix = page.get_pixmap(dpi=dpi, clip=clip)
    return pix.tobytes("png"), pix.width, pix.height


def normalize_for_repeat(t: str) -> str:
    t = unicodedata.normalize("NFKC", t).lower()
    t = re.sub(r"\d+", "#", t)
    return re.sub(r"\s+", " ", t).strip()
