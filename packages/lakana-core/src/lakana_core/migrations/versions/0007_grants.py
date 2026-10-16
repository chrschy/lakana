"""grants.

Revision ID: a4c9e57f18d2
Revises: b5c8e1f30a76
Create Date: 2026-09-19 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4c9e57f18d2"
down_revision: str | Sequence[str] | None = "b5c8e1f30a76"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=True),
        sa.Column("group_id", sa.Uuid(), nullable=True),
        sa.Column(
            "role",
            sa.Enum(
                "VIEWER",
                "ANALYST",
                "DATA_SCIENTIST",
                "DATA_ENGINEER",
                "ADMIN",
                name="role",
                native_enum=False,
            ),
            nullable=True,
        ),
        sa.Column(
            "action",
            sa.Enum(
                "MANAGE_ENVIRONMENT",
                "READ_ENVIRONMENT",
                "CREATE_NAMESPACE",
                "READ_NAMESPACE",
                "DROP_NAMESPACE",
                "MANAGE_NAMESPACE",
                "CREATE_TABLE",
                "READ_TABLE",
                "WRITE_TABLE",
                "DROP_TABLE",
                "SUBMIT_QUERY",
                "MANAGE_PRINCIPAL",
                "MANAGE_GROUP",
                "MANAGE_TOKEN",
                "MINT_TASK_TOKEN",
                "MANAGE_GRANT",
                name="action",
                native_enum=False,
            ),
            nullable=True,
        ),
        sa.Column(
            "kind",
            sa.Enum(
                "INSTANCE",
                "ENVIRONMENT",
                "NAMESPACE",
                "TABLE",
                name="securablekind",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("environment", sa.String(length=255), nullable=True),
        sa.Column("namespace", sa.String(length=1024), nullable=True),
        sa.Column("table", sa.String(length=255), nullable=True),
        sa.Column("granted_by", sa.Uuid(), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_by", sa.Uuid(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(principal_id IS NULL) != (group_id IS NULL)",
            name=op.f("ck_grants_exactly_one_subject"),
        ),
        sa.CheckConstraint(
            "(role IS NULL) != (action IS NULL)",
            name=op.f("ck_grants_exactly_one_grant"),
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name=op.f("fk_grants_principal_id_principals"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["groups.id"],
            name=op.f("fk_grants_group_id_groups"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["granted_by"],
            ["principals.id"],
            name=op.f("fk_grants_granted_by_principals"),
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by"],
            ["principals.id"],
            name=op.f("fk_grants_revoked_by_principals"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_grants")),
    )
    op.create_index(op.f("ix_grants_principal_id"), "grants", ["principal_id"])
    op.create_index(op.f("ix_grants_group_id"), "grants", ["group_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("grants")
