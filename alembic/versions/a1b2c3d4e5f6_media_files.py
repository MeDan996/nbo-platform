"""store uploaded media in the database

Revision ID: a1b2c3d4e5f6
Revises: 4ed39f5fa6d0
Create Date: 2026-11-02 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '4ed39f5fa6d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('media_files',
    sa.Column('path', sa.String(length=300), nullable=False),
    sa.Column('mime_type', sa.String(length=80), nullable=False),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('data', sa.LargeBinary(), nullable=False),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_media_files_path'), 'media_files', ['path'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_media_files_path'), table_name='media_files')
    op.drop_table('media_files')
