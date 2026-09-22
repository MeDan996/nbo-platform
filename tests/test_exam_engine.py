"""The exam runtime: starting, autosaving, deadlines, submission and grading."""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.db import utcnow
from app.models.analytics import RatingChange, TopicMastery
from app.models.enums import AttemptStatus, ExamMode
from app.services import exam_engine
from app.services.exam_engine import ExamEngineError
from tests.conftest import make_exam, make_question, make_topic, make_user


def _paper(db, count=3, truths=(True, False, True, False), **exam_kwargs):
    topic = make_topic(db)
    questions = [make_question(db, topic=topic, truths=truths) for _ in range(count)]
    return make_exam(db, questions, **exam_kwargs), questions


def test_starting_creates_answer_rows_and_freezes_the_order(db):
    exam, questions = _paper(db)
    user = make_user(db)
    attempt = exam_engine.start_attempt(db, user, exam, ip="91.213.10.1", fingerprint="fp")

    assert attempt.status is AttemptStatus.IN_PROGRESS
    assert len(attempt.answers) == len(questions)
    assert len(attempt.question_order) == len(questions)
    assert attempt.max_score == pytest.approx(exam.total_points)


def test_restarting_returns_the_same_live_attempt(db):
    """A refresh or a reconnect must never hand out a second paper."""
    exam, _ = _paper(db)
    user = make_user(db)
    first = exam_engine.start_attempt(db, user, exam, ip="91.213.10.1", fingerprint="fp")
    second = exam_engine.start_attempt(db, user, exam, ip="91.213.10.1", fingerprint="fp")
    assert first.id == second.id


def test_question_order_is_stable_across_reloads(db):
    exam, _ = _paper(db, count=6, shuffle_questions=True, shuffle_statements=True)
    user = make_user(db)
    attempt = exam_engine.start_attempt(db, user, exam, ip="91.213.10.1", fingerprint="fp")
    frozen = [entry["eq"] for entry in attempt.question_order]

    db.expire(attempt)
    reloaded = exam_engine.ordered_answers(db, attempt)
    assert [a.exam_question_id for a in reloaded] == frozen


def test_deadline_is_capped_by_the_exam_window(db):
    """Starting late shortens your sitting - the window close always wins."""
    closes = utcnow() + timedelta(minutes=10)
    exam, _ = _paper(db, duration_minutes=180, closes_at=closes)
    user = make_user(db)
    attempt = exam_engine.start_attempt(db, user, exam, ip="91.213.10.1", fingerprint="fp")
    assert attempt.expires_at == closes
    assert attempt.seconds_remaining(utcnow()) <= 600


def test_saving_only_accepts_statements_from_that_question(db):
    """A crafted payload must not be able to write keys from another question."""
    exam, questions = _paper(db)
    user = make_user(db)
    attempt = exam_engine.start_attempt(db, user, exam, ip="91.213.10.1", fingerprint="fp")

    target = attempt.answers[0]
    own = questions[0].statements[0].id
    foreign = questions[1].statements[0].id

    exam_engine.save_answer(
        db,
        attempt,
        exam_question_id=target.exam_question_id,
        response={str(own): True, str(foreign): True, "9999": True},
    )
    assert set(target.response) == {str(own)}


def test_saving_after_submission_is_refused(db):
    exam, _ = _paper(db)
    user = make_user(db)
    attempt = exam_engine.start_attempt(db, user, exam, ip="91.213.10.1", fingerprint="fp")
    exam_engine.submit_attempt(db, attempt)

    with pytest.raises(ExamEngineError):
        exam_engine.save_answer(
            db, attempt, exam_question_id=attempt.answers[0].exam_question_id, response={}
        )


def test_expired_attempt_is_auto_submitted(db):
    exam, _ = _paper(db)
    user = make_user(db)
    attempt = exam_engine.start_attempt(db, user, exam, ip="91.213.10.1", fingerprint="fp")

    attempt.expires_at = utcnow() - timedelta(seconds=1)
    db.flush()

    assert exam_engine.enforce_deadline(db, attempt) is True
    assert attempt.status is AttemptStatus.EXPIRED
    assert attempt.submitted_at is not None


def test_writing_after_the_deadline_is_refused(db):
    exam, _ = _paper(db)
    user = make_user(db)
    attempt = exam_engine.start_attempt(db, user, exam, ip="91.213.10.1", fingerprint="fp")
    attempt.expires_at = utcnow() - timedelta(seconds=1)
    db.flush()

    with pytest.raises(ExamEngineError):
        exam_engine.save_answer(
            db,
            attempt,
            exam_question_id=attempt.answers[0].exam_question_id,
            response={},
        )


def test_full_marks_end_to_end(db):
    exam, questions = _paper(db, count=4)
    user = make_user(db)
    attempt = exam_engine.start_attempt(db, user, exam, ip="91.213.10.1", fingerprint="fp")

    for answer in attempt.answers:
        statements = answer.exam_question.question.statements
        exam_engine.save_answer(
            db,
            attempt,
            exam_question_id=answer.exam_question_id,
            response={str(s.id): s.is_true for s in statements},
        )

    exam_engine.submit_attempt(db, attempt)
    assert attempt.raw_score == pytest.approx(4.0)
    assert attempt.percent == pytest.approx(100.0)
    assert all(a.is_fully_correct for a in attempt.answers)


def test_three_of_four_earns_the_curve_value(db):
    exam, questions = _paper(db, count=1)
    user = make_user(db)
    attempt = exam_engine.start_attempt(db, user, exam, ip="91.213.10.1", fingerprint="fp")

    answer = attempt.answers[0]
    statements = answer.exam_question.question.statements
    response = {str(s.id): s.is_true for s in statements}
    response[str(statements[0].id)] = not statements[0].is_true  # one wrong

    exam_engine.save_answer(db, attempt, answer.exam_question_id, response)
    exam_engine.submit_attempt(db, attempt)
    assert attempt.raw_score == pytest.approx(0.6)


def test_submission_updates_topic_mastery(db):
    """Statement-level mastery is what the weaknesses view is built from."""
    topic = make_topic(db)
    questions = [make_question(db, topic=topic) for _ in range(2)]
    exam = make_exam(db, questions)
    user = make_user(db)

    attempt = exam_engine.start_attempt(db, user, exam, ip="91.213.10.1", fingerprint="fp")
    for answer in attempt.answers:
        statements = answer.exam_question.question.statements
        exam_engine.save_answer(
            db,
            attempt,
            answer.exam_question_id,
            {str(s.id): s.is_true for s in statements},
        )
    exam_engine.submit_attempt(db, attempt)

    row = (
        db.query(TopicMastery)
        .filter(TopicMastery.user_id == user.id, TopicMastery.topic_id == topic.id)
        .one()
    )
    assert row.statements_seen == 8
    assert row.statements_correct == 8
    assert row.accuracy == pytest.approx(1.0)


def test_submitting_twice_does_not_double_count(db):
    exam, _ = _paper(db)
    user = make_user(db)
    attempt = exam_engine.start_attempt(db, user, exam, ip="91.213.10.1", fingerprint="fp")
    exam_engine.submit_attempt(db, attempt)
    score = attempt.raw_score
    submitted_at = attempt.submitted_at

    exam_engine.submit_attempt(db, attempt)
    assert attempt.raw_score == score
    assert attempt.submitted_at == submitted_at


# --------------------------------------------------------------------------- #
# Finalisation
# --------------------------------------------------------------------------- #
def _sit(db, exam, user, correct_fraction: float):
    attempt = exam_engine.start_attempt(
        db, user, exam, ip=f"91.213.10.{user.id}", fingerprint=f"fp{user.id}"
    )
    for index, answer in enumerate(attempt.answers):
        statements = answer.exam_question.question.statements
        want_right = index < len(attempt.answers) * correct_fraction
        response = {
            str(s.id): (s.is_true if want_right else not s.is_true) for s in statements
        }
        exam_engine.save_answer(db, attempt, answer.exam_question_id, response)
    exam_engine.submit_attempt(db, attempt)
    return attempt


def test_finalise_ranks_the_field_and_applies_ratings(db):
    exam, _ = _paper(db, count=10, mode=ExamMode.OFFICIAL, is_rated=True)
    strong = make_user(db, "strong")
    middle = make_user(db, "middle")
    weak = make_user(db, "weak")

    a = _sit(db, exam, strong, 1.0)
    b = _sit(db, exam, middle, 0.6)
    c = _sit(db, exam, weak, 0.2)

    report = exam_engine.finalise_exam(db, exam)

    assert report.participants == 3
    assert report.rating_changes == 3
    assert (a.rank, b.rank, c.rank) == (1, 2, 3)
    assert a.raw_score > b.raw_score > c.raw_score
    # Finishing first from an equal starting rating must not cost you rating.
    assert a.rating_delta > c.rating_delta
    assert strong.rating > weak.rating


def test_finalising_twice_does_not_apply_ratings_again(db):
    exam, _ = _paper(db, count=6, mode=ExamMode.OFFICIAL, is_rated=True)
    users = [make_user(db, f"u{i}") for i in range(4)]
    for index, user in enumerate(users):
        _sit(db, exam, user, 1.0 - index * 0.25)

    exam_engine.finalise_exam(db, exam)
    ratings = {u.id: u.rating for u in users}
    changes = db.query(RatingChange).count()

    exam_engine.finalise_exam(db, exam)
    assert db.query(RatingChange).count() == changes
    assert {u.id: u.rating for u in users} == ratings


def test_finalise_sweeps_up_abandoned_attempts(db):
    exam, _ = _paper(db, count=3)
    user = make_user(db)
    attempt = exam_engine.start_attempt(db, user, exam, ip="91.213.10.1", fingerprint="fp")
    assert attempt.status is AttemptStatus.IN_PROGRESS

    exam_engine.finalise_exam(db, exam)
    assert attempt.status is AttemptStatus.EXPIRED


def test_voided_attempt_is_excluded_from_ratings(db):
    exam, _ = _paper(db, count=6, mode=ExamMode.OFFICIAL, is_rated=True)
    honest = [make_user(db, f"honest{i}") for i in range(3)]
    cheat = make_user(db, "cheat")

    for index, user in enumerate(honest):
        _sit(db, exam, user, 0.9 - index * 0.3)
    bad = _sit(db, exam, cheat, 1.0)

    exam_engine.void_attempt(db, bad, "confirmed collusion")
    db.flush()

    report = exam_engine.finalise_exam(db, exam)
    assert report.rating_changes == 3
    assert cheat.rating == 1200  # untouched
    assert bad.rank is None
