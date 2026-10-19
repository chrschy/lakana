"""table_retention: when a table, or the tables of an environment, expire.

Revision ID: d3a0b6e9c5f1
Revises: c2f9a5d8b4e0
Create Date: 2026-10-20 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3a0b6e9c5f1"
down_revision: str | Sequence[str] | None = "c2f9a5d8b4e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds the retention of a table (`retention_days`, `retention_on`, `retention_never`, who set
    it and when) and of an environment (`retention_days`, who set it and when). Every column is
    empty (`retention_never` false) on every existing row, which means no retention: nothing
    expires because of the upgrade.
    """
    with op.batch_alter_table("tables") as batch:
        batch.add_column(sa.Column("retention_days", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("retention_on", sa.Date(), nullable=True))
        batch.add_column(
            sa.Column("retention_never", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
        batch.add_column(sa.Column("retention_set_by", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("retention_set_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_foreign_key(
            op.f("fk_tables_retention_set_by_principals"),
            "principals",
            ["retention_set_by"],
            ["id"],
            ondelete="SET NULL",
        )
    with op.batch_alter_table("environments") as batch:
        batch.add_column(sa.Column("retention_days", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("retention_set_by", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("retention_set_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_foreign_key(
            op.f("fk_environments_retention_set_by_principals"),
            "principals",
            ["retention_set_by"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """Downgrade schema.

    Drops the columns: every retention is forgotten, so the previous release expires nothing.
    Tables an expiry already dropped stay dropped (their tombstones are unchanged), and can be
    restored within their undo period as before.
    """
    with op.batch_alter_table("environments") as batch:
        batch.drop_constraint(
            op.f("fk_environments_retention_set_by_principals"), type_="foreignkey"
        )
        batch.drop_column("retention_set_at")
        batch.drop_column("retention_set_by")
        batch.drop_column("retention_days")
    with op.batch_alter_table("tables") as batch:
        batch.drop_constraint(op.f("fk_tables_retention_set_by_principals"), type_="foreignkey")
        batch.drop_column("retention_set_at")
        batch.drop_column("retention_set_by")
        batch.drop_column("retention_never")
        batch.drop_column("retention_on")
        batch.drop_column("retention_days")
