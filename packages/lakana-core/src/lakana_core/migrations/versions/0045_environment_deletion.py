"""environment_deletion: a deleted environment stays as a row until its tables' files are gone.

Revision ID: ca4f68b6e89a
Revises: d3a0b6e9c5f1
Create Date: 2026-10-20 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ca4f68b6e89a"
down_revision: str | Sequence[str] | None = "d3a0b6e9c5f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds what a deleted environment keeps until the location sweep removes it: when it was
    deleted and by whom (`deleted_at`, `deleted_by`), from when on its files may go
    (`purge_after`), and what a restore brings back (`dropped_location_ids`,
    `revoked_policy_ids`). Every column is empty on every existing row: no environment is
    deleted by the upgrade. An environment deleted before it left no row, so nothing changes
    for it.
    """
    with op.batch_alter_table("environments") as batch:
        batch.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("deleted_by", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("purge_after", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("dropped_location_ids", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("revoked_policy_ids", sa.JSON(), nullable=True))
        batch.create_foreign_key(
            op.f("fk_environments_deleted_by_principals"),
            "principals",
            ["deleted_by"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """Downgrade schema.

    Drops the columns. An environment deleted since the upgrade shows up again in the previous
    release, without its tables: each one stays a tombstone, which `lakana table undrop`
    restores within its undo period, and whose files the previous release's sweep deletes
    after it, as for any dropped table. No file is deleted earlier than before the downgrade.
    """
    with op.batch_alter_table("environments") as batch:
        batch.drop_constraint(op.f("fk_environments_deleted_by_principals"), type_="foreignkey")
        batch.drop_column("revoked_policy_ids")
        batch.drop_column("dropped_location_ids")
        batch.drop_column("purge_after")
        batch.drop_column("deleted_by")
        batch.drop_column("deleted_at")
