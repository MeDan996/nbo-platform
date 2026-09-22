"""Slug generation for exams."""
from __future__ import annotations

import re
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.exam import Exam


def slugify(text: str, fallback: str = "exam") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:80] or fallback


def unique_exam_slug(db: Session, base: str) -> str:
    """A slug that is free right now.

    `exams.slug` is unique, and a timestamp is not enough on its own: two
    practice sessions started in the same second produce the same base and the
    second one fails the constraint with a 500. Checking and suffixing keeps the
    readable form when it is available and degrades to a random suffix when it
    is not.
    """
    base = slugify(base)
    candidate = base
    for _ in range(8):
        taken = db.scalars(select(Exam.id).where(Exam.slug == candidate).limit(1)).first()
        if taken is None:
            return candidate
        candidate = f"{base}-{secrets.token_hex(3)}"
    return f"{base}-{secrets.token_hex(8)}"
