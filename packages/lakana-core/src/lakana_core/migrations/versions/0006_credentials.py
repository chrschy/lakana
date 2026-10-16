"""credentials.

Revision ID: b5c8e1f30a76
Revises: 8e2d7f4a1c93
Create Date: 2026-09-17 09:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b5c8e1f30a76"
down_revision: str | Sequence[str] | None = "8e2d7f4a1c93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "personal_access_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("key_id", sa.String(length=32), nullable=False),
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name=op.f("fk_personal_access_tokens_principal_id_principals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_personal_access_tokens")),
    )
    op.create_index(
        op.f("ix_personal_access_tokens_key_id"),
        "personal_access_tokens",
        ["key_id"],
        unique=True,
    )
    op.create_table(
        "task_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("environment", sa.String(length=255), nullable=False),
        sa.Column("asset_scope", sa.JSON(), nullable=False),
        sa.Column("key_id", sa.String(length=32), nullable=False),
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name=op.f("fk_task_tokens_principal_id_principals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_tokens")),
    )
    op.create_index(op.f("ix_task_tokens_key_id"), "task_tokens", ["key_id"], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("task_tokens")
    op.drop_table("personal_access_tokens")
