"""principal is_active.

Revision ID: 3f1a9c6d2b47
Revises: 157e375851cd
Create Date: 2026-09-17 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3f1a9c6d2b47"
down_revision: str | Sequence[str] | None = "157e375851cd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "principals",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("principals", "is_active")
