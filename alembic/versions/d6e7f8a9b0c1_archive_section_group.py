"""archive questions get a common section group

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-11-17 10:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd6e7f8a9b0c1'
down_revision: Union[str, Sequence[str], None] = 'c5d6e7f8a9b0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Schema only. The groups themselves are filled in at start-up by
    # app.archive.sections.assign_section_groups, which is safe to repeat.
    op.add_column('ibo_questions', sa.Column('section_group', sa.String(length=40), nullable=True))
    op.create_index(op.f('ix_ibo_questions_section_group'), 'ibo_questions', ['section_group'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_ibo_questions_section_group'), table_name='ibo_questions')
    op.drop_column('ibo_questions', 'section_group')
