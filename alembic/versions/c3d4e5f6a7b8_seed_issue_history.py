"""record the work done so far in the issues list

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-11-02 16:00:00

"""
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (type, description) - all already finished, oldest first.
HISTORY = [
    ("bug", "Production site went down after the 23 Oct redeploy: the stored deployment snapshot had no start command, so railway.json was never read. The start command and /healthz healthcheck are now set on the Railway service itself."),
    ("change", "Move the database from SQLite to Postgres on Railway. Migrations run automatically on startup and Railway-style postgresql:// URLs are accepted."),
    ("feature", "Create the first admin account automatically on startup from environment variables (the variables have since been removed; the account lives in the database)."),
    ("bug", "Alembic's startup migration switched off the app and uvicorn loggers, so startup messages never reached the Railway logs."),
    ("change", "Store uploaded figure images in the database instead of the container disk, so they survive deploys."),
    ("feature", "Collapsible top menu on phones: a hamburger button expands the navigation, language picker and account actions."),
    ("change", "Creative mode: move the Submit for review and Save buttons to the bottom of the question editor."),
    ("bug", "Draft questions could not be submitted for review on the new database because it had no topics to choose. The seven IBO topics are now created at startup."),
    ("bug", "The Submit your exam dialog showed immediately in practice and could not be closed, so no answers could be given. Hidden elements now stay hidden."),
    ("change", "Creative mode: show an appendix badge on figures and a re-upload notice when a figure's image file is missing."),
    ("change", "Deploy to Railway automatically on every push to master."),
    ("feature", "Issues panel for reporting bugs, changes and feature requests, with a number, description, reporter, type and status, stored in the database."),
    ("change", "Issues: anyone who can open the panel can change an issue's status, and the reporter is shown by account name."),
]


def upgrade() -> None:
    bind = op.get_bind()
    issues = sa.table(
        "issues",
        sa.column("description", sa.Text),
        sa.column("issue_type", sa.String),
        sa.column("status", sa.String),
        sa.column("reporter_id", sa.Integer),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    # Only seed an empty tracker, so a re-run never duplicates the history.
    if bind.execute(sa.text("SELECT COUNT(*) FROM issues")).scalar():
        return
    reporter = bind.execute(
        sa.text("SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1")
    ).scalar()
    now = datetime.now(timezone.utc)
    op.bulk_insert(
        issues,
        [
            {
                "description": description,
                "issue_type": kind,
                "status": "done",
                "reporter_id": reporter,
                "created_at": now,
                "updated_at": now,
            }
            for kind, description in HISTORY
        ],
    )


def downgrade() -> None:
    pass
