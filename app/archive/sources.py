"""Per-paper parsing rules for the IBO 2013-2018 theory papers.

Each `PaperSpec` describes one paper: where its PDF is, how its question
numbers, section headings, statements, answers and explanations look. The
practical exams in the same folders are deliberately not listed.

Layouts in brief:
- 2013 (Bern): exam-system print, solutions inline ("A. False ... Original
  commentary / A false <why>"). Vector figures.
- 2014 (Bali): Russian translation (Word). Answers only in a separate English
  answer-sheet PDF ("IBO 2014 Theory Answers CCL"); two questions per part
  were cancelled (blacked out on the sheet). No explanations.
- 2015 (Aarhus): web print with solutions; unlabeled statements under a
  "TRUE FALSE" header; "Solution: 1. False <why>".
- 2016 (Hanoi): scanned with an embedded OCR layer, "Answer key:" +
  "Explanation:" inline. Includes gap-filling questions.
- 2017 (Coventry): web print; T/F tables with an X in the True/False column,
  numeric "Choose the nearest ..." items; bulleted explanations per statement.
- 2018 (Tehran): Word export, A-E statements; answers in a separate key PDF.
"""
from __future__ import annotations

import re

from app.archive.answer_keys import key_2014, key_2016, key_2017_choices, key_2018
from app.archive.parse import Line, PaperSpec

ROOT = "data/IBO 2013-2018"
IBO_CURVE = {"type": "ibo_curve", "curve": {"4": [0, 0, 0.2, 0.6, 1.0]}}
PER_STATEMENT = {"type": "per_item", "points": 1}


# ------------------------------------------------------------------ 2013 ---

def _q2013(ln: Line, ctx):
    if re.fullmatch(r"\d{1,2}", ln.text) and ln.bold and ln.size >= 12 and ln.x0 < 120:
        n = int(ln.text)
        return n, str(n), None
    return None


def _sec2013(ln: Line):
    if ln.bold and ln.size < 8 and ln.x0 > 300 and ln.y0 < 170 and len(ln.text) > 5:
        return ln.text
    return None


def spec_2013(paper: str) -> PaperSpec:
    return PaperSpec(
        year=2013, paper_code=paper, lang="en", host_country="Switzerland",
        title=f"IBO 2013 Theoretical Exam {1 if paper == 'A' else 2}",
        path=f"{ROOT}/2013/Theory/2013-{paper}-SOLUTION.pdf",
        doc_role="questions_with_solutions", page_range=(2, None),
        question_start=_q2013, section=_sec2013,
        drop=[r"^IBO 2013 Theoretical Exam", r"^Translation version"],
        inline_answers=r"^A\.\s*(True|False)\s+B\.",
        explanation_start=r"^Original commentary",
        item_explanation=r"^([A-F])\s+(true|false|wrong|right|correct|incorrect)\b\s*(.*)$",
        scoring=IBO_CURVE,
    )


# ------------------------------------------------------------------ 2014 ---

def _q2014(ln: Line, ctx):
    m = re.match(r"^(\d{1,2})\.\s*(.*)$", ln.text)
    if m and ln.x0 < 70 and ctx.get("probe"):
        return 0, "", None  # structural line: keep it out of figures
    # numbers are trusted when they move forward by 1-2 (one missed marker)
    if m and ln.x0 < 70 and ctx["next"] <= int(m.group(1)) <= ctx["next"] + 1:
        n = int(m.group(1))
        rest = re.sub(r"^(<b>)?\s*\d{1,2}\.\s*(</b>)?\s*", "", ln.rich)
        return n, f"{n}.", None, rest or None
    return None


def _sec2014(ln: Line):
    t = ln.text.strip()
    if ln.y0 < 75 or re.match(r"^(ЧАСТЬ|ТЕОРЕТИЧЕСКИЙ|БАЛИ|IBO)", t):
        return None  # running header
    if len(t) > 6 and t == t.upper() and re.search(r"[А-ЯЁ]{4}", t) and not re.match(r"^\d", t):
        return t.title()
    return None


def spec_2014(paper: str) -> PaperSpec:
    return PaperSpec(
        year=2014, paper_code=paper, lang="ru", host_country="Indonesia",
        title=f"IBO 2014 Theoretical Test Part {paper} (Russian translation)",
        path=f"{ROOT}/2014/Theory/Theory Part {paper} (Russian).pdf",
        page_range=(3, None),
        question_start=_q2014, section=_sec2014,
        instruction=r"(Для каждого (из (следующих|приведенных) )?(утверждени|типа)|Укажите для каждого|верно оно или нет|является (ли )?(оно|соответствующее утверждение) верным)",
        statements_without_instruction=True,
        statement_label=r"^([A-H])\s*\.\s*(.*)$",
        scoring=IBO_CURVE, duration_minutes=180, total_points=48 if paper == "A" else None,
        answer_keys=[lambda p, part=paper: key_2014(p, part)],
        extra_docs=[(f"{ROOT}/2014/Theory/IBO 2014 Theory Answers CCL.pdf", "answer_key")],
    )


# ------------------------------------------------------------------ 2015 ---

def _q2015(ln: Line, ctx):
    m = re.fullmatch(r"Q\.\s*(\d{1,2})", ln.text.strip())
    if m:
        n = int(m.group(1))
        return n, f"Q. {n}", None
    return None


def spec_2015(paper: str) -> PaperSpec:
    return PaperSpec(
        year=2015, paper_code=paper, lang="en", host_country="Denmark",
        title=f"IBO 2015 Theory {paper}",
        path=f"{ROOT}/2015/Theory/Theory {paper}_with solutions.pdf",
        doc_role="questions_with_solutions", page_range=(2, None),
        question_start=_q2015,
        instruction=r"^Indicate if each of the following",
        tf_header=r"(TRUE|FALSE)(\s*(TRUE|FALSE))?",
        explanation_start=r"^Solution\s*:?",
        item_explanation=r"^(\d)\s*\.\s*(True|False)\b\s*(.*)$",
        item_expl_numeric=True,
        scoring=IBO_CURVE,
    )


# ------------------------------------------------------------------ 2016 ---

_OCR_DIGITS = str.maketrans({"i": "1", "I": "1", "l": "1", "|": "1", "O": "0", "o": "0", "S": "5", "s": "5"})


def _q2016(ln: Line, ctx):
    """'Q.12', 'Q . l l' (OCR of 11), 'Q.IO' (10), optionally followed by text.
    Part B is numbered 51-100. Numbers are trusted when they move forward by
    1-3 (a missed marker), otherwise the next sequential number is used."""
    m = re.match(r"^Q\s*[\.,]\s*((?:[0-9iIlOoSs|]\s?){1,3})\s*\.?(?:\s+(?=[A-Z])(.*))?$", ln.text.strip())
    if not m or ln.x0 > 120:
        return None
    raw = m.group(1).replace(" ", "").translate(_OCR_DIGITS)
    nxt = ctx["next"]
    if ctx.get("probe"):
        return 0, "", None
    n = int(raw) if raw.isdigit() else None
    if nxt == 1 and n in (1, 51):
        pass
    elif n is None or not (nxt <= n <= nxt + 2):
        if n is not None and n < nxt:
            return None  # a back-reference such as "Q.22)." inside a question
        n = nxt
    rest = m.group(2)
    return n, f"Q.{n}", None, rest


def _sec2016(ln: Line):
    t = ln.text.replace(" ", "")
    known = ("CELLBIOLOGY", "PLANTANATOMYANDPHYSIOLOGY", "ANIMALANATOMYANDPHYSIOLOGY", "ETHOLOGY",
             "GENETICSANDEVOLUTION", "ECOLOGY", "BIOSYSTEMATICS")
    if t.upper() in known and ln.text.upper() == ln.text:
        return {"CELLBIOLOGY": "Cell Biology", "PLANTANATOMYANDPHYSIOLOGY": "Plant Anatomy and Physiology",
                "ANIMALANATOMYANDPHYSIOLOGY": "Animal Anatomy and Physiology", "ETHOLOGY": "Ethology",
                "GENETICSANDEVOLUTION": "Genetics and Evolution", "ECOLOGY": "Ecology",
                "BIOSYSTEMATICS": "Biosystematics"}[t.upper()]
    return None


def spec_2016(paper: str) -> PaperSpec:
    return PaperSpec(
        year=2016, paper_code=paper, lang="en", host_country="Vietnam",
        title=f"IBO 2016 Theoretical Test Part {paper}",
        path=f"{ROOT}/2016/Theory/Theory part {paper} Detailed Answers.pdf",
        doc_role="questions_with_solutions", text_layer="embedded_ocr", page_range=(5, None),
        infer_missing_markers=True,
        question_start=_q2016, section=_sec2016,
        drop=[r"^IBO\s*2016", r"^THEORETICAL\s*TEST", r"^T\s*H\s*E\s*O\s*R\s*E\s*T"],
        instruction=r"^Indicate in the Answer Sheet|^Fill in|^Write (down )?(the|your)",
        answer_key_start=r"^Answer\s*key",
        explanation_start=r"^Explanation\s*:?",
        item_explanation=r"^([A-F])\s*[\.\)]\s*(True|False)\s*[\.,;:]?\s*(.*)$",
        reference_start=r"^(References?)\s*:?\s*(.*)$",
        scoring=IBO_CURVE, duration_minutes=180, total_points=50,
        answer_keys=[key_2016],
    )


# ------------------------------------------------------------------ 2017 ---

def _caps_title(t: str) -> bool:
    return len(t) >= 3 and t == t.upper() and bool(re.search(r"[A-Z]{3}", t))


def _q2017(ln: Line, ctx):
    """Question titles are 14 pt capitals ('CANCER MICROARRAY'); 20 pt
    capitals are section titles; the instruction pages also use 14 pt."""
    t = ln.text.strip()
    if 13.0 <= ln.size <= 15.5 and _caps_title(t) and not re.match(r"^(PAPER \d|USEFUL SCIENTIFIC)", t):
        n = ctx["next"] or 0
        return n, str(n), t.title()
    return None


def _sec2017(ln: Line):
    t = ln.text.strip()
    if ln.size >= 18 and _caps_title(t) and not re.match(r"^(NAVIGATING|INSTRUCTIONS|USEFUL)", t):
        return t.title()
    return None


def spec_2017(paper: str) -> PaperSpec:
    path = {"1": "Theory1 Final Solutions_online.pdf", "2": "Theory2 Final Solutions.pdf"}[paper]
    return PaperSpec(
        year=2017, paper_code=paper, lang="en", host_country="United Kingdom",
        title=f"IBO 2017 Theory Paper {paper}",
        path=f"{ROOT}/2017/Theory/{path}",
        doc_role="questions_with_solutions", page_range=(2, None),
        question_start=_q2017, section=_sec2017,
        instruction=None,
        statement_label=r"^([A-H])\s*[\.\)]\s+(.*)$",
        tf_header=r"(True|False)(\s*(True|False))?",
        explanation_start=r"^Explanation\s*:?",
        choice_prompt=r"^Choose the nearest",
        reference_start=r"^(References?)\s*:\s*(.*)$",
        scoring=PER_STATEMENT, duration_minutes=180,
        answer_keys=[key_2017_choices],
    )


# ------------------------------------------------------------------ 2018 ---

def _q2018(ln: Line, ctx):
    m = re.match(r"^Q\.\s*(\d{1,2})\s*[–—-]\s*(.+)$", ln.text.strip())
    if m:
        n = int(m.group(1))
        return n, f"Q. {n}", m.group(2).strip()
    return None


def spec_2018(paper: str) -> PaperSpec:
    return PaperSpec(
        year=2018, paper_code=paper, lang="en", host_country="Iran",
        title=f"IBO 2018 Theoretical Exam {paper}",
        path=f"{ROOT}/2018/Theory/IBO 2018_Theoretical exam {paper}.pdf",
        page_range=(2, None),
        question_start=_q2018, statements_need_header=True,
        drop=[r"^IBO 2018 [–-] Theoretical Exam"],
        instruction=None,
        tf_header=r"(True|False)(\s*(True|False))?",
        statement_label=r"^([A-H])\s*\.\s*(.*)$",
        scoring=IBO_CURVE,
        answer_keys=[lambda p, part=paper: key_2018(p, part)],
        extra_docs=[(f"{ROOT}/2018/Theory/IBO 2018_Theoretical exams 1 and 2_Answer key.pdf", "answer_key")],
    )


ALL_SPECS = {
    "2013-A": lambda: spec_2013("A"),
    "2013-B": lambda: spec_2013("B"),
    "2014-A": lambda: spec_2014("A"),
    "2014-B": lambda: spec_2014("B"),
    "2015-1": lambda: spec_2015("1"),
    "2015-2": lambda: spec_2015("2"),
    "2016-A": lambda: spec_2016("A"),
    "2016-B": lambda: spec_2016("B"),
    "2017-1": lambda: spec_2017("1"),
    "2017-2": lambda: spec_2017("2"),
    "2018-1": lambda: spec_2018("1"),
    "2018-2": lambda: spec_2018("2"),
}
