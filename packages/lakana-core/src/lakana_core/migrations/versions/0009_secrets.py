"""secrets.

Revision ID: e1a2f6b9c3d4
Revises: c7f3b91a5e02
Create Date: 2026-09-20 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1a2f6b9c3d4"
down_revision: str | Sequence[str] | None = "c7f3b91a5e02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "environments",
        sa.Column("storage_secret_ref", sa.String(length=255), nullable=True),
    )
    op.create_table(
        "secrets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("wrapped_data_key", sa.LargeBinary(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_by", sa.Uuid(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("environment_id", "name", name=op.f("uq_secrets_environment_name")),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_secrets_environment_id_environments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["principals.id"],
            name=op.f("fk_secrets_created_by_principals"),
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by"],
            ["principals.id"],
            name=op.f("fk_secrets_revoked_by_principals"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_secrets")),
    )
    op.create_index(op.f("ix_secrets_environment_id"), "secrets", ["environment_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("secrets")
    op.drop_column("environments", "storage_secret_ref")
