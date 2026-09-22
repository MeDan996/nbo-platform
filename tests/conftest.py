"""Test fixtures: an isolated database per test, and a client that can sign in."""
from __future__ import annotations

import tempfile
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db, utcnow
from app.main import app
from app.models.content import Question, QuestionTranslation, Statement, StatementTranslation, Topic, TopicTranslation
from app.models.enums import (
    ExamMode,
    ExamStatus,
    QuestionStatus,
    QuestionType,
    Role,
)
from app.models.exam import Exam, ExamQuestion, ExamSection
from app.models.user import School, User
from app.services.auth import hash_password, issue_session


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture()
def SessionFactory(db_path):
    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_connection, _record):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield factory
    engine.dispose()


@pytest.fixture()
def db(SessionFactory):
    session = SessionFactory()
    yield session
    session.rollback()
    session.close()


@pytest.fixture()
def client(SessionFactory, db):
    """A TestClient sharing the test's session, so assertions see its writes."""

    def _override():
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def sign_in(client: TestClient, user: User, fingerprint: str = "fp-test") -> None:
    client.cookies.set("nbo_session", issue_session(user, fingerprint))
    client.cookies.set("nbo_fp", fingerprint)


# --------------------------------------------------------------------------- #
# Factories
#
# These commit rather than just flush. The request-scoped `get_db` override
# rolls back when a route raises - which is exactly what an error-path test
# asserts on - and without a commit here that rollback would also throw away
# the fixtures the assertion then tries to read.
# --------------------------------------------------------------------------- #
def make_user(db, username="student", role=Role.PARTICIPANT, **kwargs) -> User:
    user = User(
        email=kwargs.pop("email", f"{username}@example.test"),
        username=username,
        full_name=kwargs.pop("full_name", username.title()),
        password_hash=hash_password("correct-horse-battery"),
        role=role,
        **kwargs,
    )
    db.add(user)
    db.commit()
    return user


def make_topic(db, slug="genetics-evolution", name="Genetics") -> Topic:
    topic = Topic(slug=slug, order=0)
    db.add(topic)
    db.flush()
    db.add(TopicTranslation(topic_id=topic.id, locale="en", name=name))
    db.add(TopicTranslation(topic_id=topic.id, locale="ru", name=name))
    db.commit()
    return topic


def make_question(db, topic=None, truths=(True, False, True, False), stem="Stem") -> Question:
    question = Question(
        question_type=QuestionType.TF_BLOCK,
        status=QuestionStatus.APPROVED,
        topic_id=topic.id if topic else None,
        max_points=1.0,
    )
    db.add(question)
    db.flush()
    for locale in ("en", "ru", "ky"):
        db.add(QuestionTranslation(question_id=question.id, locale=locale, stem=stem))
    for index, truth in enumerate(truths):
        statement = Statement(
            question_id=question.id, order=index, label="ABCD"[index], is_true=truth
        )
        db.add(statement)
        db.flush()
        for locale in ("en", "ru", "ky"):
            db.add(
                StatementTranslation(
                    statement_id=statement.id,
                    locale=locale,
                    text=f"Statement {'ABCD'[index]}",
                    explanation="Because.",
                )
            )
    db.commit()
    db.refresh(question)
    return question


def make_exam(db, questions, slug="test-exam", **kwargs) -> Exam:
    exam = Exam(
        slug=slug,
        title=kwargs.pop("title", "Test exam"),
        mode=kwargs.pop("mode", ExamMode.OFFICIAL),
        status=kwargs.pop("status", ExamStatus.OPEN),
        duration_minutes=kwargs.pop("duration_minutes", 60),
        max_attempts=kwargs.pop("max_attempts", 1),
        opens_at=kwargs.pop("opens_at", utcnow() - timedelta(hours=1)),
        closes_at=kwargs.pop("closes_at", utcnow() + timedelta(hours=3)),
        **kwargs,
    )
    db.add(exam)
    db.flush()
    section = ExamSection(exam_id=exam.id, title="Section", order=0)
    db.add(section)
    db.flush()
    for order, question in enumerate(questions):
        db.add(
            ExamQuestion(
                exam_id=exam.id,
                section_id=section.id,
                question_id=question.id,
                order=order,
                points=question.max_points,
            )
        )
    db.commit()
    db.refresh(exam)
    return exam
