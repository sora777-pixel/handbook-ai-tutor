"""tombstone column for deleted quiz versions

Lets a learner drop one generated practice set while keeping its answer archive:
quiz_attempts.quiz_id is a NOT NULL foreign key, so the version row has to stay
alive for the records list to remain readable on PostgreSQL.

Revision ID: 0002_quiz_deleted_at
Revises: 0001_initial
Create Date: 2026-09-23
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_quiz_deleted_at"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "quizzes",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("quizzes", "deleted_at")
