"""audit_events.

Revision ID: a3f7c2e9b4d1
Revises: e1a2f6b9c3d4
Create Date: 2026-09-21 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3f7c2e9b4d1"
down_revision: str | Sequence[str] | None = "e1a2f6b9c3d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=True),
        sa.Column(
            "event_type",
            sa.Enum(
                "AUTHENTICATION_SUCCEEDED",
                "AUTHENTICATION_FAILED",
                "AUTHORIZATION_DENIED",
                "GRANT_CREATED",
                "GRANT_REVOKED",
                "SECRET_CREATED",
                "SECRET_ROTATED",
                "SECRET_REVOKED",
                "QUERY_COMPLETED",
                name="auditeventtype",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "securable_kind",
            sa.Enum(
                "INSTANCE",
                "ENVIRONMENT",
                "NAMESPACE",
                "TABLE",
                name="securablekind",
                native_enum=False,
            ),
            nullable=True,
        ),
        sa.Column("securable_environment", sa.String(length=255), nullable=True),
        sa.Column("securable_namespace", sa.String(length=1024), nullable=True),
        sa.Column("securable_table", sa.String(length=255), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name=op.f("fk_audit_events_principal_id_principals"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index(op.f("ix_audit_events_occurred_at"), "audit_events", ["occurred_at"])
    op.create_index(op.f("ix_audit_events_principal_id"), "audit_events", ["principal_id"])
    op.create_index(op.f("ix_audit_events_event_type"), "audit_events", ["event_type"])
    op.create_index(
        op.f("ix_audit_events_securable_environment"), "audit_events", ["securable_environment"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("audit_events")
