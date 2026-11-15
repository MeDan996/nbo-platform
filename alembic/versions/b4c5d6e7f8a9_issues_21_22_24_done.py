"""mark issues 21, 22 and 24 done; record the Enter-key fix

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-11-16 10:00:00

"""
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b4c5d6e7f8a9'
down_revision: Union[str, Sequence[str], None] = 'a3b4c5d6e7f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (issue number, start of its wording). Both must match, so another database's
# issue with the same number is never changed.
DONE = [
    (21, "In creative mode, figures are coming after statements"),
    (22, "In creative mode, when editing figures of a question"),
    (24, "in creative mode, the preview is without figures"),
]
TICKET = (
    "bug",
    "Question editor: pressing Enter in a text field added a new statement instead of saving. "
    "Enter now saves the question. Fixed together with the figure layout changes.",
)


def upgrade() -> None:
    bind = op.get_bind()
    for number, start in DONE:
        bind.execute(
            sa.text(
                "UPDATE issues SET status = 'done' WHERE id = :n AND lower(description) LIKE :p"
            ),
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
    op.bulk_insert(issues, [{
        "description": TICKET[1], "issue_type": TICKET[0], "status": "done",
        "reporter_id": reporter, "created_at": now, "updated_at": now,
    }])


def downgrade() -> None:
    pass
