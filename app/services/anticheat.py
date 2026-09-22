"""Anti-cheat: access control before an exam, and forensics after it.

Two ideas shape this module.

**Blocks are narrow, flags are wide.** Participants sit exams from a mix of
school computer labs and home connections. A school lab shares one NAT'd public
address, so "one account per IP" would lock out thirty innocent classmates. Hard
blocks therefore apply only to signals that survive that: a repeat *account* on a
repeat *device*, or many accounts on an address nobody has vouched for. Everything
else records a flag, raises a risk score, and goes to a human review queue.

**Nothing is voided automatically.** Rules fire, evidence accumulates, an admin
decides. `ExamAccessDenial` records every refusal so a participant wrongly blocked
can be found and granted an override instead of silently losing their sitting.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import utcnow
from app.models.attempt import Attempt, AttemptAnswer, AttemptEvent
from app.models.enums import AttemptStatus, EventKind, ExamMode, FlagSeverity, FlagStatus
from app.models.exam import Exam
from app.models.security import (
    CheatFlag,
    DeviceRecord,
    ExamAccessDenial,
    ExamAccessGrant,
    IpRecord,
    UserDeviceLink,
    UserIpLink,
)
from app.models.user import School, User
from app.services.auth import ip_in_cidrs, is_private_ip


# --------------------------------------------------------------------------- #
# Identity bookkeeping
# --------------------------------------------------------------------------- #
def touch_ip(db: Session, ip: str, user: User | None = None, is_registration: bool = False) -> IpRecord:
    """Record that we have seen this address, and keep its account count current."""
    record = db.scalars(select(IpRecord).where(IpRecord.ip_address == ip)).first()
    now = utcnow()
    if record is None:
        record = IpRecord(ip_address=ip, first_seen=now, last_seen=now)
        db.add(record)
        db.flush()
    record.last_seen = now
    record.login_count += 1
    if is_registration:
        record.registration_count += 1

    if user is not None:
        link = db.scalars(
            select(UserIpLink).where(
                UserIpLink.user_id == user.id, UserIpLink.ip_address == ip
            )
        ).first()
        if link is None:
            db.add(UserIpLink(user_id=user.id, ip_address=ip, first_seen=now, last_seen=now))
            db.flush()
            record.distinct_user_count = _count_users_for_ip(db, ip)
        else:
            link.last_seen = now
            link.use_count += 1

    # An address inside a verified school's declared range is trusted by default.
    if not record.is_allowlisted and record.school_id is None:
        school = _school_owning_ip(db, ip)
        if school is not None:
            record.school_id = school.id
            record.is_allowlisted = True
    return record


def touch_device(
    db: Session, fingerprint: str, user: User | None = None, components: dict | None = None
) -> DeviceRecord:
    record = db.scalars(
        select(DeviceRecord).where(DeviceRecord.fingerprint == fingerprint)
    ).first()
    now = utcnow()
    if record is None:
        record = DeviceRecord(
            fingerprint=fingerprint, components=components or {}, first_seen=now, last_seen=now
        )
        db.add(record)
        db.flush()
    record.last_seen = now
    if components:
        record.components = components

    if user is not None:
        link = db.scalars(
            select(UserDeviceLink).where(
                UserDeviceLink.user_id == user.id, UserDeviceLink.fingerprint == fingerprint
            )
        ).first()
        if link is None:
            db.add(
                UserDeviceLink(
                    user_id=user.id, fingerprint=fingerprint, first_seen=now, last_seen=now
                )
            )
            db.flush()
            record.distinct_user_count = _count_users_for_device(db, fingerprint)
        else:
            link.last_seen = now
            link.use_count += 1
    return record


def _count_users_for_ip(db: Session, ip: str) -> int:
    return int(
        db.scalar(select(func.count(UserIpLink.id)).where(UserIpLink.ip_address == ip)) or 0
    )


def _count_users_for_device(db: Session, fingerprint: str) -> int:
    return int(
        db.scalar(
            select(func.count(UserDeviceLink.id)).where(
                UserDeviceLink.fingerprint == fingerprint
            )
        )
        or 0
    )


def _school_owning_ip(db: Session, ip: str) -> School | None:
    for school in db.scalars(select(School).where(School.is_verified.is_(True))):
        if ip_in_cidrs(ip, school.allowlisted_cidrs or []):
            return school
    return None


# --------------------------------------------------------------------------- #
# Pre-exam access control
# --------------------------------------------------------------------------- #
@dataclass
class AccessDecision:
    allowed: bool
    rule: str = ""
    message: str = ""
    # Signals gathered while deciding, carried into the attempt for later review.
    context: dict = field(default_factory=dict)

    @property
    def denied(self) -> bool:
        return not self.allowed


def _waived(db: Session, exam: Exam, user: User, rule: str) -> bool:
    grant = db.scalars(
        select(ExamAccessGrant).where(
            ExamAccessGrant.exam_id == exam.id, ExamAccessGrant.user_id == user.id
        )
    ).first()
    if grant is None:
        return False
    waived = grant.waived_rules or []
    return not waived or rule in waived


def check_exam_access(
    db: Session, user: User, exam: Exam, ip: str, fingerprint: str | None
) -> AccessDecision:
    """Decide whether `user` may start (or resume) `exam` from this IP/device."""
    context = {"ip": ip, "fingerprint": fingerprint}

    if user.is_banned:
        return AccessDecision(False, "account_banned", "account_banned", context)

    if not exam.is_open_at():
        return AccessDecision(False, "exam_closed", "exam_not_open", context)

    if exam.allowed_grades and user.grade not in exam.allowed_grades:
        return AccessDecision(False, "grade_restricted", "grade_not_eligible", context)

    if exam.allowed_school_ids and user.school_id not in exam.allowed_school_ids:
        return AccessDecision(False, "school_restricted", "school_not_eligible", context)

    if exam.registration_required:
        from app.models.exam import ExamEnrollment

        enrolled = db.scalars(
            select(ExamEnrollment).where(
                ExamEnrollment.exam_id == exam.id,
                ExamEnrollment.user_id == user.id,
                ExamEnrollment.approved.is_(True),
            )
        ).first()
        if enrolled is None and not _waived(db, exam, user, "registration_required"):
            return AccessDecision(False, "not_enrolled", "registration_required", context)

    # --- Retake prevention -------------------------------------------------- #
    own_attempts = list(
        db.scalars(
            select(Attempt).where(Attempt.exam_id == exam.id, Attempt.user_id == user.id)
        )
    )
    live = next((a for a in own_attempts if a.status is AttemptStatus.IN_PROGRESS), None)
    if live is not None:
        # Resuming is always allowed; the deadline is enforced server-side.
        return AccessDecision(True, "resume", "", {**context, "attempt_id": live.id})

    finished = [a for a in own_attempts if a.status is not AttemptStatus.VOIDED]
    if len(finished) >= exam.max_attempts and not _waived(db, exam, user, "max_attempts"):
        return AccessDecision(
            False, "max_attempts", "already_attempted", {**context, "attempts": len(finished)}
        )

    # Practice exams stop here: they are unrated and meant to be repeatable.
    if exam.mode is ExamMode.PRACTICE:
        return AccessDecision(True, "", "", context)

    # --- Device reuse ------------------------------------------------------- #
    # The strongest signal: this exact browser already sat this exam as somebody
    # else. Unlike an IP, a fingerprint is not shared by a whole computer lab
    # unless the lab machines are cloned - which is why a lab's addresses get
    # allowlisted and its devices still get checked.
    if fingerprint and exam.policy("block_duplicate_device", True):
        device = db.scalars(
            select(DeviceRecord).where(DeviceRecord.fingerprint == fingerprint)
        ).first()
        if device is not None and device.is_blocked:
            return AccessDecision(False, "device_blocked", "device_blocked", context)

        other = db.scalars(
            select(Attempt)
            .where(
                Attempt.exam_id == exam.id,
                Attempt.device_fingerprint == fingerprint,
                Attempt.user_id != user.id,
                Attempt.status != AttemptStatus.VOIDED,
            )
            .limit(1)
        ).first()
        if other is not None and not _waived(db, exam, user, "duplicate_device"):
            return AccessDecision(
                False,
                "duplicate_device",
                "device_already_used",
                {**context, "other_attempt_id": other.id, "other_user_id": other.user_id},
            )

    # --- Address reuse ------------------------------------------------------ #
    if exam.policy("block_duplicate_ip", True) and not is_private_ip(ip):
        record = db.scalars(select(IpRecord).where(IpRecord.ip_address == ip)).first()
        if record is not None and record.is_blocked:
            return AccessDecision(False, "ip_blocked", "ip_blocked", context)

        allowlisted = bool(record and record.is_allowlisted) or (
            _school_owning_ip(db, ip) is not None
        )
        distinct_accounts = int(
            db.scalar(
                select(func.count(func.distinct(Attempt.user_id))).where(
                    Attempt.exam_id == exam.id,
                    Attempt.ip_address == ip,
                    Attempt.user_id != user.id,
                    Attempt.status != AttemptStatus.VOIDED,
                )
            )
            or 0
        )
        context["ip_accounts"] = distinct_accounts
        context["ip_allowlisted"] = allowlisted

        threshold = int(
            exam.policy("ip_account_threshold", settings.ip_shared_account_threshold)
        )
        if allowlisted:
            # A verified lab: no block, but the school's own ceiling still flags.
            school = record.school if record and record.school else _school_owning_ip(db, ip)
            ceiling = school.max_concurrent_from_ip if school else 40
            if distinct_accounts >= ceiling:
                return AccessDecision(
                    False,
                    "school_ip_ceiling",
                    "too_many_from_school_ip",
                    {**context, "ceiling": ceiling},
                )
        elif (
            distinct_accounts >= threshold
            and settings.ip_hard_block_unallowlisted
            and not _waived(db, exam, user, "duplicate_ip")
        ):
            return AccessDecision(
                False,
                "duplicate_ip",
                "too_many_from_ip",
                {**context, "threshold": threshold},
            )

    return AccessDecision(True, "", "", context)


def record_denial(
    db: Session, user: User, exam: Exam, decision: AccessDecision
) -> ExamAccessDenial:
    denial = ExamAccessDenial(
        exam_id=exam.id,
        user_id=user.id,
        at=utcnow(),
        rule=decision.rule,
        message=decision.message,
        ip_address=decision.context.get("ip"),
        fingerprint=decision.context.get("fingerprint"),
    )
    db.add(denial)
    return denial


# --------------------------------------------------------------------------- #
# Flags
# --------------------------------------------------------------------------- #
def raise_flag(
    db: Session,
    *,
    attempt: Attempt | None,
    user_id: int | None,
    exam_id: int | None,
    rule: str,
    severity: FlagSeverity,
    message: str,
    details: dict | None = None,
    related_user_id: int | None = None,
) -> CheatFlag:
    """Add a flag unless an identical open one already exists for this attempt."""
    # The session runs with autoflush off, so pending inserts are invisible to a
    # SELECT until they are flushed explicitly. Without this, a rule that fires
    # twice in one request would insert a duplicate flag.
    db.flush()

    if attempt is not None:
        existing = db.scalars(
            select(CheatFlag).where(
                CheatFlag.attempt_id == attempt.id,
                CheatFlag.rule == rule,
                CheatFlag.related_user_id == related_user_id,
            )
        ).first()
        if existing is not None:
            existing.details = {**(existing.details or {}), **(details or {})}
            return existing

    flag = CheatFlag(
        attempt_id=attempt.id if attempt else None,
        user_id=user_id,
        exam_id=exam_id,
        related_user_id=related_user_id,
        rule=rule,
        severity=severity,
        status=FlagStatus.OPEN,
        message=message,
        details=details or {},
    )
    db.add(flag)
    db.flush()
    return flag


def recompute_risk(db: Session, attempt: Attempt) -> int:
    """Risk is the sum of open/confirmed flag severities, capped at 100."""
    db.flush()
    flags = list(
        db.scalars(
            select(CheatFlag).where(
                CheatFlag.attempt_id == attempt.id,
                CheatFlag.status.in_([FlagStatus.OPEN, FlagStatus.REVIEWING, FlagStatus.CONFIRMED]),
            )
        )
    )
    total = sum(FlagSeverity(f.severity).score for f in flags)
    attempt.risk_score = min(100, total)
    attempt.is_flagged = attempt.risk_score > 0
    return attempt.risk_score


# --------------------------------------------------------------------------- #
# Live monitoring, called as telemetry arrives
# --------------------------------------------------------------------------- #
def handle_event(db: Session, attempt: Attempt, kind: EventKind, payload: dict, ip: str) -> None:
    """Persist one telemetry event and fire any rule it trips."""
    exam = attempt.exam
    db.add(
        AttemptEvent(
            attempt_id=attempt.id, kind=kind, at=utcnow(), payload=payload or {}, ip_address=ip
        )
    )

    if kind in (EventKind.WINDOW_BLUR, EventKind.TAB_HIDDEN):
        attempt.focus_losses += 1
        limit = int(exam.policy("max_focus_losses", 5))
        if exam.policy("track_focus_loss", True) and attempt.focus_losses > limit:
            raise_flag(
                db,
                attempt=attempt,
                user_id=attempt.user_id,
                exam_id=attempt.exam_id,
                rule="excessive_focus_loss",
                severity=FlagSeverity.MEDIUM,
                message="left_exam_window_repeatedly",
                details={"count": attempt.focus_losses, "limit": limit},
            )

    elif kind is EventKind.PASTE and exam.policy("block_copy_paste", True):
        raise_flag(
            db,
            attempt=attempt,
            user_id=attempt.user_id,
            exam_id=attempt.exam_id,
            rule="paste_detected",
            severity=FlagSeverity.LOW,
            message="content_pasted_into_answer",
            details={"length": payload.get("length")},
        )

    elif kind is EventKind.FULLSCREEN_EXIT and exam.policy("require_fullscreen", False):
        raise_flag(
            db,
            attempt=attempt,
            user_id=attempt.user_id,
            exam_id=attempt.exam_id,
            rule="fullscreen_exit",
            severity=FlagSeverity.MEDIUM,
            message="left_fullscreen_mode",
        )

    # An address change mid-exam can be a phone switching towers, or it can be a
    # second person picking up the session. Low severity; the review queue sorts it.
    if ip and attempt.ip_address and ip != attempt.ip_address:
        seen = list(attempt.seen_ips or [])
        if ip not in seen:
            seen.append(ip)
            attempt.seen_ips = seen
        if len(seen) > 2:
            raise_flag(
                db,
                attempt=attempt,
                user_id=attempt.user_id,
                exam_id=attempt.exam_id,
                rule="ip_hopping",
                severity=FlagSeverity.LOW,
                message="multiple_networks_during_attempt",
                details={"ips": seen},
            )

    recompute_risk(db, attempt)


# --------------------------------------------------------------------------- #
# Post-submission forensics
# --------------------------------------------------------------------------- #
def analyse_attempt(db: Session, attempt: Attempt) -> list[CheatFlag]:
    """Run every after-the-fact rule over a finished attempt."""
    flags: list[CheatFlag] = []
    answers = list(attempt.answers)

    # 1. Superhuman pace: answered questions far faster than the floor.
    answered = [a for a in answers if a.is_answered]
    if answered:
        too_fast = [a for a in answered if 0 < a.seconds_spent < settings.min_seconds_per_question]
        if len(too_fast) >= max(5, len(answered) // 3):
            flags.append(
                raise_flag(
                    db,
                    attempt=attempt,
                    user_id=attempt.user_id,
                    exam_id=attempt.exam_id,
                    rule="impossible_pace",
                    severity=FlagSeverity.MEDIUM,
                    message="many_questions_answered_implausibly_fast",
                    details={
                        "fast_answers": len(too_fast),
                        "answered": len(answered),
                        "floor_seconds": settings.min_seconds_per_question,
                    },
                )
            )

    # 2. A high score delivered in a small fraction of the allotted time.
    if attempt.started_at and attempt.submitted_at and attempt.max_score:
        elapsed = (attempt.submitted_at - attempt.started_at).total_seconds()
        allotted = attempt.exam.duration_minutes * 60
        if allotted and elapsed < allotted * 0.2 and attempt.percent >= 80:
            flags.append(
                raise_flag(
                    db,
                    attempt=attempt,
                    user_id=attempt.user_id,
                    exam_id=attempt.exam_id,
                    rule="fast_high_score",
                    severity=FlagSeverity.HIGH,
                    message="high_score_in_very_short_time",
                    details={"elapsed_seconds": int(elapsed), "percent": attempt.percent},
                )
            )

    # 3. Shared device or address with another sitting of the same exam.
    if attempt.device_fingerprint:
        siblings = list(
            db.scalars(
                select(Attempt).where(
                    Attempt.exam_id == attempt.exam_id,
                    Attempt.device_fingerprint == attempt.device_fingerprint,
                    Attempt.user_id != attempt.user_id,
                    Attempt.status != AttemptStatus.VOIDED,
                )
            )
        )
        for sibling in siblings:
            flags.append(
                raise_flag(
                    db,
                    attempt=attempt,
                    user_id=attempt.user_id,
                    exam_id=attempt.exam_id,
                    related_user_id=sibling.user_id,
                    rule="shared_device",
                    severity=FlagSeverity.CRITICAL,
                    message="same_browser_as_another_participant",
                    details={"other_attempt_id": sibling.id},
                )
            )

    # 4. Answer-pattern collusion.
    flags.extend(detect_collusion(db, attempt))

    recompute_risk(db, attempt)
    return [f for f in flags if f is not None]


def _response_vector(answers: list[AttemptAnswer]) -> dict[str, tuple]:
    """Flatten an attempt into {statement_id: (given, was_correct)} pairs."""
    vector: dict[str, tuple] = {}
    for answer in answers:
        correctness = answer.per_statement_correct or {}
        for statement_id, given in (answer.response or {}).items():
            if given is None:
                continue
            vector[str(statement_id)] = (given, bool(correctness.get(str(statement_id))))
    return vector


def similarity_index(a: dict[str, tuple], b: dict[str, tuple]) -> dict:
    """Harpp-Hogan style index over two response vectors.

    Two strong students agreeing on every correct answer proves nothing. What
    does carry signal is *matching wrong answers*: the same specific error made
    by both. The index is

        H = (exact errors in common) / (number of answers that differ)

    A value at or above 1.0 alongside a meaningful count of shared errors is the
    published threshold for "worth investigating by hand". This is evidence for a
    human reviewer, never a verdict on its own.
    """
    shared = set(a) & set(b)
    if not shared:
        return {"errors_in_common": 0, "differences": 0, "index": 0.0, "overlap": 0}

    errors_in_common = 0
    differences = 0
    for key in shared:
        given_a, correct_a = a[key]
        given_b, correct_b = b[key]
        if given_a == given_b:
            if not correct_a and not correct_b:
                errors_in_common += 1
        else:
            differences += 1

    index = errors_in_common / differences if differences else float(errors_in_common)
    return {
        "errors_in_common": errors_in_common,
        "differences": differences,
        "index": round(index, 3),
        "overlap": len(shared),
    }


def detect_collusion(db: Session, attempt: Attempt, min_errors: int = 6) -> list[CheatFlag]:
    """Compare this attempt against others on the same exam."""
    exam = attempt.exam
    if not exam.policy("detect_collusion", True):
        return []

    mine = _response_vector(list(attempt.answers))
    if len(mine) < 8:
        return []

    others = db.scalars(
        select(Attempt).where(
            Attempt.exam_id == attempt.exam_id,
            Attempt.id != attempt.id,
            Attempt.status.in_(
                [AttemptStatus.SUBMITTED, AttemptStatus.EXPIRED, AttemptStatus.GRADED]
            ),
        )
    )

    flags: list[CheatFlag] = []
    for other in others:
        stats = similarity_index(mine, _response_vector(list(other.answers)))
        if stats["errors_in_common"] < min_errors or stats["index"] < 1.0:
            continue

        severity = FlagSeverity.HIGH if stats["index"] >= 2.0 else FlagSeverity.MEDIUM
        shared_context = (
            attempt.ip_address
            and attempt.ip_address == other.ip_address
            or attempt.device_fingerprint
            and attempt.device_fingerprint == other.device_fingerprint
        )
        if shared_context:
            severity = FlagSeverity.CRITICAL

        flags.append(
            raise_flag(
                db,
                attempt=attempt,
                user_id=attempt.user_id,
                exam_id=attempt.exam_id,
                related_user_id=other.user_id,
                rule="answer_similarity",
                severity=severity,
                message="matching_wrong_answers_with_another_participant",
                details={**stats, "other_attempt_id": other.id, "shared_context": bool(shared_context)},
            )
        )
    return flags


def registration_signals(db: Session, ip: str, fingerprint: str | None) -> list[dict]:
    """Warnings shown to an admin about a newly created account."""
    signals: list[dict] = []
    window_start = utcnow() - timedelta(days=1)

    if not is_private_ip(ip):
        recent = int(
            db.scalar(
                select(func.count(UserIpLink.id)).where(
                    UserIpLink.ip_address == ip, UserIpLink.first_seen >= window_start
                )
            )
            or 0
        )
        if recent >= settings.ip_shared_account_threshold:
            signals.append(
                {"rule": "ip_registration_burst", "count": recent, "ip": ip, "severity": "medium"}
            )

    if fingerprint:
        count = _count_users_for_device(db, fingerprint)
        if count >= 2:
            signals.append(
                {"rule": "device_multi_account", "count": count, "severity": "high"}
            )
    return signals
