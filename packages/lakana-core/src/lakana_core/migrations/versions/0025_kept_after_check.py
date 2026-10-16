"""kept after check: the note on a table that keeps data that failed a check.

Revision ID: c9f3d6a8b2e5
Revises: b8e2c5f7a1d4
Create Date: 2026-10-12 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9f3d6a8b2e5"
down_revision: str | Sequence[str] | None = "b8e2c5f7a1d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds the note of a table that was kept after a failed `keep` check: `tables.kept_run_id`
    (the run), `tables.kept_task_run_id` (its check phase) and `tables.kept_check` (the text
    the state message shows for the check), all nullable and without foreign keys, so the
    note outlives its run. A table written before this reads as having no note.
    """
    with op.batch_alter_table("tables") as batch_op:
        batch_op.add_column(sa.Column("kept_run_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("kept_task_run_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("kept_check", sa.String(length=1024), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("tables") as batch_op:
        batch_op.drop_column("kept_check")
        batch_op.drop_column("kept_task_run_id")
        batch_op.drop_column("kept_run_id")
