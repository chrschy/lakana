"""principal_passed_by: until when workers pass a principal's ad-hoc queries by.

Revision ID: a0d7e3b6f2c8
Revises: f9c6d2a5e1b7
Create Date: 2026-10-19 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a0d7e3b6f2c8"
down_revision: str | Sequence[str] | None = "f9c6d2a5e1b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds the nullable `query_passed_by_until` to `principals`. Rows stay null: no principal is
    passed by.
    """
    op.add_column(
        "principals",
        sa.Column("query_passed_by_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema.

    Drops the time: the previous release keeps this in its workers' memory only.
    """
    with op.batch_alter_table("principals") as batch:
        batch.drop_column("query_passed_by_until")
