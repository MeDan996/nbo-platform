"""Anti-cheat records: network identity, device identity, flags and the audit trail.

The design assumption (set by the deployment model) is that participants sit
exams from a mix of school labs and home connections. A whole school shares one
NAT'd public address, so a naive one-account-per-IP block would lock out
legitimate classmates. Instead every signal is recorded, scored, and combined;
hard blocks apply only where an IP is *not* covered by a school allowlist.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, EnumType, UtcDateTime
from app.models.enums import FlagSeverity, FlagStatus

if TYPE_CHECKING:
    from app.models.user import School, User


class IpRecord(Base):
    """One public address the platform has seen, with its reputation."""

    __tablename__ = "ip_records"

    ip_address: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    first_seen: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_seen: Mapped[datetime | None] = mapped_column(UtcDateTime, index=True)

    registration_count: Mapped[int] = mapped_column(Integer, default=0)
    login_count: Mapped[int] = mapped_column(Integer, default=0)
    distinct_user_count: Mapped[int] = mapped_column(Integer, default=0)

    # Set when the address belongs to a verified school lab.
    school_id: Mapped[int | None] = mapped_column(ForeignKey("schools.id", ondelete="SET NULL"))
    school: Mapped["School | None"] = relationship()
    is_allowlisted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_datacenter: Mapped[bool] = mapped_column(Boolean, default=False)  # VPN/proxy suspicion
    country: Mapped[str | None] = mapped_column(String(2))
    notes: Mapped[str | None] = mapped_column(Text)


class DeviceRecord(Base):
    """A browser fingerprint hash. Weaker than an IP alone but much harder to
    change casually, so it catches the 'register a second account' pattern even
    when the student switches networks."""

    __tablename__ = "device_records"

    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    components: Mapped[dict] = mapped_column(JSON, default=dict)
    first_seen: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_seen: Mapped[datetime | None] = mapped_column(UtcDateTime, index=True)
    distinct_user_count: Mapped[int] = mapped_column(Integer, default=0)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)


class UserDeviceLink(Base):
    """Which accounts have used which device, and how often."""

    __tablename__ = "user_device_links"
    __table_args__ = (UniqueConstraint("user_id", "fingerprint", name="uq_user_device"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    first_seen: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_seen: Mapped[datetime | None] = mapped_column(UtcDateTime)
    use_count: Mapped[int] = mapped_column(Integer, default=1)


class UserIpLink(Base):
    """Which accounts have used which address, and how often."""

    __tablename__ = "user_ip_links"
    __table_args__ = (UniqueConstraint("user_id", "ip_address", name="uq_user_ip"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    ip_address: Mapped[str] = mapped_column(String(64), index=True)
    first_seen: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_seen: Mapped[datetime | None] = mapped_column(UtcDateTime)
    use_count: Mapped[int] = mapped_column(Integer, default=1)


class LoginEvent(Base):
    __tablename__ = "login_events"

    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    email_attempted: Mapped[str | None] = mapped_column(String(255), index=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    at: Mapped[datetime | None] = mapped_column(UtcDateTime, index=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), index=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    user_agent: Mapped[str | None] = mapped_column(String(400))
    reason: Mapped[str | None] = mapped_column(String(120))


class CheatFlag(Base):
    """A single fired rule. Flags accumulate into an attempt's risk score and
    land in the admin review queue; only a human voids an attempt."""

    __tablename__ = "cheat_flags"

    attempt_id: Mapped[int | None] = mapped_column(
        ForeignKey("attempts.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    exam_id: Mapped[int | None] = mapped_column(
        ForeignKey("exams.id", ondelete="CASCADE"), index=True
    )
    related_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    rule: Mapped[str] = mapped_column(String(60), index=True)
    severity: Mapped[FlagSeverity] = mapped_column(EnumType(FlagSeverity), default=FlagSeverity.LOW, index=True)
    status: Mapped[FlagStatus] = mapped_column(EnumType(FlagStatus), default=FlagStatus.OPEN, index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)

    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reviewed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    resolution_note: Mapped[str | None] = mapped_column(Text)

    user: Mapped["User | None"] = relationship(foreign_keys=[user_id])
    related_user: Mapped["User | None"] = relationship(foreign_keys=[related_user_id])
    reviewed_by: Mapped["User | None"] = relationship(foreign_keys=[reviewed_by_id])


class ExamAccessDenial(Base):
    """Every refused entry attempt, so a participant wrongly blocked can be
    found and cleared instead of silently losing their sitting."""

    __tablename__ = "exam_access_denials"

    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime | None] = mapped_column(UtcDateTime, index=True)
    rule: Mapped[str] = mapped_column(String(60), index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    ip_address: Mapped[str | None] = mapped_column(String(64))
    fingerprint: Mapped[str | None] = mapped_column(String(64))
    overridden_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    user: Mapped["User"] = relationship(foreign_keys=[user_id])


class ExamAccessGrant(Base):
    """An admin override that lets a specific user past a specific block."""

    __tablename__ = "exam_access_grants"
    __table_args__ = (UniqueConstraint("exam_id", "user_id", name="uq_exam_access_grant"),)

    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    granted_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reason: Mapped[str | None] = mapped_column(Text)
    # Rules this grant waives; empty list waives every automated block.
    waived_rules: Mapped[list] = mapped_column(JSON, default=list)

    user: Mapped["User"] = relationship(foreign_keys=[user_id])


class AuditLog(Base):
    """Who did what, for anything staff-facing or destructive."""

    __tablename__ = "audit_logs"

    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    action: Mapped[str] = mapped_column(String(80), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(60), index=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    ip_address: Mapped[str | None] = mapped_column(String(64))

    actor: Mapped["User | None"] = relationship()
