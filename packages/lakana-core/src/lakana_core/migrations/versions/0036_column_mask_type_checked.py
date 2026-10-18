"""column mask type_checked: whether the mask was checked against its column's type.

Revision ID: b5e2d8f1a7c3
Revises: a3d7c9e1f502
Create Date: 2026-10-18 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b5e2d8f1a7c3"
down_revision: str | Sequence[str] | None = "a3d7c9e1f502"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds `column_masks.type_checked`. Every existing mask starts `false`: it was never checked
    against its column's type, and a read keeps giving the value it always gave. A mask created
    from this release on is `true`, and a read casts it to the column's type.
    """
    with op.batch_alter_table("column_masks") as batch_op:
        batch_op.add_column(
            sa.Column("type_checked", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("column_masks") as batch_op:
        batch_op.drop_column("type_checked")
