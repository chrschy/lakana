"""table external config.

Revision ID: 157e375851cd
Revises: 7c957aaa4637
Create Date: 2026-09-14 20:59:55.654062

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "157e375851cd"
down_revision: str | Sequence[str] | None = "7c957aaa4637"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("tables", sa.Column("external_config", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("tables", "external_config")
