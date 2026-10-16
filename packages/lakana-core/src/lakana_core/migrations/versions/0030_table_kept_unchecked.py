"""table kept unchecked: the note says the checks could not run on the kept data.

Revision ID: b5e8c1f4a7d0
Revises: a4d7b0e3f6c9
Create Date: 2026-10-15 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b5e8c1f4a7d0"
down_revision: str | Sequence[str] | None = "a4d7b0e3f6c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds `tables.kept_unchecked`: whether the note names checks that could not run (a rebuilt
    table whose `keep` check read another project's kept table), not a check that failed. A
    note written before this reads as a failed check.
    """
    with op.batch_alter_table("tables") as batch_op:
        batch_op.add_column(
            sa.Column("kept_unchecked", sa.Boolean(), server_default=sa.false(), nullable=False)
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("tables") as batch_op:
        batch_op.drop_column("kept_unchecked")
