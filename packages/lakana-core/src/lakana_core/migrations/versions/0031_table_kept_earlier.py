"""table kept earlier: the marks a kept table carried before its current one, and the snapshot.

Revision ID: c6f9d2a5b8e1
Revises: b5e8c1f4a7d0
Create Date: 2026-10-15 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c6f9d2a5b8e1"
down_revision: str | Sequence[str] | None = "b5e8c1f4a7d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds `tables.kept_earlier`, a JSON list of the marks the table carried before the current
    one, the newest last. Every existing row starts with an empty list: a mark written before this
    has no recorded history, so setting its data back leaves it as it is. Adds
    `tables.kept_snapshot_id`, the snapshot whose verdict the current mark state is; it is null
    for every existing row, except that an unmarked table with a write in flight starts as -1
    (unknown), because a clear that ran before this upgrade may have judged it.
    """
    with op.batch_alter_table("tables") as batch_op:
        batch_op.add_column(
            sa.Column("kept_earlier", sa.JSON(), server_default=sa.text("'[]'"), nullable=False)
        )
        batch_op.add_column(sa.Column("kept_snapshot_id", sa.BigInteger(), nullable=True))
    # A table with no mark and a write in flight may have been judged clean by a check of another
    # run before this upgrade, on data that its own run's later verdict then fails. "No history"
    # (null) would let a set-back trust the unmarked state; -1 (unknown) never does.
    op.execute(
        sa.text(
            "UPDATE tables SET kept_snapshot_id = -1 WHERE kept_run_id IS NULL AND id IN "
            "(SELECT written_table_id FROM task_runs WHERE written_table_id IS NOT NULL)"
        )
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("tables") as batch_op:
        batch_op.drop_column("kept_snapshot_id")
        batch_op.drop_column("kept_earlier")
