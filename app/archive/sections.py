"""The eight sections archive questions are filed under.

Each paper printed its own section names: spelled differently from year to year
("Animal anatomy and physiology" / "Animal Anatomy and Physiology"), in Russian,
lumped together ("Genetics and evolution"), themed rather than by subject (2017)
or missing altogether (2015, 2018). `IboQuestion.section` keeps what was printed;
`IboQuestion.section_group` is the common section used for filtering.

How a question gets its group:
1. `section_groups.json` names it explicitly (paper -> question number -> group).
   That file is where classifications are reviewed and corrected.
2. Otherwise its printed section name is looked up in `PRINTED_NAMES`.
3. Otherwise it has no group.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.archive import IboExam, IboQuestion

# (slug, English name), in the order shown to users. Translations live in the
# locale files as `archive.group.<slug>`.
GROUPS: list[tuple[str, str]] = [
    ("cell-biology", "Cell Biology"),
    ("genetics", "Genetics"),
    ("evolution", "Evolution"),
    ("animal-anatomy-physiology", "Animal Anatomy and Physiology"),
    ("plant-anatomy-physiology", "Plant Anatomy and Physiology"),
    ("ecology", "Ecology"),
    ("ethology", "Ethology"),
    ("biosystematics", "Biosystematics"),
]
GROUP_SLUGS = [slug for slug, _ in GROUPS]

# Some years numbered their two papers 1 and 2; they are the same two papers as
# A and B in the other years, and are stored as A and B.
PAPER_ALIASES = {"1": "A", "2": "B"}


def canonical_paper(code: str | None) -> str:
    code = (code or "").strip()
    return PAPER_ALIASES.get(code, code)

# Section names as printed (lower case) that name exactly one group.
PRINTED_NAMES: dict[str, str] = {
    "cell biology": "cell-biology",
    "cell-, molecular- and microbiology": "cell-biology",
    "биология клетки и молекулярная биология": "cell-biology",
    "animal anatomy and physiology": "animal-anatomy-physiology",
    "анатомия и физиология животных": "animal-anatomy-physiology",
    "plant anatomy and physiology": "plant-anatomy-physiology",
    "анатомия и физиология растений": "plant-anatomy-physiology",
    "ecology": "ecology",
    "экология": "ecology",
    "ethology": "ethology",
    "этология": "ethology",
    "biosystematics": "biosystematics",
    "биосистематика": "biosystematics",
}

# The topic an author's draft gets when started from an archive question
# (slugs from app.services.topics).
GROUP_TOPICS: dict[str, str] = {
    "cell-biology": "cell-molecular-biology",
    "genetics": "genetics-evolution",
    "evolution": "genetics-evolution",
    "animal-anatomy-physiology": "animal-anatomy-physiology",
    "plant-anatomy-physiology": "plant-anatomy-physiology",
    "ecology": "ecology",
    "ethology": "ethology",
    "biosystematics": "biosystematics",
}

_DATA = Path(__file__).with_name("section_groups.json")


@lru_cache(maxsize=1)
def explicit_assignments() -> dict[str, dict[str, str]]:
    data = json.loads(_DATA.read_text(encoding="utf-8"))
    for paper, numbers in data.items():
        for number, slug in numbers.items():
            if slug not in GROUP_SLUGS:
                raise ValueError(f"{paper} question {number}: unknown section group {slug!r}")
    return data


def exam_key(exam: IboExam) -> str:
    return f"{exam.year}-{canonical_paper(exam.paper_code)}"


def group_for(key: str, number: int, printed: str | None) -> str | None:
    year, _, paper = key.partition("-")
    key = f"{year}-{canonical_paper(paper)}"  # accepts the old 1/2 spelling too
    explicit = explicit_assignments().get(key, {}).get(str(number))
    if explicit:
        return explicit
    return PRINTED_NAMES.get((printed or "").strip().lower())


def assign_section_groups(db: Session) -> int:
    """Fill in `section_group` where it is empty. Returns how many were set.

    Safe to repeat. Existing values are left alone, so a group corrected by hand
    in the database survives a restart.
    """
    rows = db.execute(
        select(IboQuestion, IboExam)
        .join(IboExam, IboQuestion.exam_id == IboExam.id)
        .where(IboQuestion.section_group.is_(None))
    ).all()
    changed = 0
    for question, exam in rows:
        group = group_for(exam_key(exam), question.number, question.section)
        if group:
            question.section_group = group
            changed += 1
    db.flush()
    return changed
