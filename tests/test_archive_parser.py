"""IBO archive parser: unit checks, plus integration checks when the PDFs exist.

The source PDFs live in data/ (not tracked in git), so the integration tests
skip on machines that do not have them.
"""
from pathlib import Path

import pytest

from app.archive.layout import CAPTION_RE, fix_spaced_letters, strip_markup
from app.archive.parse import parse_answer_run, to_bool

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data" / "IBO 2013-2018"


@pytest.mark.parametrize("text, expected", [
    ("Answer key: A. False; B. False, C. False, D. True", {"A": False, "B": False, "C": False, "D": True}),
    ("A.TrueB.FalseC.TrueD.False", {"A": True, "B": False, "C": True, "D": False}),
    ("Answer key A.TmeB.FalseC.TmeD.Tme", {"A": True, "B": False, "C": True, "D": True}),  # OCR
    ("A- True; B- False", {"A": True, "B": False}),
    ("no answers here", {}),
])
def test_parse_answer_run(text, expected):
    assert parse_answer_run(text) == expected


def test_to_bool_vocab():
    assert to_bool("True") is True and to_bool("wrong") is False and to_bool("Верно") is True
    assert to_bool("maybe") is None


@pytest.mark.parametrize("line, is_caption", [
    ("Fig. 1. Effect of okadaic acid", True),
    ("Figure Q.2. Time course", True),
    ("Fig.Q.l. (A) Control", True),
    ("Table 2 shows", True),
    ("Рисунок 3", True),
    ("Схема показывает петли обратной связи", False),
    ("Figures are rounded to aid comparison", False),
])
def test_caption_regex(line, is_caption):
    assert bool(CAPTION_RE.match(line)) is is_caption


def test_text_helpers():
    assert fix_spaced_letters("T H E O R E T I C A L") == "THEORETICAL"
    assert strip_markup("tRNA<sup>Trp</sup> &amp; O<sub>2</sub>") == "tRNATrp & O2"


needs_pdfs = pytest.mark.skipif(not DATA.exists(), reason="IBO source PDFs not present")


@needs_pdfs
def test_2013_part_a_end_to_end():
    from app.archive.parse import parse_paper
    from app.archive.sources import ALL_SPECS

    paper = parse_paper(ALL_SPECS["2013-A"](), BASE)
    assert [q.number for q in paper.questions] == list(range(1, 46))
    q1 = paper.questions[0]
    assert q1.section == "Cell-, molecular- and microbiology"
    assert [i.label for i in q1.items] == ["A", "B", "C", "D"]
    assert [i.answer["bool"] for i in q1.items] == [False, False, True, True]
    assert "tRNA<sup>Trp</sup>" in q1.items[1].rich  # superscript survives
    assert {e.item_label for e in q1.explanations if e.kind == "explanation"} >= {"A", "B", "C", "D"}
    assert any(e.role == "figure" for e in q1.elements)
    assert not any(q.needs_review for q in paper.questions)


@needs_pdfs
def test_2014_cancelled_questions_from_answer_sheet():
    from app.archive.parse import parse_paper
    from app.archive.sources import ALL_SPECS

    paper = parse_paper(ALL_SPECS["2014-A"](), BASE)
    cancelled = sorted(q.number for q in paper.questions if q.status == "cancelled")
    assert cancelled == [21, 30]  # blacked out on the official answer sheet
    q1 = paper.questions[0]
    assert q1.items[0].answer_source == "answer_key"
