"""Exam-integrity rules.

Addresses in these tests are deliberately real public ones. Python treats the
RFC 5737 documentation ranges (192.0.2/24, 198.51.100/24, 203.0.113/24) as
private, and the IP rules skip private addresses by design, so using them here
would make every network test pass vacuously.

The central design tension: a school computer lab shares one public address, so
blocking on a shared IP would lock out a whole class. These tests pin down that
an allowlisted school network is exempt while an unvouched one is not.
"""
from __future__ import annotations

from datetime import timedelta

from app.db import utcnow
from app.models.attempt import Attempt
from app.models.enums import AttemptStatus, EventKind, ExamMode, FlagSeverity
from app.models.security import CheatFlag, ExamAccessGrant, IpRecord
from app.models.user import School
from app.services import anticheat
from app.services.anticheat import (
    check_exam_access,
    handle_event,
    recompute_risk,
    similarity_index,
)
from tests.conftest import make_exam, make_question, make_user


def _sat(db, exam, user, ip, fingerprint, status=AttemptStatus.SUBMITTED) -> Attempt:
    attempt = Attempt(
        exam_id=exam.id,
        user_id=user.id,
        status=status,
        started_at=utcnow() - timedelta(minutes=30),
        submitted_at=utcnow(),
        ip_address=ip,
        device_fingerprint=fingerprint,
        seen_ips=[ip],
    )
    db.add(attempt)
    db.flush()
    return attempt


def test_first_attempt_is_allowed(db):
    question = make_question(db)
    exam = make_exam(db, [question])
    user = make_user(db)
    assert check_exam_access(db, user, exam, "91.213.10.5", "fp-a").allowed


def test_second_attempt_is_refused_once_the_limit_is_reached(db):
    question = make_question(db)
    exam = make_exam(db, [question], max_attempts=1)
    user = make_user(db)
    _sat(db, exam, user, "91.213.10.5", "fp-a")

    decision = check_exam_access(db, user, exam, "91.213.10.5", "fp-a")
    assert decision.denied
    assert decision.rule == "max_attempts"


def test_a_live_attempt_can_always_be_resumed(db):
    question = make_question(db)
    exam = make_exam(db, [question], max_attempts=1)
    user = make_user(db)
    _sat(db, exam, user, "91.213.10.5", "fp-a", status=AttemptStatus.IN_PROGRESS)

    decision = check_exam_access(db, user, exam, "91.213.10.9", "fp-a")
    assert decision.allowed
    assert decision.rule == "resume"


def test_second_account_on_the_same_device_is_refused(db):
    """The core retake defence: a new account in the same browser."""
    question = make_question(db)
    exam = make_exam(db, [question])
    first = make_user(db, "first")
    second = make_user(db, "second")
    _sat(db, exam, first, "91.213.10.5", "same-device")

    decision = check_exam_access(db, second, exam, "77.88.55.7", "same-device")
    assert decision.denied
    assert decision.rule == "duplicate_device"


def test_unvouched_network_blocks_beyond_the_threshold(db):
    question = make_question(db)
    exam = make_exam(db, [question])
    exam.anticheat_policy = {**exam.anticheat_policy, "ip_account_threshold": 3}
    db.flush()

    home_ip = "77.88.55.20"
    for index in range(3):
        _sat(db, exam, make_user(db, f"sibling{index}"), home_ip, f"fp-{index}")

    newcomer = make_user(db, "newcomer")
    decision = check_exam_access(db, newcomer, exam, home_ip, "fp-new")
    assert decision.denied
    assert decision.rule == "duplicate_ip"


def test_allowlisted_school_lab_is_not_blocked(db):
    """Thirty classmates behind one NAT address must all get to sit the paper."""
    school = School(
        name="Lyceum 61",
        allowlisted_cidrs=["212.42.101.0/24"],
        is_verified=True,
        max_concurrent_from_ip=40,
    )
    db.add(school)
    db.flush()

    question = make_question(db)
    exam = make_exam(db, [question])
    exam.anticheat_policy = {**exam.anticheat_policy, "ip_account_threshold": 3}
    db.flush()

    lab_ip = "212.42.101.17"
    for index in range(12):
        _sat(db, exam, make_user(db, f"pupil{index}"), lab_ip, f"lab-fp-{index}")

    newcomer = make_user(db, "pupil99", school_id=school.id)
    decision = check_exam_access(db, newcomer, exam, lab_ip, "lab-fp-99")
    assert decision.allowed, decision.rule
    assert decision.context["ip_allowlisted"] is True


def test_school_lab_still_has_a_ceiling(db):
    school = School(
        name="Small lab",
        allowlisted_cidrs=["212.42.102.0/24"],
        is_verified=True,
        max_concurrent_from_ip=3,
    )
    db.add(school)
    db.flush()

    question = make_question(db)
    exam = make_exam(db, [question])
    lab_ip = "212.42.102.5"
    for index in range(3):
        _sat(db, exam, make_user(db, f"small{index}"), lab_ip, f"small-fp-{index}")

    decision = check_exam_access(db, make_user(db, "small99"), exam, lab_ip, "small-fp-99")
    assert decision.denied
    assert decision.rule == "school_ip_ceiling"


def test_admin_grant_overrides_a_block(db):
    """A participant wrongly blocked must be recoverable, not simply lost."""
    question = make_question(db)
    exam = make_exam(db, [question])
    first = make_user(db, "first")
    second = make_user(db, "second")
    _sat(db, exam, first, "91.213.10.5", "shared-family-pc")

    assert check_exam_access(db, second, exam, "91.213.10.5", "shared-family-pc").denied

    db.add(ExamAccessGrant(exam_id=exam.id, user_id=second.id, waived_rules=[]))
    db.flush()
    assert check_exam_access(db, second, exam, "91.213.10.5", "shared-family-pc").allowed


def test_practice_mode_skips_device_and_network_checks(db):
    question = make_question(db)
    exam = make_exam(db, [question], mode=ExamMode.PRACTICE, max_attempts=5)
    first = make_user(db, "first")
    second = make_user(db, "second")
    _sat(db, exam, first, "91.213.10.5", "same-device")

    assert check_exam_access(db, second, exam, "91.213.10.5", "same-device").allowed


def test_blocked_network_is_refused(db):
    question = make_question(db)
    exam = make_exam(db, [question])
    db.add(IpRecord(ip_address="91.213.10.99", is_blocked=True))
    db.flush()

    decision = check_exam_access(db, make_user(db), exam, "91.213.10.99", "fp")
    assert decision.denied
    assert decision.rule == "ip_blocked"


def test_closed_exam_is_refused(db):
    question = make_question(db)
    exam = make_exam(db, [question], closes_at=utcnow() - timedelta(minutes=5))
    decision = check_exam_access(db, make_user(db), exam, "91.213.10.5", "fp")
    assert decision.denied
    assert decision.rule == "exam_closed"


# --------------------------------------------------------------------------- #
# Telemetry and risk
# --------------------------------------------------------------------------- #
def test_repeated_focus_loss_raises_a_flag_and_risk(db):
    question = make_question(db)
    exam = make_exam(db, [question])
    exam.anticheat_policy = {**exam.anticheat_policy, "max_focus_losses": 2}
    db.flush()

    user = make_user(db)
    attempt = _sat(db, exam, user, "91.213.10.5", "fp", status=AttemptStatus.IN_PROGRESS)

    for _ in range(4):
        handle_event(db, attempt, EventKind.WINDOW_BLUR, {}, "91.213.10.5")

    flags = db.query(CheatFlag).filter(CheatFlag.attempt_id == attempt.id).all()
    assert [f.rule for f in flags] == ["excessive_focus_loss"]
    assert attempt.focus_losses == 4
    assert attempt.risk_score == FlagSeverity.MEDIUM.score
    assert attempt.is_flagged is True


def test_risk_is_capped_and_recomputed_from_open_flags(db):
    question = make_question(db)
    exam = make_exam(db, [question])
    attempt = _sat(db, exam, make_user(db), "91.213.10.5", "fp")

    for rule in ("shared_device", "fast_high_score", "impossible_pace"):
        anticheat.raise_flag(
            db,
            attempt=attempt,
            user_id=attempt.user_id,
            exam_id=exam.id,
            rule=rule,
            severity=FlagSeverity.CRITICAL,
            message=rule,
        )
    assert recompute_risk(db, attempt) == 100

    for flag in db.query(CheatFlag).filter(CheatFlag.attempt_id == attempt.id):
        flag.status = "dismissed"
    db.flush()
    assert recompute_risk(db, attempt) == 0
    assert attempt.is_flagged is False


def test_duplicate_flags_are_merged_not_repeated(db):
    question = make_question(db)
    exam = make_exam(db, [question])
    attempt = _sat(db, exam, make_user(db), "91.213.10.5", "fp")

    for count in (1, 2, 3):
        anticheat.raise_flag(
            db,
            attempt=attempt,
            user_id=attempt.user_id,
            exam_id=exam.id,
            rule="excessive_focus_loss",
            severity=FlagSeverity.MEDIUM,
            message="x",
            details={"count": count},
        )
    flags = db.query(CheatFlag).filter(CheatFlag.attempt_id == attempt.id).all()
    assert len(flags) == 1
    assert flags[0].details["count"] == 3


# --------------------------------------------------------------------------- #
# Collusion
# --------------------------------------------------------------------------- #
def test_similarity_ignores_agreement_on_correct_answers():
    """Two strong candidates agreeing on right answers proves nothing."""
    a = {str(i): (True, True) for i in range(20)}
    b = {str(i): (True, True) for i in range(20)}
    stats = similarity_index(a, b)
    assert stats["errors_in_common"] == 0
    assert stats["index"] == 0.0


def test_similarity_flags_identical_wrong_answers():
    """Matching *errors* are the signal - the Harpp-Hogan index."""
    a = {str(i): (True, False) for i in range(10)}
    b = {str(i): (True, False) for i in range(10)}
    stats = similarity_index(a, b)
    assert stats["errors_in_common"] == 10
    assert stats["differences"] == 0
    assert stats["index"] == 10.0


def test_similarity_index_falls_as_answers_diverge():
    a = {str(i): (True, False) for i in range(10)}
    b = dict(a)
    for i in range(6):
        b[str(i)] = (False, False)  # different answers on six items
    stats = similarity_index(a, b)
    assert stats["errors_in_common"] == 4
    assert stats["differences"] == 6
    assert stats["index"] < 1.0  # below the investigate-by-hand threshold
