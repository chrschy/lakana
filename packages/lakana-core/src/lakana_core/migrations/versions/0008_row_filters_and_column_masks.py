"""row_filters_and_column_masks.

Revision ID: c7f3b91a5e02
Revises: a4c9e57f18d2
Create Date: 2026-09-20 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7f3b91a5e02"
down_revision: str | Sequence[str] | None = "a4c9e57f18d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "row_filters",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=True),
        sa.Column("group_id", sa.Uuid(), nullable=True),
        sa.Column("environment", sa.String(length=255), nullable=False),
        sa.Column("namespace", sa.String(length=1024), nullable=False),
        sa.Column("table", sa.String(length=255), nullable=False),
        sa.Column("expression", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_by", sa.Uuid(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(principal_id IS NULL) != (group_id IS NULL)",
            name=op.f("ck_row_filters_exactly_one_subject"),
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name=op.f("fk_row_filters_principal_id_principals"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["groups.id"],
            name=op.f("fk_row_filters_group_id_groups"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["principals.id"],
            name=op.f("fk_row_filters_created_by_principals"),
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by"],
            ["principals.id"],
            name=op.f("fk_row_filters_revoked_by_principals"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_row_filters")),
    )
    op.create_index(op.f("ix_row_filters_principal_id"), "row_filters", ["principal_id"])
    op.create_index(op.f("ix_row_filters_group_id"), "row_filters", ["group_id"])
    op.create_table(
        "column_masks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=True),
        sa.Column("group_id", sa.Uuid(), nullable=True),
        sa.Column("environment", sa.String(length=255), nullable=False),
        sa.Column("namespace", sa.String(length=1024), nullable=False),
        sa.Column("table", sa.String(length=255), nullable=False),
        sa.Column("column", sa.String(length=255), nullable=False),
        sa.Column("expression", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_by", sa.Uuid(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(principal_id IS NULL) != (group_id IS NULL)",
            name=op.f("ck_column_masks_exactly_one_subject"),
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name=op.f("fk_column_masks_principal_id_principals"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["groups.id"],
            name=op.f("fk_column_masks_group_id_groups"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["principals.id"],
            name=op.f("fk_column_masks_created_by_principals"),
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by"],
            ["principals.id"],
            name=op.f("fk_column_masks_revoked_by_principals"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_column_masks")),
    )
    op.create_index(op.f("ix_column_masks_principal_id"), "column_masks", ["principal_id"])
    op.create_index(op.f("ix_column_masks_group_id"), "column_masks", ["group_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("column_masks")
    op.drop_table("row_filters")
