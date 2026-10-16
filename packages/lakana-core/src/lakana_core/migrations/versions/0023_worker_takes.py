"""workers.takes: the kind of work a worker takes.

Revision ID: a7d3c9e1f4b6
Revises: f6c1a8d3b5e2
Create Date: 2026-10-10 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7d3c9e1f4b6"
down_revision: str | Sequence[str] | None = "f6c1a8d3b5e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds `workers.takes` (`all`, `interactive` or `pipeline`), not null. The server default
    `all` is kept: a row inserted by code that does not set the column means "takes
    everything".
    """
    with op.batch_alter_table("workers") as batch_op:
        batch_op.add_column(
            sa.Column("takes", sa.String(length=16), nullable=False, server_default="all")
        )


def downgrade() -> None:
    """Downgrade schema.

    Drops the column; the rows stay.
    """
    with op.batch_alter_table("workers") as batch_op:
        batch_op.drop_column("takes")
