"""record the app-wide local-time change as a finished issue

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-11-02 23:00:00

"""
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c9d0e1f2a3b4'
down_revision: Union[str, Sequence[str], None] = 'b8c9d0e1f2a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DESCRIPTION = "Whole app: times are shown in the viewer's own time zone (exam open and close times, attempts, results, rating history, admin logs). Exam open, close and results times are also entered in local time when editing an exam. Everything is still stored in UTC."


def upgrade() -> None:
    bind = op.get_bind()
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
        "description": DESCRIPTION, "issue_type": "change", "status": "done",
        "reporter_id": reporter, "created_at": now, "updated_at": now,
    }])


def downgrade() -> None:
    pass
