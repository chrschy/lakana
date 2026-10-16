"""notification deliveries: the queue and the record of a job's webhook notifications.

Revision ID: a4d7b0e3f6c9
Revises: f3c6a9d2e5b8
Create Date: 2026-10-14 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4d7b0e3f6c9"
down_revision: str | Sequence[str] | None = "f3c6a9d2e5b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATES = ("PENDING", "DELIVERED", "FAILED")


def upgrade() -> None:
    """Upgrade schema.

    Creates `notification_deliveries`, one row per notification of a run (or a test), unique on
    (`run_id`, `event`) so a run queues an event once, and indexed on (`state`,
    `next_attempt_at`) for the loop that finds what is due. `run_id` is nullable: a test
    notification has no run.
    """
    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("job", sa.String(length=255), nullable=False),
        sa.Column("event", sa.String(length=16), nullable=False),
        sa.Column("format", sa.String(length=8), nullable=False),
        sa.Column("secret_name", sa.String(length=255), nullable=False),
        sa.Column(
            "state",
            sa.Enum(*_STATES, name="notificationstate", native_enum=False, length=9),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_result", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_notification_deliveries_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_notification_deliveries_environment_id_environments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name=op.f("fk_notification_deliveries_repository_id_repositories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_deliveries")),
        sa.UniqueConstraint(
            "run_id", "event", name=op.f("uq_notification_deliveries_run_id_event")
        ),
    )
    op.create_index(
        "ix_notification_deliveries_state_next_attempt_at",
        "notification_deliveries",
        ["state", "next_attempt_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_notification_deliveries_state_next_attempt_at", table_name="notification_deliveries"
    )
    op.drop_table("notification_deliveries")
