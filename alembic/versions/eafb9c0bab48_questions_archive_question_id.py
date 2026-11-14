"""questions archive_question_id

A draft built from an IBO archive question keeps a pointer to it.

Revision ID: eafb9c0bab48
Revises: aaa8a5bed3fc
Create Date: 2026-11-14 22:21:08.156112

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'eafb9c0bab48'
down_revision: Union[str, Sequence[str], None] = 'aaa8a5bed3fc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Link a draft question to the IBO archive question it was built from."""
    with op.batch_alter_table('questions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('archive_question_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_questions_archive_question_id'), ['archive_question_id'], unique=False)
        batch_op.create_foreign_key('fk_questions_archive_question_id', 'ibo_questions',
                                    ['archive_question_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    with op.batch_alter_table('questions', schema=None) as batch_op:
        batch_op.drop_constraint('fk_questions_archive_question_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_questions_archive_question_id'))
        batch_op.drop_column('archive_question_id')
