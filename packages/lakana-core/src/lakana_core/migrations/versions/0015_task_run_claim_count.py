"""task_runs.claim_count: a monotonic claim counter for the lease guards.

Revision ID: f4b8d2a6c9e1
Revises: e7f2a9c4b1d3
Create Date: 2026-10-03 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f4b8d2a6c9e1"
down_revision: str | Sequence[str] | None = "e7f2a9c4b1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds `task_runs.claim_count`, seeded from `attempt`: a task running across
    the upgrade keeps a distinct value from its next claim, which is all the
    guards need.
    """
    with op.batch_alter_table("task_runs") as batch_op:
        batch_op.add_column(
            sa.Column("claim_count", sa.Integer(), nullable=False, server_default="0")
        )
    op.execute("UPDATE task_runs SET claim_count = attempt")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("task_runs") as batch_op:
        batch_op.drop_column("claim_count")
