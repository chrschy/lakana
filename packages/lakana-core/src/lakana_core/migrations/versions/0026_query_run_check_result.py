"""query run check result: the binding of a query run to the check result whose rows it shows.

Revision ID: d1a4e7b9c3f6
Revises: c9f3d6a8b2e5
Create Date: 2026-10-12 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1a4e7b9c3f6"
down_revision: str | Sequence[str] | None = "c9f3d6a8b2e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds `query_runs.check_result_id`, nullable and without a foreign key. A query run that
    carries it reads the checked table at the snapshot the result names. A foreign key that
    emptied the column when the result goes (with its run) would turn such a query into one
    of the table as it is now; without one the id stays and the worker fails the query with
    a message. A query run written before this reads as bound to no check result.
    """
    with op.batch_alter_table("query_runs") as batch_op:
        batch_op.add_column(sa.Column("check_result_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("query_runs") as batch_op:
        batch_op.drop_column("check_result_id")
