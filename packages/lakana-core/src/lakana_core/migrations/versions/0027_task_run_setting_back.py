"""task run setting back: a lost task is put back to set its table back.

Revision ID: e2b5f8c1d4a7
Revises: d1a4e7b9c3f6
Create Date: 2026-10-13 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2b5f8c1d4a7"
down_revision: str | Sequence[str] | None = "d1a4e7b9c3f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds `task_runs.setting_back` and `task_runs.set_back_claims`. A task run written before
    this reads as not setting its table back, with no set-back claim taken.
    """
    with op.batch_alter_table("task_runs") as batch_op:
        batch_op.add_column(
            sa.Column("setting_back", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
        batch_op.add_column(
            sa.Column("set_back_claims", sa.Integer(), server_default="0", nullable=False)
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("task_runs") as batch_op:
        batch_op.drop_column("set_back_claims")
        batch_op.drop_column("setting_back")
