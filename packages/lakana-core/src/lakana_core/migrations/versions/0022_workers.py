"""workers, the worker id on query and deploy runs, and the query status index.

Revision ID: f6c1a8d3b5e2
Revises: e4b7d2a9c6f1
Create Date: 2026-10-09 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6c1a8d3b5e2"
down_revision: str | Sequence[str] | None = "e4b7d2a9c6f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds the `workers` table, the nullable `worker_id` on `query_runs` and `deploy_runs`
    (`NULL` for every existing row, so no default is needed) and the index the wait-reason
    and sweep queries read `query_runs` by, and the index the list of ended queries reads
    them by (an environment's runs by finish time).
    """
    op.create_table(
        "workers",
        sa.Column("id", sa.String(length=255), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workers")),
    )
    with op.batch_alter_table("query_runs") as batch_op:
        batch_op.add_column(sa.Column("worker_id", sa.String(length=255), nullable=True))
    with op.batch_alter_table("deploy_runs") as batch_op:
        batch_op.add_column(sa.Column("worker_id", sa.String(length=255), nullable=True))
    op.create_index("ix_query_runs_status_created_at", "query_runs", ["status", "created_at"])
    op.create_index(
        "ix_query_runs_environment_id_finished_at",
        "query_runs",
        ["environment_id", "finished_at"],
    )


def downgrade() -> None:
    """Downgrade schema.

    Converts nothing: the cause `worker_lost` is a string an earlier release reads as an
    unknown cause.
    """
    op.drop_index("ix_query_runs_environment_id_finished_at", table_name="query_runs")
    op.drop_index("ix_query_runs_status_created_at", table_name="query_runs")
    with op.batch_alter_table("deploy_runs") as batch_op:
        batch_op.drop_column("worker_id")
    with op.batch_alter_table("query_runs") as batch_op:
        batch_op.drop_column("worker_id")
    op.drop_table("workers")
