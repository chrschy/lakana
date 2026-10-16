"""package_indexes, and grants.action wide enough for MANAGE_PACKAGE_INDEX.

Revision ID: c3f8a6d1e9b4
Revises: b5d9f2a8c4e7
Create Date: 2026-10-07 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3f8a6d1e9b4"
down_revision: str | Sequence[str] | None = "b5d9f2a8c4e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds `package_indexes`. Widens `grants.action` from VARCHAR(18) to
    VARCHAR(20): ``MANAGE_PACKAGE_INDEX`` is the first action longer than
    18 characters, and Postgres enforces the length. `audit_events.event_type`
    (VARCHAR(64)) already fits the three new event types.
    """
    op.create_table(
        "package_indexes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("url", sa.String(length=1024), nullable=False),
        sa.Column("credential_secret", sa.String(length=255), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["principals.id"],
            name=op.f("fk_package_indexes_created_by_principals"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_package_indexes")),
        sa.UniqueConstraint("name", name=op.f("uq_package_indexes_name")),
        sa.UniqueConstraint("url", name=op.f("uq_package_indexes_url")),
    )
    with op.batch_alter_table("grants") as batch_op:
        batch_op.alter_column(
            "action",
            existing_type=sa.String(length=18),
            type_=sa.String(length=20),
            existing_nullable=True,
        )


def downgrade() -> None:
    """Downgrade schema.

    Deletes every `MANAGE_PACKAGE_INDEX` grant first, revoked or not, for
    the reason `0012`'s downgrade gives for `MAINTAIN_TABLE`: the previous
    release's `Action` enum has no such member, so a leftover row raises
    `LookupError` on every authorization of its subject. Every registered
    index is dropped with its table; the audit rows stay.
    """
    op.execute(sa.text("DELETE FROM grants WHERE action = 'MANAGE_PACKAGE_INDEX'"))
    with op.batch_alter_table("grants") as batch_op:
        batch_op.alter_column(
            "action",
            existing_type=sa.String(length=20),
            type_=sa.String(length=18),
            existing_nullable=True,
        )
    op.drop_table("package_indexes")
