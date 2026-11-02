"""Issues: bugs, change requests and feature ideas reported by the team."""
from __future__ import annotations

from sqlalchemy import ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, EnumType
from app.models.enums import IssueStatus, IssueType
from app.models.user import User


class Issue(Base):
    """`id` is the public issue number."""

    __tablename__ = "issues"

    description: Mapped[str] = mapped_column(Text)
    issue_type: Mapped[IssueType] = mapped_column(
        EnumType(IssueType), default=IssueType.BUG, index=True
    )
    status: Mapped[IssueStatus] = mapped_column(
        EnumType(IssueStatus), default=IssueStatus.SUBMITTED, index=True
    )
    # SET NULL keeps the issue if the reporter's account is ever deleted.
    reporter_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    reporter: Mapped[User | None] = relationship(lazy="joined")
