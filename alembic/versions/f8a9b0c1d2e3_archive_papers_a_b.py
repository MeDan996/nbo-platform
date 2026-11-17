"""archive papers numbered 1 and 2 become A and B

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-11-17 14:00:00

"""
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'f8a9b0c1d2e3'
down_revision: Union[str, Sequence[str], None] = 'e7f8a9b0c1d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RENAMES = {"1": "A", "2": "B"}
DESCRIPTION = (
    "IBO archive: the papers numbered 1 and 2 (2015, 2017, 2018) are the same two papers as A and B "
    "in the other years, so they are now A and B everywhere. The Paper filter lists only A and B, "
    "and the source label on drafts started from those papers says A or B."
)


def upgrade() -> None:
    bind = op.get_bind()
    exams = bind.execute(
        sa.text("SELECT id, year, paper_code FROM ibo_exams WHERE paper_code IN ('1', '2')")
    ).fetchall()
    for exam_id, year, old in exams:
        new = RENAMES[old]
        clash = bind.execute(
            sa.text("SELECT 1 FROM ibo_exams WHERE year = :y AND paper_code = :c"), {"y": year, "c": new}
        ).first()
        if clash:
            continue  # that year already has its own A/B; never merge two real papers
        bind.execute(sa.text("UPDATE ibo_exams SET paper_code = :c WHERE id = :i"), {"c": new, "i": exam_id})
        # Labels already stored on drafts started from this paper: "IBO 2017 1 Q. 3" -> "IBO 2017 A Q. 3".
        old_prefix, new_prefix = f"IBO {year} {old} ", f"IBO {year} {new} "
        for table, column in (("questions", "source_ref"), ("question_translations", "title")):
            bind.execute(
                sa.text(
                    f"UPDATE {table} SET {column} = :np || substr({column}, :cut) WHERE {column} LIKE :pat"
                ),
                {"np": new_prefix, "cut": len(old_prefix) + 1, "pat": old_prefix + "%"},
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
        "description": DESCRIPTION, "issue_type": "change", "status": "done",
        "reporter_id": reporter, "created_at": now, "updated_at": now,
    }])


def downgrade() -> None:
    pass
