"""Users, schools and identity."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, EnumType, UtcDateTime
from app.models.enums import Role

if TYPE_CHECKING:
    from app.models.attempt import Attempt


class School(Base):
    """An institution. Schools may own public IP ranges so that a whole
    computer lab sharing one NAT address is not mistaken for ballot stuffing."""

    __tablename__ = "schools"

    name: Mapped[str] = mapped_column(String(200), index=True)
    short_name: Mapped[str | None] = mapped_column(String(50))
    city: Mapped[str | None] = mapped_column(String(100), index=True)
    region: Mapped[str | None] = mapped_column(String(100), index=True)
    country: Mapped[str] = mapped_column(String(2), default="KG")
    contact_email: Mapped[str | None] = mapped_column(String(255))
    # List of CIDR strings, e.g. ["212.42.101.0/24", "10.0.0.0/8"].
    allowlisted_cidrs: Mapped[list] = mapped_column(JSON, default=list)
    # Above this many accounts from one school IP we still want a human to look.
    max_concurrent_from_ip: Mapped[int] = mapped_column(Integer, default=40)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)

    users: Mapped[list["User"]] = relationship(back_populates="school")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<School {self.name!r}>"


class User(Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(EnumType(Role), default=Role.PARTICIPANT, index=True)

    school_id: Mapped[int | None] = mapped_column(ForeignKey("schools.id", ondelete="SET NULL"))
    school: Mapped[School | None] = relationship(back_populates="users")

    grade: Mapped[int | None] = mapped_column(Integer)  # school year, for age brackets
    locale: Mapped[str] = mapped_column(String(5), default="ru")
    avatar_seed: Mapped[str | None] = mapped_column(String(32))
    bio: Mapped[str | None] = mapped_column(Text)

    # Competitive rating (Codeforces-style, see services/rating.py).
    rating: Mapped[int] = mapped_column(Integer, default=1200, index=True)
    peak_rating: Mapped[int] = mapped_column(Integer, default=1200)
    rated_contests: Mapped[int] = mapped_column(Integer, default=0)
    # Practice-mode mastery, an exponential moving average of accuracy.
    mastery: Mapped[float] = mapped_column(Float, default=0.0)
    streak_days: Mapped[int] = mapped_column(Integer, default=0)
    last_active_date: Mapped[datetime | None] = mapped_column(UtcDateTime)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    ban_reason: Mapped[str | None] = mapped_column(Text)
    email_verified_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_login_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    registration_ip: Mapped[str | None] = mapped_column(String(64), index=True)
    registration_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)

    attempts: Mapped[list["Attempt"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def is_staff(self) -> bool:
        return Role(self.role).rank >= Role.AUTHOR.rank

    @property
    def is_admin(self) -> bool:
        return Role(self.role) is Role.ADMIN

    def can(self, minimum: Role | str) -> bool:
        """Templates pass a plain string (`user.can('reviewer')`), so coerce."""
        return Role(self.role).rank >= Role(minimum).rank

    @property
    def rating_tier(self) -> str:
        """Khan-Academy-ish badge tiers, also used for leaderboard colouring."""
        r = self.rating
        if r >= 2400:
            return "legend"
        if r >= 2100:
            return "master"
        if r >= 1900:
            return "expert"
        if r >= 1600:
            return "specialist"
        if r >= 1350:
            return "apprentice"
        return "novice"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<User {self.username!r} {self.role}>"


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
