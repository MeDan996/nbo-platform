import json
from pathlib import Path

from sqlalchemy import select

from app.archive import sections
from app.models.archive import IboExam, IboQuestion
from app.models.enums import Role
from app.services.archive_templates import build_draft
from tests.conftest import make_user, sign_in
from tests.test_archive_templates import make_archive_question


def test_the_data_file_is_well_formed():
    data = json.loads(Path(sections.__file__).with_name("section_groups.json").read_text(encoding="utf-8"))
    assert data  # not empty
    for paper, numbers in data.items():
        assert paper.count("-") == 1 and paper.split("-")[0].isdigit(), paper
        for number, slug in numbers.items():
            assert number.isdigit(), (paper, number)
            assert slug in sections.GROUP_SLUGS, (paper, number, slug)
    assert len(sections.GROUP_SLUGS) == 8
    assert set(sections.GROUP_TOPICS) == set(sections.GROUP_SLUGS)


def test_printed_names_merge_spellings_and_languages():
    f = sections.group_for
    # spellings, case and the Russian paper all land in one group
    assert f("2013-A", 1, "Animal anatomy and physiology") == "animal-anatomy-physiology"
    assert f("2016-A", 1, "Animal Anatomy and Physiology") == "animal-anatomy-physiology"
    assert f("2014-A", 1, "Анатомия И Физиология Животных") == "animal-anatomy-physiology"
    assert f("2013-A", 1, "Cell-, molecular- and microbiology") == f("2016-A", 1, "Cell Biology") == "cell-biology"
    assert f("2014-A", 1, "Этология") == "ethology"


def test_explicit_assignment_wins_and_unknown_stays_empty():
    # 2015 papers print no section; the data file decides.
    assert sections.group_for("2015-A", 1, None) == "animal-anatomy-physiology"
    # a themed 2017 name tells us nothing, an explicit entry does
    assert sections.group_for("2017-A", 1, "Blueprints Of Life") == "genetics"
    # the old 1/2 spelling of a paper still finds the same entry
    assert sections.group_for("2017-1", 1, "Blueprints Of Life") == "genetics"
    assert sections.group_for("2099-Z", 1, "Mystery") is None
    assert sections.group_for("2099-Z", 1, None) is None


def test_assign_is_idempotent_and_keeps_manual_corrections(db):
    q = make_archive_question(db)  # 2013 A question 1, "Cell-, molecular- and microbiology"
    assert q.section_group is None
    assert sections.assign_section_groups(db) == 1
    db.refresh(q)
    assert q.section_group == "cell-biology"
    q.section_group = "genetics"  # someone corrects it by hand
    db.commit()
    assert sections.assign_section_groups(db) == 0
    db.refresh(q)
    assert q.section_group == "genetics"


def test_filter_uses_the_group_and_lists_each_section_once(client, db):
    q = make_archive_question(db)
    exam = db.get(IboExam, q.exam_id)
    other = IboQuestion(exam_id=exam.id, number=2, label="2", section="Cell Biology", format="true_false",
                        status="active", language="en", parse_notes=[], tags=[], extra={})
    third = IboQuestion(exam_id=exam.id, number=3, label="3", section="Plant anatomy and physiology",
                        format="true_false", status="active", language="en", parse_notes=[], tags=[], extra={})
    db.add_all([other, third])
    db.commit()
    sections.assign_section_groups(db)
    db.commit()
    sign_in(client, make_user(db, "author_s", Role.AUTHOR))
    page = client.get("/creative/archive?lang=en")
    assert page.status_code == 200
    html = page.text
    # two printed spellings of cell biology -> one option; no raw printed names as options
    assert html.count('value="cell-biology"') == 1
    assert 'value="plant-anatomy-physiology"' in html
    assert 'value="Cell-, molecular- and microbiology"' not in html
    only_cell = client.get("/creative/archive?section=cell-biology&lang=en").text
    assert f"/creative/archive/{q.id}" in only_cell and f"/creative/archive/{other.id}" in only_cell
    assert f"/creative/archive/{third.id}" not in only_cell
    # an unknown value is ignored instead of hiding everything
    assert f"/creative/archive/{third.id}" in client.get("/creative/archive?section=bogus").text


def test_draft_topic_comes_from_the_group(db):
    q = make_archive_question(db)
    q.section = "Blueprints Of Life"   # a printed name that matches no topic by itself
    q.section_group = "genetics"
    db.commit()
    author = make_user(db, "drafter", Role.AUTHOR)
    draft = build_draft(db, q, author.id)
    db.flush()
    from app.models.content import Topic
    assert db.get(Topic, draft.topic_id).slug == "genetics-evolution"
