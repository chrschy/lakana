"""table_tombstones: what an undrop needs, and when the sweep began a purge.

Revision ID: c2f9a5d8b4e0
Revises: b1e8f4c7a3d9
Create Date: 2026-10-19 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c2f9a5d8b4e0"
down_revision: str | Sequence[str] | None = "b1e8f4c7a3d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds the nullable `dropped_table` (the dropped row's snapshot) and `purge_started_at` to
    `table_locations`. Every record stays as it is: no table dropped before this revision has
    a snapshot, and nothing is deleted.
    """
    with op.batch_alter_table("table_locations") as batch:
        batch.add_column(sa.Column("purge_started_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("dropped_table", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    Drops both columns. A `DROPPED` record keeps its state and `purge_after`, so the previous
    release's sweep still deletes its files after the undo period; it can no longer be undone.
    """
    with op.batch_alter_table("table_locations") as batch:
        batch.drop_column("dropped_table")
        batch.drop_column("purge_started_at")
