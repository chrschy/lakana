"""query_runs.error_cause: a machine-readable cause of a failed query run.

Revision ID: c8e1a5d7f3b9
Revises: a6c3e9d1f7b2
Create Date: 2026-10-05 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8e1a5d7f3b9"
down_revision: str | Sequence[str] | None = "a6c3e9d1f7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds a nullable column; existing rows keep `NULL` (cause unknown).
    """
    with op.batch_alter_table("query_runs") as batch_op:
        batch_op.add_column(sa.Column("error_cause", sa.String(length=32), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("query_runs") as batch_op:
        batch_op.drop_column("error_cause")
