"""descriptions: the text of tables and columns, from the project and from an edit.

Revision ID: c6f3a9d2b8e4
Revises: b5e2d8f1a7c3
Create Date: 2026-10-18 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c6f3a9d2b8e4"
down_revision: str | Sequence[str] | None = "b5e2d8f1a7c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds `tables.description_project` and `tables.description_edited`, and creates
    `column_descriptions`, one row per (table, column) with the project's text and an edit. Every
    existing table starts with neither; the next deploy writes the project's text.
    """
    with op.batch_alter_table("tables") as batch_op:
        batch_op.add_column(sa.Column("description_project", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("description_edited", sa.Text(), nullable=True))
    op.create_table(
        "column_descriptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("column", sa.String(length=1024), nullable=False),
        sa.Column("description_project", sa.Text(), nullable=True),
        sa.Column("description_edited", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["table_id"],
            ["tables.id"],
            name=op.f("fk_column_descriptions_table_id_tables"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["principals.id"],
            name=op.f("fk_column_descriptions_updated_by_principals"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_column_descriptions")),
        sa.UniqueConstraint(
            "table_id", "column", name=op.f("uq_column_descriptions_table_id_column")
        ),
    )


def downgrade() -> None:
    """Downgrade schema.

    Drops the descriptions, edits included: the previous release has no place for them.
    """
    op.drop_table("column_descriptions")
    with op.batch_alter_table("tables") as batch_op:
        batch_op.drop_column("description_edited")
        batch_op.drop_column("description_project")
