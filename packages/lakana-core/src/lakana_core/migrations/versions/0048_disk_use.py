"""disk_use: the measured sizes of tables, code archives and the caches of workers.

Revision ID: f1a2b3c4d5e6
Revises: e0d9e6185736
Create Date: 2026-10-21 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1a2b3c4d5e6"
down_revision: str | Sequence[str] | None = "e0d9e6185736"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds `tables.size_bytes` and `tables.size_metadata_location` (the size of a managed table's
    current files, and the metadata file it was read from), `job_versions.code_bytes` (the size
    of a project version's code archive) and five columns of `workers` (the size of each cache a
    worker measured, whether the measurement finished, and when). Every column is empty on every
    existing row: the worker's housekeeping measures the tables from the first sweep on, a
    deploy records its archive's size, and a worker its caches.
    """
    with op.batch_alter_table("tables") as batch:
        batch.add_column(sa.Column("size_bytes", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("size_metadata_location", sa.String(1024), nullable=True))
    with op.batch_alter_table("job_versions") as batch:
        batch.add_column(sa.Column("code_bytes", sa.BigInteger(), nullable=True))
    with op.batch_alter_table("workers") as batch:
        batch.add_column(sa.Column("cache_environments_bytes", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("cache_packages_bytes", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("cache_tasks_bytes", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("cache_complete", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("cache_measured_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    Drops the columns; the figures are measurements, measured again after a new upgrade.
    """
    with op.batch_alter_table("workers") as batch:
        batch.drop_column("cache_measured_at")
        batch.drop_column("cache_complete")
        batch.drop_column("cache_tasks_bytes")
        batch.drop_column("cache_packages_bytes")
        batch.drop_column("cache_environments_bytes")
    with op.batch_alter_table("job_versions") as batch:
        batch.drop_column("code_bytes")
    with op.batch_alter_table("tables") as batch:
        batch.drop_column("size_metadata_location")
        batch.drop_column("size_bytes")
