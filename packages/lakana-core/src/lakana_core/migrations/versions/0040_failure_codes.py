"""failure_codes: a stable failure code on task runs and their attempts.

Revision ID: f9c6d2a5e1b7
Revises: e8b5c1f4d0a6
Create Date: 2026-10-19 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f9c6d2a5e1b7"
down_revision: str | Sequence[str] | None = "e8b5c1f4d0a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds the nullable `failure_code` to `task_runs` and `task_attempts`. Rows that exist stay
    null: the API shows a failed one as ``unknown``.
    """
    op.add_column("task_runs", sa.Column("failure_code", sa.String(length=32), nullable=True))
    op.add_column("task_attempts", sa.Column("failure_code", sa.String(length=32), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    Drops the codes: the previous release has no place for them.
    """
    with op.batch_alter_table("task_attempts") as batch:
        batch.drop_column("failure_code")
    with op.batch_alter_table("task_runs") as batch:
        batch.drop_column("failure_code")
