"""Issues: bugs, change requests and feature ideas reported by the team."""
from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, LargeBinary, String, Text
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
    attachments: Mapped[list["IssueAttachment"]] = relationship(
        back_populates="issue", cascade="all, delete-orphan", lazy="selectin",
        order_by="IssueAttachment.id",
    )


class IssueAttachment(Base):
    """A screenshot or file attached to an issue, stored in the database."""

    __tablename__ = "issue_attachments"

    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(Integer)
    # Deferred so listing issues does not pull every file into memory.
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, deferred=True)

    issue: Mapped[Issue] = relationship(back_populates="attachments")

    @property
    def is_image(self) -> bool:
        return self.mime_type in ("image/png", "image/jpeg", "image/gif", "image/webp")
