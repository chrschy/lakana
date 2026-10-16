"""groups.

Revision ID: 8e2d7f4a1c93
Revises: 3f1a9c6d2b47
Create Date: 2026-09-17 09:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8e2d7f4a1c93"
down_revision: str | Sequence[str] | None = "3f1a9c6d2b47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "groups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_groups")),
        sa.UniqueConstraint("name", name=op.f("uq_groups_name")),
    )
    op.create_table(
        "group_memberships",
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["groups.id"],
            name=op.f("fk_group_memberships_group_id_groups"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name=op.f("fk_group_memberships_principal_id_principals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("group_id", "principal_id", name=op.f("pk_group_memberships")),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("group_memberships")
    op.drop_table("groups")
