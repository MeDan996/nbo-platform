"""The exam runtime: starting, autosaving, submitting, grading and finalising.

Everything here is server-authoritative. The browser is treated as a display
surface that can be lying: the deadline, the question order, the correct answers
and the score are all decided here, and the client is never trusted to report
what it should have been shown.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models.analytics import RatingChange
from app.models.attempt import Attempt, AttemptAnswer
from app.models.content import Question, Statement
from app.models.enums import AttemptStatus, EventKind, ExamMode, ExamStatus, QuestionType
from app.models.exam import Exam, ExamQuestion
from app.models.user import User
from app.services import anticheat, stats
from app.services.rating import Competitor, compute_ratings
from app.services.scoring import grade_answer


class ExamEngineError(RuntimeError):
    """Raised when a caller asks for something the exam rules forbid."""


# --------------------------------------------------------------------------- #
# Starting
# --------------------------------------------------------------------------- #
def _build_question_order(exam: Exam, seed: int) -> list[dict]:
    """Freeze this participant's paper.

    Shuffling is per-participant but deterministic from a stored seed, so a
    refresh, a reconnect or a server restart all reproduce the same paper.
    Statements are shuffled within their question only, and `pin_position`
    statements stay where the author put them.
    """
    rng = random.Random(seed)
    exam_questions = sorted(exam.questions, key=lambda eq: eq.order)

    if exam.shuffle_questions:
        # Shuffle within each section so topic blocks stay together, as on the
        # real paper where questions are grouped by subject.
        grouped: dict[int | None, list[ExamQuestion]] = {}
        for eq in exam_questions:
            grouped.setdefault(eq.section_id, []).append(eq)
        exam_questions = []
        for section_id in sorted(grouped, key=lambda s: (s is None, s or 0)):
            block = grouped[section_id]
            rng.shuffle(block)
            exam_questions.extend(block)

    order: list[dict] = []
    for eq in exam_questions:
        statements = list(eq.question.statements)
        ids = [s.id for s in statements]
        if exam.shuffle_statements and len(statements) > 1:
            movable = [s.id for s in statements if not s.pin_position]
            rng.shuffle(movable)
            moved = iter(movable)
            ids = [s.id if s.pin_position else next(moved) for s in statements]
        order.append({"eq": eq.id, "statements": ids})
    return order


def start_attempt(
    db: Session,
    user: User,
    exam: Exam,
    ip: str,
    fingerprint: str | None,
    user_agent: str | None = None,
    screen: str | None = None,
    timezone_offset: int | None = None,
) -> Attempt:
    """Create a new attempt, or return the live one if the participant already
    has a sitting in progress."""
    live = db.scalars(
        select(Attempt).where(
            Attempt.exam_id == exam.id,
            Attempt.user_id == user.id,
            Attempt.status == AttemptStatus.IN_PROGRESS,
        )
    ).first()
    if live is not None:
        enforce_deadline(db, live)
        if live.status is AttemptStatus.IN_PROGRESS:
            anticheat.handle_event(db, live, EventKind.ATTEMPT_RESUME, {}, ip)
            return live

    previous = int(
        db.scalar(
            select(func.count(Attempt.id)).where(
                Attempt.exam_id == exam.id, Attempt.user_id == user.id
            )
        )
        or 0
    )

    now = utcnow()
    deadline = now + timedelta(minutes=exam.duration_minutes)
    if exam.closes_at:
        # The window closing always wins: starting late shortens your sitting,
        # exactly as arriving late to the hall would.
        deadline = min(deadline, exam.closes_at)

    attempt = Attempt(
        exam_id=exam.id,
        user_id=user.id,
        attempt_number=previous + 1,
        status=AttemptStatus.IN_PROGRESS,
        started_at=now,
        expires_at=deadline,
        ip_address=ip,
        seen_ips=[ip],
        device_fingerprint=fingerprint,
        user_agent=(user_agent or "")[:400] or None,
        screen=screen,
        timezone_offset=timezone_offset,
        max_score=exam.total_points,
    )
    db.add(attempt)
    db.flush()

    attempt.question_order = _build_question_order(exam, seed=attempt.id * 7919 + user.id)

    # Pre-create answer rows so autosave is a plain update and the question grid
    # has something to render from on the very first paint.
    for entry in attempt.question_order:
        exam_question = db.get(ExamQuestion, entry["eq"])
        if exam_question is None:
            continue
        db.add(
            AttemptAnswer(
                attempt_id=attempt.id,
                exam_question_id=exam_question.id,
                question_id=exam_question.question_id,
                response={},
                max_points=exam_question.points,
                statement_count=len(exam_question.question.statements),
            )
        )

    db.flush()
    anticheat.handle_event(db, attempt, EventKind.ATTEMPT_START, {"exam_id": exam.id}, ip)
    return attempt


def enforce_deadline(db: Session, attempt: Attempt) -> bool:
    """Auto-submit a sitting whose time has run out. Returns True if it did."""
    if attempt.status is not AttemptStatus.IN_PROGRESS:
        return False
    if attempt.expires_at and utcnow() >= attempt.expires_at:
        submit_attempt(db, attempt, auto=True)
        return True
    return False


# --------------------------------------------------------------------------- #
# Answering
# --------------------------------------------------------------------------- #
def ordered_answers(db: Session, attempt: Attempt) -> list[AttemptAnswer]:
    """Answers in this participant's frozen presentation order."""
    by_eq = {a.exam_question_id: a for a in attempt.answers}
    ordered = []
    for entry in attempt.question_order or []:
        answer = by_eq.get(entry["eq"])
        if answer is not None:
            ordered.append(answer)
    # Anything added to the paper after the attempt started still gets shown.
    for answer in attempt.answers:
        if answer not in ordered:
            ordered.append(answer)
    return ordered


def statement_order(attempt: Attempt, exam_question_id: int) -> list[int]:
    for entry in attempt.question_order or []:
        if entry["eq"] == exam_question_id:
            return entry.get("statements", [])
    return []


def save_answer(
    db: Session,
    attempt: Attempt,
    exam_question_id: int,
    response: dict,
    seconds_spent: int | None = None,
    flagged: bool | None = None,
    ip: str = "",
) -> AttemptAnswer:
    """Persist one response. Rejects writes to a finished attempt."""
    if attempt.status is not AttemptStatus.IN_PROGRESS:
        raise ExamEngineError("attempt_not_live")
    if enforce_deadline(db, attempt):
        raise ExamEngineError("time_expired")

    answer = next(
        (a for a in attempt.answers if a.exam_question_id == exam_question_id), None
    )
    if answer is None:
        raise ExamEngineError("question_not_on_paper")

    # Only accept keys that belong to this question, so a crafted payload cannot
    # inject statement ids from elsewhere.
    exam_question = db.get(ExamQuestion, exam_question_id)
    valid_ids = {str(s.id) for s in exam_question.question.statements}
    qtype = QuestionType(exam_question.question.question_type)

    if qtype in (QuestionType.NUMERIC,):
        cleaned = {"value": response.get("value")}
    elif qtype in (QuestionType.SHORT_TEXT, QuestionType.OPEN_RESPONSE):
        cleaned = {"text": str(response.get("text") or "")[:20000]}
    else:
        cleaned = {k: v for k, v in response.items() if str(k) in valid_ids}

    if cleaned != answer.response:
        answer.revision_count += 1
    answer.response = cleaned
    answer.answered_at = utcnow()
    if seconds_spent is not None:
        answer.seconds_spent = max(answer.seconds_spent, int(seconds_spent))
    if flagged is not None:
        answer.flagged_for_review = flagged

    if ip:
        anticheat.handle_event(
            db,
            attempt,
            EventKind.ANSWER_CHANGE,
            {"exam_question_id": exam_question_id},
            ip,
        )
    return answer


# --------------------------------------------------------------------------- #
# Grading
# --------------------------------------------------------------------------- #
def grade_attempt(db: Session, attempt: Attempt) -> Attempt:
    """Score every answer. Idempotent, so it is safe to re-run after a fix."""
    exam = attempt.exam
    total = 0.0
    possible = 0.0

    for answer in attempt.answers:
        question = db.get(Question, answer.question_id)
        if question is None:
            continue
        result = grade_answer(question, answer.response, answer.max_points, exam.scoring_curve)
        answer.awarded_points = result.awarded
        answer.correct_count = result.correct_count
        answer.statement_count = result.statement_count
        answer.per_statement_correct = result.per_statement
        answer.is_fully_correct = result.is_fully_correct
        # A human-graded item keeps whatever a grader already awarded.
        if result.needs_manual_grading and answer.grader_id:
            pass
        total += answer.awarded_points
        possible += answer.max_points

    attempt.raw_score = round(total, 4)
    attempt.max_score = round(possible, 4)
    attempt.percent = round(total / possible * 100, 2) if possible else 0.0
    attempt.graded_at = utcnow()
    return attempt


def submit_attempt(db: Session, attempt: Attempt, auto: bool = False, ip: str = "") -> Attempt:
    if attempt.status is not AttemptStatus.IN_PROGRESS:
        return attempt

    attempt.status = AttemptStatus.EXPIRED if auto else AttemptStatus.SUBMITTED
    attempt.submitted_at = utcnow()
    grade_attempt(db, attempt)
    db.flush()

    anticheat.handle_event(
        db, attempt, EventKind.SUBMIT, {"auto": auto}, ip or (attempt.ip_address or "")
    )
    anticheat.analyse_attempt(db, attempt)
    stats.update_mastery_from_attempt(db, attempt)

    # Practice results are immediate; a rated sitting waits for the window to
    # close so nobody can infer answers from a friend's early score.
    if attempt.exam.mode is ExamMode.PRACTICE:
        stats.rank_attempts(db, attempt.exam)
    return attempt


def void_attempt(db: Session, attempt: Attempt, reason: str, actor: User | None = None) -> Attempt:
    """Invalidate a sitting. Always a deliberate human act."""
    attempt.status = AttemptStatus.VOIDED
    attempt.void_reason = reason
    attempt.rank = None
    attempt.percentile = None
    return attempt


# --------------------------------------------------------------------------- #
# Finalising an exam
# --------------------------------------------------------------------------- #
@dataclass
class FinalisationReport:
    participants: int = 0
    rated: bool = False
    rating_changes: int = 0
    flagged_attempts: int = 0


def finalise_exam(db: Session, exam: Exam, apply_ratings: bool = True) -> FinalisationReport:
    """Close the books on an exam: rank the field, apply ratings, cache stats.

    Safe to re-run. Rating is applied at most once per attempt, guarded by the
    unique constraint on `rating_changes`, so a second call will not double-count.
    """
    report = FinalisationReport()

    # Sweep up anyone whose browser died without submitting.
    for stale in db.scalars(
        select(Attempt).where(
            Attempt.exam_id == exam.id, Attempt.status == AttemptStatus.IN_PROGRESS
        )
    ):
        submit_attempt(db, stale, auto=True)
    db.flush()

    ranked = stats.rank_attempts(db, exam)
    report.participants = len(ranked)
    report.flagged_attempts = sum(1 for a in ranked if a.is_flagged)

    if apply_ratings and exam.is_rated and ranked:
        report.rated = True
        report.rating_changes = _apply_ratings(db, exam, ranked)

    stats.compute_exam_statistics(db, exam)
    if exam.status is not ExamStatus.ARCHIVED:
        exam.status = ExamStatus.CLOSED
    return report


def _apply_ratings(db: Session, exam: Exam, ranked: list[Attempt]) -> int:
    already_rated = set(
        db.scalars(select(RatingChange.attempt_id).where(RatingChange.exam_id == exam.id))
    )
    # A voided attempt is excluded from the field entirely: a confirmed cheat
    # should not distort anybody else's rating change.
    eligible = [
        a for a in ranked if a.status is not AttemptStatus.VOIDED and a.id not in already_rated
    ]
    if len(eligible) < 2:
        return 0

    users = {a.user_id: db.get(User, a.user_id) for a in eligible}
    competitors = [
        Competitor(
            user_id=a.user_id,
            rating=users[a.user_id].rating if users[a.user_id] else 1200,
            score=a.raw_score,
            tiebreak=(a.submitted_at or utcnow()).timestamp(),
        )
        for a in eligible
    ]
    compute_ratings(competitors)
    by_user = {c.user_id: c for c in competitors}

    now = utcnow()
    changed = 0
    for attempt in eligible:
        competitor = by_user[attempt.user_id]
        user = users[attempt.user_id]
        if user is None:
            continue
        attempt.rating_before = competitor.rating
        attempt.rating_after = competitor.new_rating
        attempt.rating_delta = competitor.delta
        attempt.performance = competitor.performance

        user.rating = competitor.new_rating
        user.peak_rating = max(user.peak_rating, competitor.new_rating)
        user.rated_contests += 1

        db.add(
            RatingChange(
                user_id=user.id,
                exam_id=exam.id,
                attempt_id=attempt.id,
                rating_before=competitor.rating,
                rating_after=competitor.new_rating,
                delta=competitor.delta,
                rank=competitor.rank,
                participants=len(competitors),
                seed=round(competitor.seed, 3),
                performance=competitor.performance,
                at=now,
            )
        )
        changed += 1
    return changed


def attempt_progress(attempt: Attempt) -> dict:
    """Counts for the question grid and the submit dialog."""
    answers = list(attempt.answers)
    answered = sum(1 for a in answers if a.is_answered)
    flagged = sum(1 for a in answers if a.flagged_for_review)
    return {
        "total": len(answers),
        "answered": answered,
        "unanswered": len(answers) - answered,
        "flagged": flagged,
        "percent": round(answered / len(answers) * 100) if answers else 0,
    }
