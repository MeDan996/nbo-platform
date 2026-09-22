"""Administration: exam integrity review, users, schools and exam finalisation."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select

from app.db import utcnow
from app.models.attempt import Attempt, AttemptEvent
from app.models.enums import AttemptStatus, ExamStatus, FlagSeverity, FlagStatus, Role
from app.models.exam import Exam
from app.models.security import (
    AuditLog,
    CheatFlag,
    DeviceRecord,
    ExamAccessDenial,
    ExamAccessGrant,
    IpRecord,
    LoginEvent,
)
from app.models.user import School, User
from app.responses import redirect
from app.services import exam_engine, stats
from app.services.anticheat import recompute_risk
from app.services.auth import require_admin
from app.templating import PageContext, get_context, templates

router = APIRouter(prefix="/admin", tags=["admin"])


def _audit(ctx: PageContext, actor: User, action: str, entity: str, entity_id: int, **payload):
    ctx.db.add(
        AuditLog(
            actor_id=actor.id,
            action=action,
            entity_type=entity,
            entity_id=entity_id,
            payload=payload,
            ip_address=ctx.ip,
        )
    )


@router.get("")
def admin_home(ctx: PageContext = Depends(get_context), admin: User = Depends(require_admin)):
    db = ctx.db
    open_flags = int(
        db.scalar(select(func.count(CheatFlag.id)).where(CheatFlag.status == FlagStatus.OPEN))
        or 0
    )
    critical = int(
        db.scalar(
            select(func.count(CheatFlag.id)).where(
                CheatFlag.status == FlagStatus.OPEN,
                CheatFlag.severity.in_([FlagSeverity.HIGH, FlagSeverity.CRITICAL]),
            )
        )
        or 0
    )
    return templates.page(
        ctx,
        "admin/home.html",
        nav="admin",
        open_flags=open_flags,
        critical_flags=critical,
        denials=int(db.scalar(select(func.count(ExamAccessDenial.id))) or 0),
        users=int(db.scalar(select(func.count(User.id))) or 0),
        schools=int(db.scalar(select(func.count(School.id))) or 0),
        live_attempts=int(
            db.scalar(
                select(func.count(Attempt.id)).where(
                    Attempt.status == AttemptStatus.IN_PROGRESS
                )
            )
            or 0
        ),
        recent_flags=list(
            db.scalars(
                select(CheatFlag)
                .where(CheatFlag.status == FlagStatus.OPEN)
                .order_by(CheatFlag.id.desc())
                .limit(10)
            )
        ),
        exams=list(db.scalars(select(Exam).order_by(Exam.id.desc()).limit(10))),
    )


# --------------------------------------------------------------------------- #
# Integrity queue
# --------------------------------------------------------------------------- #
@router.get("/integrity")
def integrity(
    ctx: PageContext = Depends(get_context),
    admin: User = Depends(require_admin),
    status: str = FlagStatus.OPEN.value,
    exam_id: int | None = None,
):
    stmt = select(CheatFlag).order_by(CheatFlag.severity.desc(), CheatFlag.id.desc())
    if status and status != "all":
        stmt = stmt.where(CheatFlag.status == status)
    if exam_id:
        stmt = stmt.where(CheatFlag.exam_id == exam_id)
    flags = list(ctx.db.scalars(stmt.limit(300)))

    # Sort by severity weight rather than the enum's alphabetical order.
    flags.sort(key=lambda f: -FlagSeverity(f.severity).score)
    attempts = {f.attempt_id: ctx.db.get(Attempt, f.attempt_id) for f in flags if f.attempt_id}
    exams = list(ctx.db.scalars(select(Exam).order_by(Exam.id.desc()).limit(50)))
    return templates.page(
        ctx,
        "admin/integrity.html",
        nav="admin",
        flags=flags,
        attempts=attempts,
        exams=exams,
        status=status,
        exam_id=exam_id,
        statuses=list(FlagStatus),
    )


@router.get("/attempt/{attempt_id}")
def attempt_detail(
    attempt_id: int,
    ctx: PageContext = Depends(get_context),
    admin: User = Depends(require_admin),
):
    """Everything known about one sitting, for a human to judge."""
    attempt = ctx.db.get(Attempt, attempt_id)
    if attempt is None:
        raise HTTPException(status_code=404, detail="common.not_found")

    flags = list(ctx.db.scalars(select(CheatFlag).where(CheatFlag.attempt_id == attempt.id)))
    events = list(
        ctx.db.scalars(
            select(AttemptEvent)
            .where(AttemptEvent.attempt_id == attempt.id)
            .order_by(AttemptEvent.at.desc())
            .limit(200)
        )
    )
    same_ip = list(
        ctx.db.scalars(
            select(Attempt).where(
                Attempt.exam_id == attempt.exam_id,
                Attempt.ip_address == attempt.ip_address,
                Attempt.id != attempt.id,
            )
        )
    )
    same_device = list(
        ctx.db.scalars(
            select(Attempt).where(
                Attempt.exam_id == attempt.exam_id,
                Attempt.device_fingerprint == attempt.device_fingerprint,
                Attempt.id != attempt.id,
            )
        )
        if attempt.device_fingerprint
        else []
    )
    logins = list(
        ctx.db.scalars(
            select(LoginEvent)
            .where(LoginEvent.user_id == attempt.user_id)
            .order_by(LoginEvent.at.desc())
            .limit(20)
        )
    )
    return templates.page(
        ctx,
        "admin/attempt.html",
        nav="admin",
        attempt=attempt,
        flags=flags,
        events=events,
        same_ip=same_ip,
        same_device=same_device,
        logins=logins,
        related_users={
            f.related_user_id: ctx.db.get(User, f.related_user_id)
            for f in flags
            if f.related_user_id
        },
    )


@router.post("/flags/{flag_id}/resolve")
def resolve_flag(
    flag_id: int,
    ctx: PageContext = Depends(get_context),
    admin: User = Depends(require_admin),
    decision: str = Form(...),
    note: str = Form(""),
):
    flag = ctx.db.get(CheatFlag, flag_id)
    if flag is None:
        raise HTTPException(status_code=404, detail="common.not_found")

    flag.status = FlagStatus(decision)
    flag.reviewed_by_id = admin.id
    flag.reviewed_at = utcnow()
    flag.resolution_note = note.strip() or None

    if flag.attempt_id:
        attempt = ctx.db.get(Attempt, flag.attempt_id)
        if attempt is not None:
            recompute_risk(ctx.db, attempt)
    _audit(ctx, admin, f"flag_{decision}", "cheat_flag", flag.id, rule=flag.rule)
    ctx.db.flush()
    return redirect(ctx, ctx.request.headers.get("referer") or "/admin/integrity")


@router.post("/attempts/{attempt_id}/void")
def void_attempt(
    attempt_id: int,
    ctx: PageContext = Depends(get_context),
    admin: User = Depends(require_admin),
    reason: str = Form(...),
):
    """Invalidate a sitting. Ratings are not recomputed automatically - an
    organiser re-finalises the exam when they are done reviewing, so the field
    is corrected once rather than after every individual decision."""
    attempt = ctx.db.get(Attempt, attempt_id)
    if attempt is None:
        raise HTTPException(status_code=404, detail="common.not_found")
    exam_engine.void_attempt(ctx.db, attempt, reason.strip(), actor=admin)
    _audit(ctx, admin, "attempt_voided", "attempt", attempt.id, reason=reason[:300])
    ctx.db.flush()
    return redirect(ctx, f"/admin/attempt/{attempt.id}")


@router.post("/attempts/{attempt_id}/restore")
def restore_attempt(
    attempt_id: int,
    ctx: PageContext = Depends(get_context),
    admin: User = Depends(require_admin),
):
    attempt = ctx.db.get(Attempt, attempt_id)
    if attempt is None:
        raise HTTPException(status_code=404, detail="common.not_found")
    attempt.status = AttemptStatus.SUBMITTED
    attempt.void_reason = None
    _audit(ctx, admin, "attempt_restored", "attempt", attempt.id)
    ctx.db.flush()
    return redirect(ctx, f"/admin/attempt/{attempt.id}")


# --------------------------------------------------------------------------- #
# Blocked entries and overrides
# --------------------------------------------------------------------------- #
@router.get("/denials")
def denials(ctx: PageContext = Depends(get_context), admin: User = Depends(require_admin)):
    rows = list(
        ctx.db.scalars(
            select(ExamAccessDenial).order_by(ExamAccessDenial.id.desc()).limit(200)
        )
    )
    exams = {r.exam_id: ctx.db.get(Exam, r.exam_id) for r in rows}
    return templates.page(ctx, "admin/denials.html", nav="admin", rows=rows, exams=exams)


@router.post("/grants")
def grant_access(
    ctx: PageContext = Depends(get_context),
    admin: User = Depends(require_admin),
    exam_id: int = Form(...),
    user_id: int = Form(...),
    reason: str = Form(""),
):
    """Let one participant past the automated blocks for one exam."""
    existing = ctx.db.scalars(
        select(ExamAccessGrant).where(
            ExamAccessGrant.exam_id == exam_id, ExamAccessGrant.user_id == user_id
        )
    ).first()
    if existing is None:
        ctx.db.add(
            ExamAccessGrant(
                exam_id=exam_id,
                user_id=user_id,
                granted_by_id=admin.id,
                reason=reason.strip() or None,
                waived_rules=[],
            )
        )
    _audit(ctx, admin, "access_granted", "exam", exam_id, user_id=user_id, reason=reason[:300])
    ctx.db.flush()
    return redirect(ctx, "/admin/denials")


# --------------------------------------------------------------------------- #
# Networks and devices
# --------------------------------------------------------------------------- #
@router.get("/networks")
def networks(ctx: PageContext = Depends(get_context), admin: User = Depends(require_admin)):
    ips = list(
        ctx.db.scalars(
            select(IpRecord).order_by(IpRecord.distinct_user_count.desc()).limit(200)
        )
    )
    devices = list(
        ctx.db.scalars(
            select(DeviceRecord)
            .where(DeviceRecord.distinct_user_count > 1)
            .order_by(DeviceRecord.distinct_user_count.desc())
            .limit(100)
        )
    )
    schools = list(ctx.db.scalars(select(School).order_by(School.name)))
    return templates.page(
        ctx, "admin/networks.html", nav="admin", ips=ips, devices=devices, schools=schools
    )


@router.post("/networks/{ip_id}")
def update_ip(
    ip_id: int,
    ctx: PageContext = Depends(get_context),
    admin: User = Depends(require_admin),
    action: str = Form(...),
    school_id: str = Form(""),
):
    record = ctx.db.get(IpRecord, ip_id)
    if record is None:
        raise HTTPException(status_code=404, detail="common.not_found")
    if action == "allowlist":
        record.is_allowlisted = True
        record.is_blocked = False
        record.school_id = int(school_id) if school_id.isdigit() else record.school_id
    elif action == "block":
        record.is_blocked = True
        record.is_allowlisted = False
    elif action == "clear":
        record.is_blocked = False
        record.is_allowlisted = False
    _audit(ctx, admin, f"ip_{action}", "ip_record", record.id, ip=record.ip_address)
    ctx.db.flush()
    return redirect(ctx, "/admin/networks")


# --------------------------------------------------------------------------- #
# Users and schools
# --------------------------------------------------------------------------- #
@router.get("/users")
def user_list(
    ctx: PageContext = Depends(get_context),
    admin: User = Depends(require_admin),
    q: str = "",
):
    stmt = select(User).order_by(User.id.desc())
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(User.email.ilike(like), User.username.ilike(like), User.full_name.ilike(like))
        )
    users = list(ctx.db.scalars(stmt.limit(200)))
    return templates.page(
        ctx, "admin/users.html", nav="admin", users=users, q=q, roles=list(Role)
    )


@router.post("/users/{user_id}")
def update_user(
    user_id: int,
    ctx: PageContext = Depends(get_context),
    admin: User = Depends(require_admin),
    action: str = Form(...),
    role: str = Form(""),
    reason: str = Form(""),
):
    target = ctx.db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="common.not_found")

    if action == "set_role" and role:
        # Guard against an admin removing the last admin and locking everyone out.
        if Role(target.role) is Role.ADMIN and Role(role) is not Role.ADMIN:
            remaining = int(
                ctx.db.scalar(
                    select(func.count(User.id)).where(
                        User.role == Role.ADMIN, User.id != target.id
                    )
                )
                or 0
            )
            if remaining == 0:
                raise HTTPException(status_code=400, detail="cannot_remove_last_admin")
        target.role = Role(role)
    elif action == "ban":
        target.is_banned = True
        target.ban_reason = reason.strip() or None
    elif action == "unban":
        target.is_banned = False
        target.ban_reason = None

    _audit(ctx, admin, f"user_{action}", "user", target.id, role=role, reason=reason[:300])
    ctx.db.flush()
    return redirect(ctx, "/admin/users")


@router.get("/schools")
def school_list(ctx: PageContext = Depends(get_context), admin: User = Depends(require_admin)):
    schools = list(ctx.db.scalars(select(School).order_by(School.name)))
    counts = dict(
        ctx.db.execute(
            select(User.school_id, func.count(User.id)).group_by(User.school_id)
        ).all()
    )
    return templates.page(
        ctx, "admin/schools.html", nav="admin", schools=schools, counts=counts
    )


@router.post("/schools")
def save_school(
    ctx: PageContext = Depends(get_context),
    admin: User = Depends(require_admin),
    school_id: str = Form(""),
    name: str = Form(...),
    city: str = Form(""),
    region: str = Form(""),
    cidrs: str = Form(""),
    max_from_ip: int = Form(40),
    is_verified: str = Form(""),
):
    """Create or update a school, including the networks it owns.

    Registering a school's public address range is what lets a whole computer
    lab sit an exam without tripping the shared-IP block.
    """
    import ipaddress

    parsed: list[str] = []
    for raw in cidrs.replace(",", "\n").split("\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            parsed.append(str(ipaddress.ip_network(raw, strict=False)))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"bad_cidr:{raw}")

    school = ctx.db.get(School, int(school_id)) if school_id.isdigit() else None
    if school is None:
        school = School(name=name.strip()[:200])
        ctx.db.add(school)
    school.name = name.strip()[:200]
    school.city = city.strip()[:100] or None
    school.region = region.strip()[:100] or None
    school.allowlisted_cidrs = parsed
    school.max_concurrent_from_ip = max(1, min(500, int(max_from_ip)))
    school.is_verified = bool(is_verified)
    ctx.db.flush()

    _audit(ctx, admin, "school_saved", "school", school.id, cidrs=parsed)
    return redirect(ctx, "/admin/schools")


# --------------------------------------------------------------------------- #
# Exam lifecycle
# --------------------------------------------------------------------------- #
@router.post("/exams/{exam_id}/finalise")
def finalise(
    exam_id: int,
    ctx: PageContext = Depends(get_context),
    admin: User = Depends(require_admin),
    apply_ratings: str = Form("on"),
):
    exam = ctx.db.get(Exam, exam_id)
    if exam is None:
        raise HTTPException(status_code=404, detail="common.not_found")
    report = exam_engine.finalise_exam(ctx.db, exam, apply_ratings=bool(apply_ratings))
    _audit(
        ctx, admin, "exam_finalised", "exam", exam.id,
        participants=report.participants, rating_changes=report.rating_changes,
    )
    ctx.db.flush()
    return templates.page(
        ctx, "admin/finalised.html", nav="admin", exam=exam, report=report
    )


@router.post("/exams/{exam_id}/release-results")
def release_results(
    exam_id: int,
    ctx: PageContext = Depends(get_context),
    admin: User = Depends(require_admin),
):
    exam = ctx.db.get(Exam, exam_id)
    if exam is None:
        raise HTTPException(status_code=404, detail="common.not_found")
    exam.results_visible_at = utcnow()
    if exam.status is ExamStatus.OPEN:
        exam.status = ExamStatus.CLOSED
    _audit(ctx, admin, "results_released", "exam", exam.id)
    ctx.db.flush()
    return redirect(ctx, f"/survival/{exam.slug}/standings")


@router.get("/exams/{exam_id}/analysis")
def item_analysis(
    exam_id: int,
    ctx: PageContext = Depends(get_context),
    admin: User = Depends(require_admin),
):
    """Classical item analysis: which questions worked, and which misbehaved."""
    exam = ctx.db.get(Exam, exam_id)
    if exam is None:
        raise HTTPException(status_code=404, detail="common.not_found")
    exam_stats = stats.compute_exam_statistics(ctx.db, exam)
    ctx.db.flush()

    rows = []
    for exam_question in sorted(exam.questions, key=lambda eq: eq.order):
        entry = (exam_stats.question_stats or {}).get(str(exam_question.question_id)) or {}
        rows.append(
            {
                "exam_question": exam_question,
                "question": exam_question.question,
                "p": entry.get("p"),
                "d": entry.get("d"),
                "n": entry.get("n", 0),
            }
        )
    return templates.page(
        ctx, "admin/analysis.html", nav="admin", exam=exam, rows=rows, exam_stats=exam_stats
    )
