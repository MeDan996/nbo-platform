"""mark issue 23 (edit issues after submission) as done

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-11-15 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e1f2a3b4c5d6'
down_revision: Union[str, Sequence[str], None] = 'd0e1f2a3b4c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Matched by number and wording so a different database's #23 is never touched.
    op.get_bind().execute(sa.text(
        "UPDATE issues SET status = 'done' "
        "WHERE id = 23 AND description LIKE 'I want to be able to change the issue after submission%'"
    ))


def downgrade() -> None:
    pass
