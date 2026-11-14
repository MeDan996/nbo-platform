from app.models.content import Figure
from app.models.enums import QuestionStatus, Role
from app.models.media import MediaFile
from tests.conftest import make_question, make_user, sign_in

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def test_uploaded_appendix_figure_survives_save_and_reopen(client, db):
    author = make_user(db, "author1", Role.AUTHOR)
    question = make_question(db)
    question.author_id = author.id
    question.status = QuestionStatus.DRAFT
    db.commit()
    sign_in(client, author)

    resp = client.post(
        f"/creative/questions/{question.id}/figures",
        files={"file": ("pic.png", PNG, "image/png")},
        data={"in_appendix": "1"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), resp.text[:300]
    figure = db.query(Figure).filter(Figure.question_id == question.id).one()
    assert figure.in_appendix is True
    assert db.query(MediaFile).filter(MediaFile.path == figure.path).count() == 1

    # Save the question form, then reopen the editor.
    client.post(f"/creative/questions/{question.id}", data={"stem__en": "x"}, follow_redirects=False)
    db.expire_all()
    page = client.get(f"/creative/questions/{question.id}")
    assert f'src="/media/{figure.path}"' in page.text
    assert client.get(f"/media/{figure.path}").content == PNG


def test_figure_with_lost_file_is_flagged(client, db):
    author = make_user(db, "author2", Role.AUTHOR)
    question = make_question(db)
    question.author_id = author.id
    question.status = QuestionStatus.DRAFT
    db.add(Figure(question_id=question.id, order=0, path="figures/1/gone.png", mime_type="image/png"))
    db.commit()
    sign_in(client, author)
    db.expire_all()
    page = client.get(f"/creative/questions/{question.id}")
    assert "image file for this figure was lost" in page.text
