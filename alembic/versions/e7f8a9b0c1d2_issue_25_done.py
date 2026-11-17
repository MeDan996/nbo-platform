"""mark issue 25 (archive section duplicates) as done

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-11-17 11:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e7f8a9b0c1d2'
down_revision: Union[str, Sequence[str], None] = 'd6e7f8a9b0c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Matched by number and wording so a different database's #25 is never touched.
    op.get_bind().execute(sa.text(
        "UPDATE issues SET status = 'done' "
        "WHERE id = 25 AND lower(description) LIKE 'in creative mode, there are a lot of duplicates%'"
    ))


def downgrade() -> None:
    pass
