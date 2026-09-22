"""Regressions for bugs found while exercising the running application."""
from __future__ import annotations

from datetime import timedelta

from app.db import utcnow
from app.models.enums import AttemptStatus, ExamMode, QuestionStatus, Role
from app.models.exam import Exam
from app.services import exam_engine
from app.services.slugs import slugify, unique_exam_slug
from tests.conftest import make_exam, make_question, make_topic, make_user, sign_in


def test_enum_columns_survive_a_database_round_trip(db, SessionFactory):
    """Enum columns must load back as enum members, not bare strings.

    They were plain `String` columns, so a row read from the database returned
    `'in_progress'` rather than `AttemptStatus.IN_PROGRESS`. Every `is`
    comparison against these then evaluated False - silently, and only for
    persisted objects. `submit_attempt` guards on exactly such a comparison, so
    submitting a reloaded attempt quietly did nothing.
    """
    question = make_question(db, topic=make_topic(db))
    exam = make_exam(db, [question], mode=ExamMode.PRACTICE, slug="roundtrip")
    user = make_user(db, "rt", role=Role.AUTHOR)
    attempt = exam_engine.start_attempt(db, user, exam, ip="91.213.10.1", fingerprint="fp")
    db.commit()

    fresh = SessionFactory()
    try:
        from app.models.attempt import Attempt
        from app.models.content import Question

        reloaded = fresh.get(Attempt, attempt.id)
        assert reloaded.status is AttemptStatus.IN_PROGRESS
        assert reloaded.exam.mode is ExamMode.PRACTICE
        assert fresh.get(Question, question.id).status is QuestionStatus.APPROVED
        assert fresh.get(type(user), user.id).role is Role.AUTHOR

        # And the guard that depends on it actually fires.
        exam_engine.submit_attempt(fresh, reloaded)
        assert reloaded.status is AttemptStatus.SUBMITTED
        assert reloaded.submitted_at is not None
    finally:
        fresh.close()


def test_datetimes_come_back_timezone_aware(db, SessionFactory):
    """SQLite drops tzinfo, which made every deadline comparison raise."""
    question = make_question(db, topic=make_topic(db))
    exam = make_exam(db, [question], slug="tz-exam")
    db.commit()

    fresh = SessionFactory()
    try:
        reloaded = fresh.get(Exam, exam.id)
        assert reloaded.opens_at.tzinfo is not None
        assert reloaded.closes_at.tzinfo is not None
        # The comparison that used to raise TypeError.
        assert reloaded.is_open_at() is True
    finally:
        fresh.close()


def test_practice_slugs_do_not_collide_within_one_second(db):
    """Practice slugs embedded a second-resolution timestamp, so two sessions
    started in the same second violated the unique constraint and 500'd."""
    base = "practice-4-1790089000"
    slugs = set()
    for _ in range(12):
        slug = unique_exam_slug(db, base)
        assert slug not in slugs
        db.add(Exam(slug=slug, title="Practice", mode=ExamMode.PRACTICE))
        db.flush()
        slugs.add(slug)
    assert len(slugs) == 12


def test_exam_slugs_with_identical_titles_do_not_collide(db):
    """Each generated slug must be inserted before the next is asked for -
    uniqueness is decided against what is stored, not against what was handed
    out earlier and thrown away."""
    slugs = set()
    for _ in range(6):
        slug = unique_exam_slug(db, "Regional round")
        db.add(Exam(slug=slug, title="Regional round"))
        db.flush()
        slugs.add(slug)
    assert len(slugs) == 6
    assert "regional-round" in slugs  # the readable form is used when free


def test_slugify_handles_unusable_titles():
    assert slugify("Региональный тур") == "exam"  # no latin characters left
    assert slugify("  Hello,  World!  ") == "hello-world"
    assert slugify("") == "exam"


def test_starting_an_exam_commits_before_redirecting(client, db):
    """The redirect target reads the attempt we just created.

    The session commits in dependency teardown, which runs after the handler
    returns, so the browser's follow-up GET could arrive before the write landed
    and render a 404 for the exam it had just started.
    """
    question = make_question(db, topic=make_topic(db))
    exam = make_exam(db, [question], mode=ExamMode.PRACTICE, slug="commit-exam")
    user = make_user(db, "starter")
    sign_in(client, user)

    response = client.post("/survival/commit-exam/start", follow_redirects=False)
    assert response.status_code == 303

    location = response.headers["location"]
    attempt_id = int(location.rsplit("/", 1)[-1])

    # Readable from a session that was not involved in the write.
    from sqlalchemy.orm import sessionmaker

    fresh = sessionmaker(bind=db.get_bind())()
    try:
        from app.models.attempt import Attempt

        assert fresh.get(Attempt, attempt_id) is not None
    finally:
        fresh.close()

    assert client.get(location).status_code == 200


def test_user_can_accepts_a_role_name_as_a_string(db):
    """Templates call `user.can('reviewer')`, which used to raise AttributeError."""
    reviewer = make_user(db, "checker", role=Role.REVIEWER)
    assert reviewer.can("author") is True
    assert reviewer.can("reviewer") is True
    assert reviewer.can("admin") is False
    assert reviewer.can(Role.ADMIN) is False


def test_expired_window_shortens_a_late_start(db):
    """Regression guard for the deadline cap, which depends on tz-aware values."""
    question = make_question(db, topic=make_topic(db))
    exam = make_exam(
        db, [question], slug="short-window",
        duration_minutes=180, closes_at=utcnow() + timedelta(minutes=5),
    )
    user = make_user(db, "latecomer")
    attempt = exam_engine.start_attempt(db, user, exam, ip="91.213.10.1", fingerprint="fp")
    assert attempt.seconds_remaining(utcnow()) <= 300
