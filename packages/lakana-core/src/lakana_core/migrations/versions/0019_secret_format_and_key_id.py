"""secrets.format_version and secrets.key_id: how a secret is stored and under which key.

Revision ID: b5d9f2a8c4e7
Revises: e2a7c4f1b6d8
Create Date: 2026-10-06 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b5d9f2a8c4e7"
down_revision: str | Sequence[str] | None = "e2a7c4f1b6d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Every existing row becomes format 1 with the bare key id ``env`` (the
    provider, without a key fingerprint: the migration has no master key and
    needs none). Nothing is re-encrypted; a secret becomes format 2 when it
    is next set. The temporary server defaults are dropped, so the
    application must always say both values.
    """
    with op.batch_alter_table("secrets") as batch_op:
        batch_op.add_column(
            sa.Column("format_version", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.add_column(
            sa.Column("key_id", sa.String(length=64), nullable=False, server_default="env")
        )
    with op.batch_alter_table("secrets") as batch_op:
        batch_op.alter_column("format_version", server_default=None)
        batch_op.alter_column("key_id", server_default=None)


def downgrade() -> None:
    """Downgrade schema.

    Deletes every secret of a newer format first. The previous release
    decrypts every row without associated data, so it cannot read a format-2
    row: it would fail with a decryption error for each, and an environment
    that references one could not resolve its storage. Format-1 rows are left
    untouched and keep working.
    """
    op.execute("DELETE FROM secrets WHERE format_version > 1")
    with op.batch_alter_table("secrets") as batch_op:
        batch_op.drop_column("key_id")
        batch_op.drop_column("format_version")
