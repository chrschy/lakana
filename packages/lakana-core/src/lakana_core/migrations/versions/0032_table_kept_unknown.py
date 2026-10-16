"""table kept unknown: a mark for a table set back to data whose checks are not known.

Revision ID: d7a0e3b6c9f2
Revises: c6f9d2a5b8e1
Create Date: 2026-10-16 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d7a0e3b6c9f2"
down_revision: str | Sequence[str] | None = "c6f9d2a5b8e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds `tables.kept_unknown`, whether a table's mark says it was set back to data whose checks
    are not known. Every existing mark names a failed check or a check that could not run, so
    every existing row starts as false.
    """
    with op.batch_alter_table("tables") as batch_op:
        batch_op.add_column(
            sa.Column("kept_unknown", sa.Boolean(), server_default=sa.false(), nullable=False)
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("tables") as batch_op:
        batch_op.drop_column("kept_unknown")
