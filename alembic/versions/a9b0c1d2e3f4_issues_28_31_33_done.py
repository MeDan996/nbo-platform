"""mark issues 28, 31 and 33 done; record two layout fixes made alongside

Revision ID: a9b0c1d2e3f4
Revises: f8a9b0c1d2e3
Create Date: 2026-11-18 10:00:00

"""
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a9b0c1d2e3f4'
down_revision: Union[str, Sequence[str], None] = 'f8a9b0c1d2e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (issue number, start of its wording). Both must match, so another database's
# issue with the same number is never changed.
DONE = [
    (28, "Question text and statement text have separate language options"),
    (31, "When you in creative mode with ibo archive, to apply filters"),
    (33, "I want to be able to paste screenshots when creating an issue"),
]


# Found while building the above and fixed with it.
EXTRA = [
    ("bug", "The site header stopped sticking after the first screenful of scrolling on every long page, because "
            "the page body was only one screen tall. It now stays at the top for the whole page."),
    ("bug", "On phones, page content ran right up to the screen edges on every page (cards, headings, buttons) "
            "because the side gutters were being reset. Pages now have 20px side margins."),
]


def upgrade() -> None:
    bind = op.get_bind()
    for number, start in DONE:
        bind.execute(
            sa.text("UPDATE issues SET status = 'done' WHERE id = :n AND lower(description) LIKE :p"),
            {"n": number, "p": start.lower() + "%"},
        )
    reporter = bind.execute(
        sa.text("SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1")
    ).scalar()
    now = datetime.now(timezone.utc)
    issues = sa.table(
        "issues",
        sa.column("description", sa.Text),
        sa.column("issue_type", sa.String),
        sa.column("status", sa.String),
        sa.column("reporter_id", sa.Integer),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(issues, [
        {"description": text, "issue_type": kind, "status": "done", "reporter_id": reporter,
         "created_at": now, "updated_at": now}
        for kind, text in EXTRA
    ])


def downgrade() -> None:
    pass
