"""query_runs.cancel_requested, and the query status CANCELLED.

Revision ID: e4b7d2a9c6f1
Revises: c3f8a6d1e9b4
Create Date: 2026-10-08 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4b7d2a9c6f1"
down_revision: str | Sequence[str] | None = "c3f8a6d1e9b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds `cancel_requested`, false for every existing row. The server default stays, so a
    server or worker of the previous release that inserts a row without the column keeps
    working while an upgrade is under way. The status `CANCELLED` needs no change: `status`
    is a VARCHAR(9) without a check constraint.
    """
    with op.batch_alter_table("query_runs") as batch_op:
        batch_op.add_column(
            sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    """Downgrade schema.

    The release before this one cannot load the status `CANCELLED`: such a run becomes
    `FAILED` with the cause `cancelled`, which is how that release records a cancellation.
    """
    op.execute(
        sa.text(
            "UPDATE query_runs SET status = 'FAILED', "
            "error_cause = COALESCE(error_cause, 'cancelled'), "
            "error_message = COALESCE(error_message, 'The query was cancelled.') "
            "WHERE status = 'CANCELLED'"
        )
    )
    with op.batch_alter_table("query_runs") as batch_op:
        batch_op.drop_column("cancel_requested")
