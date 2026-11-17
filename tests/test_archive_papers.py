import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

from app.archive.sections import canonical_paper

ROOT = Path(__file__).resolve().parent.parent


def test_canonical_paper():
    assert canonical_paper("1") == "A" and canonical_paper("2") == "B"
    assert canonical_paper("A") == "A" and canonical_paper("B") == "B"
    assert canonical_paper(" 2 ") == "B"
    assert canonical_paper(None) == ""


def _alembic(url: str, target: str) -> None:
    env = {**os.environ, "NBO_DATABASE_URL": url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", target],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr[-800:]


def test_migration_renames_papers_and_labels(tmp_path):
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    _alembic(url, "e7f8a9b0c1d2")  # the version before the rename
    engine = create_engine(url)
    now = "2026-11-17 00:00:00"
    with engine.begin() as c:
        def exam(year, code):
            c.execute(text(
                "INSERT INTO ibo_exams (year, paper_code, title, language, extra, created_at, updated_at) "
                "VALUES (:y, :c, :t, 'en', '{}', :n, :n)"), {"y": year, "c": code, "t": f"IBO {year} {code}", "n": now})
        for year, code in [(2013, "A"), (2013, "B"), (2015, "1"), (2015, "2"), (2017, "1"), (2017, "2"),
                           (2020, "A"), (2020, "1")]:  # 2020: a real A and a real 1 side by side
            exam(year, code)

        def question(ref, year):
            c.execute(text(
                "INSERT INTO questions (question_type, status, source_ref, source_year, difficulty, "
                "is_official_ibo, max_points, estimated_seconds, times_used, tags, created_at, updated_at) "
                "VALUES ('tf_block', 'draft', :r, :y, 'medium', 0, 1.0, 60, 0, '[]', :n, :n)"),
                {"r": ref, "y": year, "n": now})
        for ref, year in [("IBO 2017 1 Q. 3", 2017), ("IBO 2017 2 Q. 9", 2017), ("IBO 2013 A Q. 1", 2013),
                          ("IBO 2020 1 Q. 4", 2020), ("custom note about paper 1", None)]:
            question(ref, year)
        first = c.execute(text("SELECT id FROM questions ORDER BY id LIMIT 1")).scalar()
        for locale, title in (("en", "IBO 2017 1 Q. 3"), ("ru", "Мой вопрос про paper 1")):
            c.execute(text(
                "INSERT INTO question_translations (question_id, locale, stem, title, created_at, updated_at) "
                "VALUES (:q, :l, 's', :t, :n, :n)"), {"q": first, "l": locale, "t": title, "n": now})

    _alembic(url, "head")

    with engine.connect() as c:
        codes = sorted(tuple(r) for r in c.execute(text("SELECT year, paper_code FROM ibo_exams")))
        assert codes == [(2013, "A"), (2013, "B"), (2015, "A"), (2015, "B"), (2017, "A"), (2017, "B"),
                         (2020, "1"), (2020, "A")]  # 2020 untouched: two real papers are never merged
        refs = sorted(r[0] for r in c.execute(text("SELECT source_ref FROM questions")))
        assert refs == ["IBO 2013 A Q. 1", "IBO 2017 A Q. 3", "IBO 2017 B Q. 9",
                        "IBO 2020 1 Q. 4", "custom note about paper 1"]
        titles = sorted(r[0] for r in c.execute(text("SELECT title FROM question_translations")))
        assert titles == ["IBO 2017 A Q. 3", "Мой вопрос про paper 1"]  # only the generated label changes
        issue = c.execute(text("SELECT issue_type, status, description FROM issues ORDER BY id DESC")).first()
        assert issue[0] == "change" and issue[1] == "done" and "A and B" in issue[2]

    _alembic(url, "head")  # running it again changes nothing
    with engine.connect() as c:
        assert sorted(tuple(r) for r in c.execute(text("SELECT year, paper_code FROM ibo_exams"))) == codes
