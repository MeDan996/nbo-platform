"""split user names into first name and surname; set up the owner account

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-11-02 18:00:00

"""
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_ADMIN_EMAIL = "admin@nbo.kg"
OWNER_EMAIL = "dayan.melisov@gmail.com"
TICKET = (
    "change",
    "Users now have a separate first name and surname, and the platform username is "
    "independent of the email address. Sign-in accepts the username or the email "
    "(username is not case sensitive). The owner account is now dayan.melisov@gmail.com, "
    "username admin, Dayan Melisov.",
)


def upgrade() -> None:
    op.add_column('users', sa.Column('first_name', sa.String(length=100), nullable=False, server_default=''))
    op.add_column('users', sa.Column('last_name', sa.String(length=100), nullable=False, server_default=''))
    bind = op.get_bind()

    # Existing accounts: first word is the first name, the rest the surname.
    users = bind.execute(sa.text("SELECT id, full_name FROM users")).fetchall()
    for user_id, full_name in users:
        first, _, last = (full_name or "").strip().partition(" ")
        bind.execute(
            sa.text("UPDATE users SET first_name = :f, last_name = :l WHERE id = :i"),
            {"f": first[:100], "l": last.strip()[:100], "i": user_id},
        )

    # Owner account: only touched if the original admin exists and the new email is free.
    admin_id = bind.execute(
        sa.text("SELECT id FROM users WHERE email = :e"), {"e": OLD_ADMIN_EMAIL}
    ).scalar()
    taken = bind.execute(
        sa.text("SELECT id FROM users WHERE email = :e"), {"e": OWNER_EMAIL}
    ).scalar()
    if admin_id is not None and taken is None:
        bind.execute(
            sa.text(
                "UPDATE users SET email = :e, username = 'admin', first_name = 'Dayan', "
                "last_name = 'Melisov', full_name = 'Dayan Melisov' WHERE id = :i"
            ),
            {"e": OWNER_EMAIL, "i": admin_id},
        )

    # Record the work as a finished ticket.
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
    op.drop_column('users', 'last_name')
    op.drop_column('users', 'first_name')
