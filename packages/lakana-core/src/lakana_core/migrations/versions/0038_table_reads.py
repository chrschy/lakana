"""table_reads: how often a principal read a table, per day.

Revision ID: d7a4b0e3c9f5
Revises: c6f3a9d2b8e4
Create Date: 2026-10-19 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d7a4b0e3c9f5"
down_revision: str | Sequence[str] | None = "c6f3a9d2b8e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Creates `table_reads`, one row per (table, principal, UTC day). It starts empty: the worker
    counts the reads of the queries that complete from now on.
    """
    op.create_table(
        "table_reads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("read_count", sa.BigInteger(), nullable=False),
        sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["table_id"],
            ["tables.id"],
            name=op.f("fk_table_reads_table_id_tables"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name=op.f("fk_table_reads_principal_id_principals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_table_reads")),
        sa.UniqueConstraint(
            "table_id", "principal_id", "day", name=op.f("uq_table_reads_table_id_principal_id_day")
        ),
    )
    op.create_index(op.f("ix_table_reads_day"), "table_reads", ["day"], unique=False)


def downgrade() -> None:
    """Downgrade schema.

    Drops the counts of reads: the previous release has no place for them.
    """
    op.drop_index(op.f("ix_table_reads_day"), table_name="table_reads")
    op.drop_table("table_reads")
