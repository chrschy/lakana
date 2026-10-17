"""policy schema id: the table schema a row filter or column mask was written against.

Revision ID: e8b1f4c7d0a3
Revises: d7a0e3b6c9f2
Create Date: 2026-10-17 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e8b1f4c7d0a3"
down_revision: str | Sequence[str] | None = "d7a0e3b6c9f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds `row_filters.schema_id` and `column_masks.schema_id`, the id of the table's current
    schema when the policy was created. Every existing policy starts with none: a read judges
    it from the table's oldest schema, so a rename that happened before this upgrade is found.
    """
    with op.batch_alter_table("row_filters") as batch_op:
        batch_op.add_column(sa.Column("schema_id", sa.Integer(), nullable=True))
    with op.batch_alter_table("column_masks") as batch_op:
        batch_op.add_column(sa.Column("schema_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("column_masks") as batch_op:
        batch_op.drop_column("schema_id")
    with op.batch_alter_table("row_filters") as batch_op:
        batch_op.drop_column("schema_id")
