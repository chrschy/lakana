"""checks: what the data quality checks of M5d-a need.

Revision ID: b8e2c5f7a1d4
Revises: a7d3c9e1f4b6
Create Date: 2026-10-11 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8e2c5f7a1d4"
down_revision: str | Sequence[str] | None = "a7d3c9e1f4b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OUTCOMES = ("PASSED", "FAILED", "WARNED", "ERRORED")
_ROLLBACKS = (
    "ROLLED_BACK",
    "UNPUBLISHED",
    "NOTHING_WRITTEN",
    "NOT_A_TABLE",
    "CHANGED_SINCE",
    "FAILED",
)


def upgrade() -> None:
    """Upgrade schema.

    Adds `deploy_runs.checks_json`, nullable: the checks a deploy or dry run found, per table.
    A run written before it has none. Adds `task_runs.kind` (`asset` or `source_check`), not
    null with the server default `asset`, so a task run written before it reads as a table to
    build. Adds the record of what a write did: `tables.unpublished_run_id` (the run whose
    first build did not finish, or failed a check; no foreign key, so the marker outlives the run),
    `query_runs.snapshot_before`, and on `task_runs` `has_checks` (not null, server default
    false: a task run written before it has no checks) and the *written* stamp
    (`written_table_id`, `written_snapshot_before`, `written_snapshot_after`,
    `written_publishes`, all nullable and without foreign keys) and the baseline of a Python
    table task with checks (`baseline_snapshot_id`, `baseline_publishes`, both nullable:
    `baseline_publishes IS NULL` means that none is recorded) with the tip of that task run's
    own chain of writes (`own_snapshot_id`, nullable: null is "wrote nothing of its own", 0 is
    "wrote, and the commit made no snapshot"). Creates `check_results`: one
    row per check of a task run, a count and never a row of a table. `table_id` has no foreign
    key, so a dropped table keeps its results.
    """
    with op.batch_alter_table("deploy_runs") as batch_op:
        batch_op.add_column(sa.Column("checks_json", sa.JSON(), nullable=True))
    with op.batch_alter_table("task_runs") as batch_op:
        batch_op.add_column(
            sa.Column("kind", sa.String(length=16), nullable=False, server_default="asset")
        )
    with op.batch_alter_table("tables") as batch_op:
        batch_op.add_column(sa.Column("unpublished_run_id", sa.Uuid(), nullable=True))
    with op.batch_alter_table("query_runs") as batch_op:
        batch_op.add_column(sa.Column("snapshot_before", sa.BigInteger(), nullable=True))
    with op.batch_alter_table("task_runs") as batch_op:
        batch_op.add_column(
            sa.Column("has_checks", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
        batch_op.add_column(sa.Column("written_table_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("written_snapshot_before", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("written_snapshot_after", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("written_publishes", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("baseline_snapshot_id", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("baseline_publishes", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("own_snapshot_id", sa.BigInteger(), nullable=True))
    op.create_table(
        "check_results",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("task_run_id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("check_name", sa.String(length=1024), nullable=False),
        sa.Column("check_path", sa.String(length=1024), nullable=False),
        sa.Column("table_name", sa.String(length=1024), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=True),
        sa.Column("on_failure", sa.String(length=8), nullable=False),
        sa.Column(
            "outcome",
            sa.Enum(*_OUTCOMES, name="checkoutcome", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("failing_rows", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("checked_snapshot_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "rollback_outcome",
            sa.Enum(*_ROLLBACKS, name="rollbackoutcome", native_enum=False, length=16),
            nullable=True,
        ),
        sa.Column("policy_applied", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("query_run_id", sa.Uuid(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_check_results_run_id_runs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["task_run_id"],
            ["task_runs.id"],
            name=op.f("fk_check_results_task_run_id_task_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_check_results_environment_id_environments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["query_run_id"],
            ["query_runs.id"],
            name=op.f("fk_check_results_query_run_id_query_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_check_results")),
        sa.UniqueConstraint(
            "task_run_id", "check_name", name=op.f("uq_check_results_task_run_id_check_name")
        ),
    )
    with op.batch_alter_table("check_results") as batch_op:
        batch_op.create_index(op.f("ix_check_results_run_id"), ["run_id"])
        batch_op.create_index(op.f("ix_check_results_table_id"), ["table_id"])
        batch_op.create_index(
            "ix_check_results_environment_id_finished_at",
            ["environment_id", "finished_at", "id"],
        )


def downgrade() -> None:
    """Downgrade schema.

    Drops the table and the columns; the other rows stay.
    """
    op.drop_table("check_results")
    with op.batch_alter_table("task_runs") as batch_op:
        batch_op.drop_column("own_snapshot_id")
        batch_op.drop_column("baseline_publishes")
        batch_op.drop_column("baseline_snapshot_id")
        batch_op.drop_column("written_publishes")
        batch_op.drop_column("written_snapshot_after")
        batch_op.drop_column("written_snapshot_before")
        batch_op.drop_column("written_table_id")
        batch_op.drop_column("has_checks")
        batch_op.drop_column("kind")
    with op.batch_alter_table("query_runs") as batch_op:
        batch_op.drop_column("snapshot_before")
    with op.batch_alter_table("tables") as batch_op:
        batch_op.drop_column("unpublished_run_id")
    with op.batch_alter_table("deploy_runs") as batch_op:
        batch_op.drop_column("checks_json")
