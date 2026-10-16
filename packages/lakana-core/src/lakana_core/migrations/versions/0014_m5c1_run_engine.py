"""m5c1 run engine: runs, task runs, attempts, the scheduler lease, run context columns.

Revision ID: e7f2a9c4b1d3
Revises: d1e5f3a7b9c2
Create Date: 2026-10-03 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7f2a9c4b1d3"
down_revision: str | Sequence[str] | None = "d1e5f3a7b9c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUN_KIND = sa.Enum("FULL", "SELECTION", "RESUME", name="runkind", native_enum=False, length=9)
_RUN_STATUS = sa.Enum(
    "QUEUED",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    name="runstatus",
    native_enum=False,
    length=9,
)
_TASK_STATUS = sa.Enum(
    "PENDING",
    "READY",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "UPSTREAM_FAILED",
    "CANCELLED",
    name="taskstatus",
    native_enum=False,
    length=15,
)
_OUTCOME = sa.Enum(
    "SUCCEEDED",
    "FAILED",
    "TIMED_OUT",
    "LOST",
    "RELEASED",
    "CANCELLED",
    name="attemptoutcome",
    native_enum=False,
    length=9,
)
_ACTIVE = sa.text("status IN ('QUEUED', 'RUNNING')")


def upgrade() -> None:
    """Upgrade schema.

    Adds `runs`, `task_runs`, `task_attempts` and `scheduler_lease` (with its
    one row), `environment_deployments.run_as_principal_id`,
    `query_runs.task_run_id` and `query_runs.break_glass_reason`.
    `grants.action` (VARCHAR(18)) already fits ``RUN``.
    """
    op.create_table(
        "runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("job_version_id", sa.Uuid(), nullable=False),
        sa.Column("triggered_by", sa.Uuid(), nullable=True),
        sa.Column("run_as_principal_id", sa.Uuid(), nullable=True),
        sa.Column("kind", _RUN_KIND, nullable=False),
        sa.Column("resumed_from_run_id", sa.Uuid(), nullable=True),
        sa.Column("selection_json", sa.JSON(), nullable=False),
        sa.Column("status", _RUN_STATUS, nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_runs_environment_id_environments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name=op.f("fk_runs_repository_id_repositories"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_version_id"],
            ["job_versions.id"],
            name=op.f("fk_runs_job_version_id_job_versions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["triggered_by"],
            ["principals.id"],
            name=op.f("fk_runs_triggered_by_principals"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_as_principal_id"],
            ["principals.id"],
            name=op.f("fk_runs_run_as_principal_id_principals"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["resumed_from_run_id"],
            ["runs.id"],
            name=op.f("fk_runs_resumed_from_run_id_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
    )
    with op.batch_alter_table("runs") as batch_op:
        batch_op.create_index(op.f("ix_runs_environment_id"), ["environment_id"])
        batch_op.create_index(op.f("ix_runs_status"), ["status"])
        batch_op.create_index(
            "ix_runs_one_active",
            ["environment_id", "repository_id"],
            unique=True,
            sqlite_where=_ACTIVE,
            postgresql_where=_ACTIVE,
        )
    op.create_table(
        "task_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("asset", sa.String(length=1024), nullable=False),
        sa.Column("status", _TASK_STATUS, nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("retry_delay_seconds", sa.Integer(), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("query_run_id", sa.Uuid(), nullable=True),
        sa.Column("upstream_json", sa.JSON(), nullable=False),
        sa.Column("conflict_retried", sa.Boolean(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_task_runs_run_id_runs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["query_run_id"],
            ["query_runs.id"],
            name=op.f("fk_task_runs_query_run_id_query_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_runs")),
        sa.UniqueConstraint("run_id", "asset", name=op.f("uq_task_runs_run_id_asset")),
    )
    with op.batch_alter_table("task_runs") as batch_op:
        batch_op.create_index(op.f("ix_task_runs_status"), ["status"])
    op.create_table(
        "task_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_run_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("worker", sa.String(length=255), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", _OUTCOME, nullable=True),
        sa.Column("rows_written", sa.BigInteger(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["task_run_id"],
            ["task_runs.id"],
            name=op.f("fk_task_attempts_task_run_id_task_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_attempts")),
    )
    with op.batch_alter_table("task_attempts") as batch_op:
        batch_op.create_index(op.f("ix_task_attempts_task_run_id"), ["task_run_id"])
    lease = op.create_table(
        "scheduler_lease",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("holder", sa.String(length=255), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scheduler_lease")),
    )
    op.bulk_insert(lease, [{"id": 1, "holder": None, "heartbeat_at": None}])
    with op.batch_alter_table("environment_deployments") as batch_op:
        batch_op.add_column(sa.Column("run_as_principal_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            op.f("fk_environment_deployments_run_as_principal_id_principals"),
            "principals",
            ["run_as_principal_id"],
            ["id"],
            ondelete="SET NULL",
        )
    with op.batch_alter_table("query_runs") as batch_op:
        batch_op.add_column(sa.Column("task_run_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("break_glass_reason", sa.Text(), nullable=True))
        batch_op.create_foreign_key(
            op.f("fk_query_runs_task_run_id_task_runs"),
            "task_runs",
            ["task_run_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(op.f("ix_query_runs_task_run_id"), ["task_run_id"])


def downgrade() -> None:
    """Downgrade schema.

    Deletes every `RUN` grant first, revoked or not, for the reason `0012`'s
    downgrade gives for `MAINTAIN_TABLE`: the previous release's `Action`
    enum has no such member. The M5c1 audit rows are left behind (audit rows
    are never deleted); every run, task run, attempt and the lease row are
    dropped with their tables, and the run principal bindings with their
    column.
    """
    op.execute(sa.text("DELETE FROM grants WHERE action = 'RUN'"))
    with op.batch_alter_table("query_runs") as batch_op:
        batch_op.drop_index(op.f("ix_query_runs_task_run_id"))
        batch_op.drop_constraint(op.f("fk_query_runs_task_run_id_task_runs"), type_="foreignkey")
        batch_op.drop_column("break_glass_reason")
        batch_op.drop_column("task_run_id")
    with op.batch_alter_table("environment_deployments") as batch_op:
        batch_op.drop_constraint(
            op.f("fk_environment_deployments_run_as_principal_id_principals"),
            type_="foreignkey",
        )
        batch_op.drop_column("run_as_principal_id")
    op.drop_table("scheduler_lease")
    with op.batch_alter_table("task_attempts") as batch_op:
        batch_op.drop_index(op.f("ix_task_attempts_task_run_id"))
    op.drop_table("task_attempts")
    with op.batch_alter_table("task_runs") as batch_op:
        batch_op.drop_index(op.f("ix_task_runs_status"))
    op.drop_table("task_runs")
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_index("ix_runs_one_active")
        batch_op.drop_index(op.f("ix_runs_status"))
        batch_op.drop_index(op.f("ix_runs_environment_id"))
    op.drop_table("runs")
