"""table_columns: the columns of every table, view and external table, for search.

Revision ID: e8b5c1f4d0a6
Revises: d7a4b0e3c9f5
Create Date: 2026-10-19 11:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e8b5c1f4d0a6"
down_revision: str | Sequence[str] | None = "d7a4b0e3c9f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Creates `table_columns`, one row per column of a table. It starts empty: every schema
    change from now on writes it, and the worker's housekeeping sweep fills it for the tables
    that exist already, a few per pass.
    """
    op.create_table(
        "table_columns",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=1024), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["table_id"],
            ["tables.id"],
            name=op.f("fk_table_columns_table_id_tables"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_table_columns")),
        sa.UniqueConstraint("table_id", "name", name=op.f("uq_table_columns_table_id_name")),
    )
    op.create_index(op.f("ix_table_columns_name"), "table_columns", ["name"], unique=False)


def downgrade() -> None:
    """Downgrade schema.

    Drops the stored columns: the previous release has no place for them.
    """
    op.drop_index(op.f("ix_table_columns_name"), table_name="table_columns")
    op.drop_table("table_columns")
