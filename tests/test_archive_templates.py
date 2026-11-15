"""Authors pick an IBO archive question and get a pre-filled, editable draft."""
from app.models.archive import (
    IboAnswer,
    IboAsset,
    IboExam,
    IboExplanation,
    IboItem,
    IboQuestion,
    IboQuestionElement,
)
from app.models.content import Question
from app.models.enums import QuestionStatus, QuestionType, Role
from app.models.media import MediaFile
from app.services.archive_templates import rich_to_markdown, table_to_markdown
from app.services.topics import ensure_topics
from tests.conftest import make_user, sign_in

PNG = b"\x89PNG\r\n\x1a\n" + b"1" * 64


def make_archive_question(db) -> IboQuestion:
    ensure_topics(db)
    exam = IboExam(year=2013, paper_code="A", title="IBO 2013 Theoretical Exam 1", language="en",
                   extra={})
    db.add(exam)
    db.flush()
    q = IboQuestion(exam_id=exam.id, number=1, label="1", section="Cell-, molecular- and microbiology",
                    format="true_false", status="active", language="en", parse_notes=[], tags=[], extra={})
    db.add(q)
    db.flush()
    asset = IboAsset(sha256="ab" * 32, mime="image/png", width=10, height=10, data=PNG)
    db.add(asset)
    db.flush()
    els = [
        IboQuestionElement(question_id=q.id, position=0, role="paragraph", lang="en",
                           text="The <i>trp</i> operon uses tRNA<sup>Trp</sup> and O<sub>2</sub>.",
                           text_plain="The trp operon uses tRNATrp and O2.", attrs={}),
        IboQuestionElement(question_id=q.id, position=1, role="figure", lang="en", asset_id=asset.id,
                           attrs={"labels": ["mRNA"]}),
        IboQuestionElement(question_id=q.id, position=3, role="table", lang="en",
                           attrs={"rows": [["Strain", "Growth"], ["WT", "+"]]}),
        IboQuestionElement(question_id=q.id, position=4, role="instruction", lang="en",
                           text="Indicate if each of the following statements is true or false.", attrs={}),
    ]
    db.add_all(els)
    db.flush()
    db.add(IboQuestionElement(question_id=q.id, position=2, role="legend", lang="en", parent_id=els[1].id,
                              text="Fig. 1. Attenuation.", text_plain="Fig. 1. Attenuation.", attrs={}))
    truths = [False, True, True, False]
    for i, b in enumerate(truths):
        item = IboItem(question_id=q.id, position=i, label="ABCD"[i], item_type="true_false",
                       text=f"Statement {'ABCD'[i]}", text_plain=f"Statement {'ABCD'[i]}", lang="en", attrs={})
        db.add(item)
        db.flush()
        db.add(IboAnswer(item_id=item.id, value={"bool": b}, value_text=str(b), status="official"))
        if i == 0:  # a superseded key must not win
            db.add(IboAnswer(item_id=item.id, value={"bool": True}, value_text="True", status="superseded"))
        db.add(IboExplanation(question_id=q.id, item_id=item.id, position=i, kind="explanation",
                              text=f"Why {'ABCD'[i]}", text_plain=f"Why {'ABCD'[i]}", lang="en"))
    db.add(IboExplanation(question_id=q.id, position=9, kind="reference", text="Walsh 1979",
                          text_plain="Walsh 1979", lang="en"))
    db.commit()
    return q


def test_markup_conversion():
    assert rich_to_markdown("tRNA<sup>Trp</sup> O<sub>2</sub> <i>trpA</i> <b>A. </b>x") == "tRNA^Trp^ O~2~ *trpA* **A.** x"
    assert rich_to_markdown("a &amp; b<br>c") == "a & b\nc"
    assert table_to_markdown([["a", "b"], ["1", "2|3"]]) == "| a | b |\n|---|---|\n| 1 | 2/3 |"


def test_archive_list_and_detail(client, db):
    archive_q = make_archive_question(db)
    author = make_user(db, "author1", Role.AUTHOR)
    sign_in(client, author)

    page = client.get("/creative/archive?year=2013")
    assert page.status_code == 200
    assert f"/creative/archive/{archive_q.id}" in page.text
    detail = client.get(f"/creative/archive/{archive_q.id}")
    assert detail.status_code == 200
    assert "tRNA<sup>Trp</sup>" in detail.text
    assert client.get("/creative/archive?q=operon").text.count(f"/creative/archive/{archive_q.id}/use") == 1
    asset = db.query(IboAsset).one()
    assert client.get(f"/creative/archive/assets/{asset.id}").content == PNG


def test_archive_requires_staff(client, db):
    archive_q = make_archive_question(db)
    sign_in(client, make_user(db, "pupil"))
    assert client.get("/creative/archive").status_code == 403
    assert client.post(f"/creative/archive/{archive_q.id}/use", follow_redirects=False).status_code == 403


def test_use_as_template_prefills_editable_draft(client, db):
    archive_q = make_archive_question(db)
    author = make_user(db, "author1", Role.AUTHOR)
    sign_in(client, author)

    resp = client.post(f"/creative/archive/{archive_q.id}/use", follow_redirects=False)
    assert resp.status_code == 303
    draft = db.query(Question).one()
    assert resp.headers["location"].startswith(f"/creative/questions/{draft.id}")
    assert draft.status is QuestionStatus.DRAFT and draft.author_id == author.id
    assert draft.question_type is QuestionType.TF_BLOCK
    assert draft.archive_question_id == archive_q.id and draft.source_year == 2013
    assert draft.topic is not None and draft.topic.slug == "cell-molecular-biology"

    en = draft.tr("en")
    assert "tRNA^Trp^" in en.stem and "O~2~" in en.stem and "*(Figure 1)*" in en.stem
    assert "| WT | + |" in en.stem
    assert en.instructions.startswith("Indicate if each")
    assert "Walsh 1979" in en.general_explanation
    assert [s.is_true for s in draft.statements] == [False, True, True, False]  # official key, not superseded
    assert [s.text("en") for s in draft.statements][1] == "Statement B"
    assert draft.statements[0].explanation("en") == "Why A"
    assert len(draft.figures) == 1 and draft.figures[0].caption("en") == "Fig. 1. Attenuation."
    assert db.query(MediaFile).filter(MediaFile.path == draft.figures[0].path).one().data == PNG

    editor = client.get(resp.headers["location"])
    assert editor.status_code == 200 and f"/creative/archive/{archive_q.id}" in editor.text

    # The author edits the draft; the archive original stays as it was.
    st = draft.statements[0]
    client.post(f"/creative/questions/{draft.id}", data={
        "stem__en": "A brand new stem", f"st__{st.id}__text__en": "Changed", f"st__{st.id}__true": "on",
    }, follow_redirects=False)
    db.expire_all()
    assert draft.tr("en").stem == "A brand new stem" and draft.statements[0].is_true is True
    assert db.get(IboItem, archive_q.items[0].id).text == "Statement A"

    # Using it again makes a second, independent draft that shares the image.
    client.post(f"/creative/archive/{archive_q.id}/use", follow_redirects=False)
    assert db.query(Question).filter(Question.archive_question_id == archive_q.id).count() == 2
    assert db.query(MediaFile).count() == 1
