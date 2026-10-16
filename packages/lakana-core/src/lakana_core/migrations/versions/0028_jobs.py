"""jobs: the platform state of jobs and the fields of a run that tell how it started.

Revision ID: f3c6a9d2e5b8
Revises: e2b5f8c1d4a7
Create Date: 2026-10-13 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3c6a9d2e5b8"
down_revision: str | Sequence[str] | None = "e2b5f8c1d4a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds `runs.job` (indexed, for the last run of a job), `runs.trigger` and `runs.scheduled_for`
    (all nullable: a run written before, or an ad-hoc `lakana run`, belongs to no job and no
    trigger) and `runs.missed` (not null, server default 0). Creates `job_schedules`, one row per
    (environment, repository, job) with when it fires next, whether it is paused and the one
    waiting start, and `job_fires`, which makes a scheduled time fire only once. Adds
    `deploy_runs.warnings_json` (nullable), the warnings a deploy computes while it changes the
    job schedules.
    """
    with op.batch_alter_table("deploy_runs") as batch_op:
        batch_op.add_column(sa.Column("warnings_json", sa.JSON(), nullable=True))
    with op.batch_alter_table("runs") as batch_op:
        batch_op.add_column(sa.Column("job", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("trigger", sa.String(length=8), nullable=True))
        batch_op.add_column(sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("missed", sa.Integer(), server_default="0", nullable=False))
        batch_op.create_index(batch_op.f("ix_runs_job"), ["job"], unique=False)
    op.create_table(
        "job_schedules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("job", sa.String(length=255), nullable=False),
        sa.Column("next_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("paused_by", sa.Uuid(), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pending_start", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("last_fired_for", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_job_schedules_environment_id_environments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name=op.f("fk_job_schedules_repository_id_repositories"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["paused_by"],
            ["principals.id"],
            name=op.f("fk_job_schedules_paused_by_principals"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_schedules")),
        sa.UniqueConstraint(
            "environment_id",
            "repository_id",
            "job",
            name=op.f("uq_job_schedules_environment_id_repository_id_job"),
        ),
    )
    op.create_index(
        op.f("ix_job_schedules_next_fire_at"), "job_schedules", ["next_fire_at"], unique=False
    )
    op.create_table(
        "job_fires",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_schedule_id", sa.Uuid(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_schedule_id"],
            ["job_schedules.id"],
            name=op.f("fk_job_fires_job_schedule_id_job_schedules"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_job_fires_run_id_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_fires")),
        sa.UniqueConstraint(
            "job_schedule_id",
            "scheduled_for",
            name=op.f("uq_job_fires_job_schedule_id_scheduled_for"),
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("deploy_runs") as batch_op:
        batch_op.drop_column("warnings_json")
    op.drop_table("job_fires")
    op.drop_index(op.f("ix_job_schedules_next_fire_at"), table_name="job_schedules")
    op.drop_table("job_schedules")
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_index(batch_op.f("ix_runs_job"))
        batch_op.drop_column("missed")
        batch_op.drop_column("scheduled_for")
        batch_op.drop_column("trigger")
        batch_op.drop_column("job")
