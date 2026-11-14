"""Uploaded binary files, stored in the database so they survive redeploys."""
from __future__ import annotations

from sqlalchemy import Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class MediaFile(Base):
    """An uploaded image, addressed by the same relative path as `Figure.path`."""

    __tablename__ = "media_files"

    path: Mapped[str] = mapped_column(String(300), unique=True, index=True)
    mime_type: Mapped[str] = mapped_column(String(80))
    size: Mapped[int] = mapped_column(Integer)
    data: Mapped[bytes] = mapped_column(LargeBinary)
