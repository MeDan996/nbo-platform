"""record the phone-overflow fix as a finished issue

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-11-02 20:00:00

"""
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, Sequence[str], None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DESCRIPTION = "Creative mode on phones: the bottom link row (Question bank, IBO templates, Review queue, Exams) stretched the page wider than the screen, hiding part of it unless zoomed out. The page now fits and the links wrap."


def upgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text("SELECT id FROM issues WHERE description = :d"), {"d": DESCRIPTION}
    ).scalar():
        return
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
    op.bulk_insert(issues, [{
        "description": DESCRIPTION, "issue_type": "bug", "status": "done",
        "reporter_id": reporter, "created_at": now, "updated_at": now,
    }])


def downgrade() -> None:
    pass
