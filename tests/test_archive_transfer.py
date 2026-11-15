import gzip
import io

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.archive import transfer
from app.config import settings
from app.db import Base
from app.models.archive import IboAsset, IboItem, IboQuestion, IboQuestionElement
from app.models.enums import Role
from tests.conftest import make_user, sign_in
from tests.test_archive_templates import make_archive_question


def _empty_engine(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'target.db').as_posix()}")

    @event.listens_for(engine, "connect")
    def _fk(dbapi, _rec):
        cur = dbapi.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(bind=engine)
    return engine


def _dump(db) -> bytes:
    return b"".join(transfer.iter_gzip(db.connection()))


def test_round_trip_into_an_empty_database(db, tmp_path):
    source_q = make_archive_question(db)
    # a forward link (item -> element) and a derived question, the two circular kinds
    element = db.scalars(select(IboQuestionElement).where(IboQuestionElement.role == "figure")).one()
    item = db.scalars(select(IboItem).order_by(IboItem.id)).first()
    item.element_id = element.id
    twin = IboQuestion(exam_id=source_q.exam_id, number=2, label="2", section="x", format="true_false",
                       status="active", language="en", parse_notes=[], tags=[], extra={},
                       derived_from_id=source_q.id)
    db.add(twin)
    db.commit()
    source_counts = transfer.counts(db.connection())
    dump = _dump(db)
    assert gzip.decompress(dump).startswith(b'{"format": "nbo-archive-v1"')

    target = _empty_engine(tmp_path)
    with target.begin() as conn:
        loaded = transfer.load(conn, io.BytesIO(dump))
    assert loaded == source_counts
    assert sum(loaded.values()) > 15

    with target.connect() as conn:
        assert transfer.counts(conn) == source_counts
        asset = conn.execute(select(IboAsset.data)).scalar_one()
        assert asset == db.scalars(select(IboAsset.data)).one()  # image bytes survive
        parent_links = conn.execute(
            select(IboQuestionElement.id).where(IboQuestionElement.parent_id.is_not(None))
        ).all()
        assert len(parent_links) == 1
        assert conn.execute(select(IboItem.element_id).where(IboItem.element_id.is_not(None))).scalar() == element.id
        assert conn.execute(select(IboQuestion.derived_from_id).where(IboQuestion.derived_from_id.is_not(None))).scalar() == source_q.id


def test_loading_replaces_what_was_there(db, tmp_path):
    make_archive_question(db)
    dump = _dump(db)
    target = _empty_engine(tmp_path)
    with target.begin() as conn:
        transfer.load(conn, io.BytesIO(dump))
    with target.begin() as conn:
        transfer.load(conn, io.BytesIO(dump))  # again: no duplicates, no key errors
    with target.connect() as conn:
        assert transfer.counts(conn) == transfer.counts(db.connection())


def test_rejects_something_that_is_not_a_dump(db):
    junk = io.BytesIO(gzip.compress(b'{"hello": "world"}\n'))
    try:
        transfer.load(db.connection(), junk)
    except ValueError as exc:
        assert "not an NBO archive dump" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_export_is_admin_only(client, db):
    anonymous = client.get("/admin/archive/export", follow_redirects=False)
    assert anonymous.status_code == 303
    assert anonymous.headers["location"].startswith("/auth/login")
    sign_in(client, make_user(db, "auth1", Role.AUTHOR))
    assert client.get("/admin/archive/export").status_code == 403


def test_admin_can_export(client, db, monkeypatch, SessionFactory):
    make_archive_question(db)
    monkeypatch.setattr("app.routers.archive_transfer.SessionLocal", SessionFactory)
    sign_in(client, make_user(db, "root1", Role.ADMIN))
    response = client.get("/admin/archive/export")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/gzip"
    assert "nbo-archive-" in response.headers["content-disposition"]
    assert b'"t": "ibo_questions"' in gzip.decompress(response.content)


def test_import_refused_on_production(client, db):
    make_archive_question(db)
    dump = _dump(db)
    sign_in(client, make_user(db, "root2", Role.ADMIN))
    assert settings.env_label == ""
    response = client.post("/admin/archive/import", files={"file": ("a.jsonl.gz", dump)})
    assert response.status_code == 403
    assert db.query(IboQuestion).count() == 1  # nothing was touched


def test_import_on_a_labelled_environment(client, db, monkeypatch):
    make_archive_question(db)
    dump = _dump(db)
    db.query(IboQuestion).delete()
    db.commit()
    monkeypatch.setattr(settings, "env_label", "dev")
    sign_in(client, make_user(db, "root3", Role.ADMIN))
    # non-admins are still refused
    sign_in(client, make_user(db, "auth2", Role.AUTHOR))
    assert client.post("/admin/archive/import", files={"file": ("a.jsonl.gz", dump)}).status_code == 403
    sign_in(client, db.query(__import__("app.models.user", fromlist=["User"]).User).filter_by(username="root3").one())
    response = client.post("/admin/archive/import", files={"file": ("a.jsonl.gz", dump)})
    assert response.status_code == 200, response.text
    assert response.json()["rows"] > 10
    assert db.query(IboQuestion).count() == 1
    bad = client.post("/admin/archive/import", files={"file": ("a.jsonl.gz", b"not gzip")})
    assert bad.status_code == 400
