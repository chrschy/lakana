"""m5b projects and deploy: repositories, deploy runs, job versions, the pointer.

Revision ID: d1e5f3a7b9c2
Revises: c8d4e2f1a6b3
Create Date: 2026-10-01 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1e5f3a7b9c2"
down_revision: str | Sequence[str] | None = "c8d4e2f1a6b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KIND = sa.Enum("LOCAL", "HTTPS", "SSH", name="repositorykind", native_enum=False, length=8)
_DEPLOY_KIND = sa.Enum(
    "DEPLOY", "DRY_RUN", "ROLLBACK", name="deploykind", native_enum=False, length=8
)
_DEPLOY_STATUS = sa.Enum(
    "QUEUED", "RUNNING", "SUCCEEDED", "FAILED", name="deploystatus", native_enum=False, length=9
)


def upgrade() -> None:
    """Upgrade schema.

    Adds `repositories`, `job_versions`, `environment_deployments` and
    `deploy_runs`. `grants.action` (VARCHAR(18)) already fits
    ``MANAGE_REPOSITORY`` and `audit_events.event_type` (VARCHAR(64)) the
    new event types.
    """
    op.create_table(
        "repositories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("kind", _KIND, nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("credential_secret", sa.String(length=255), nullable=True),
        sa.Column("credential_username", sa.String(length=255), nullable=True),
        sa.Column("ssh_known_hosts", sa.Text(), nullable=True),
        sa.Column("default_ref", sa.String(length=255), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["principals.id"],
            name=op.f("fk_repositories_created_by_principals"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_repositories")),
        sa.UniqueConstraint("name", name=op.f("uq_repositories_name")),
    )
    op.create_table(
        "job_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("commit_sha", sa.String(length=40), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("code_uri", sa.String(length=1024), nullable=False),
        sa.Column("code_sha256", sa.String(length=64), nullable=False),
        sa.Column("deployed_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name=op.f("fk_job_versions_repository_id_repositories"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_job_versions_environment_id_environments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["deployed_by"],
            ["principals.id"],
            name=op.f("fk_job_versions_deployed_by_principals"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_versions")),
        sa.UniqueConstraint(
            "repository_id",
            "commit_sha",
            "environment_id",
            name=op.f("uq_job_versions_repository_id_commit_sha_environment_id"),
        ),
    )
    with op.batch_alter_table("job_versions") as batch_op:
        batch_op.create_index(op.f("ix_job_versions_environment_id"), ["environment_id"])
    op.create_table(
        "environment_deployments",
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("current_job_version_id", sa.Uuid(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_environment_deployments_environment_id_environments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name=op.f("fk_environment_deployments_repository_id_repositories"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["current_job_version_id"],
            ["job_versions.id"],
            name=op.f("fk_environment_deployments_current_job_version_id_job_versions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "environment_id", "repository_id", name=op.f("pk_environment_deployments")
        ),
    )
    op.create_table(
        "deploy_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=True),
        sa.Column("kind", _DEPLOY_KIND, nullable=False),
        sa.Column("requested_ref", sa.String(length=255), nullable=True),
        sa.Column("resolved_sha", sa.String(length=40), nullable=True),
        sa.Column("rollback_to_job_version_id", sa.Uuid(), nullable=True),
        sa.Column("status", _DEPLOY_STATUS, nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("diff_json", sa.JSON(), nullable=True),
        sa.Column("job_version_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_deploy_runs_environment_id_environments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name=op.f("fk_deploy_runs_repository_id_repositories"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name=op.f("fk_deploy_runs_principal_id_principals"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["rollback_to_job_version_id"],
            ["job_versions.id"],
            name=op.f("fk_deploy_runs_rollback_to_job_version_id_job_versions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["job_version_id"],
            ["job_versions.id"],
            name=op.f("fk_deploy_runs_job_version_id_job_versions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deploy_runs")),
    )
    with op.batch_alter_table("deploy_runs") as batch_op:
        batch_op.create_index(op.f("ix_deploy_runs_environment_id"), ["environment_id"])
        batch_op.create_index(op.f("ix_deploy_runs_status"), ["status"])


def downgrade() -> None:
    """Downgrade schema.

    Deletes every `DEPLOY` and `MANAGE_REPOSITORY` grant first, revoked or
    not, for the reason `0012`'s downgrade gives for `MAINTAIN_TABLE`: the
    previous release's `Action` enum has no such member, so a leftover row
    raises `LookupError` on every authorization of its subject.

    Left behind, as for `0011` and `0012`: the M5b audit rows (audit rows
    are never deleted). Every repository, deploy run, JobVersion and
    pointer is dropped with its table.
    """
    op.execute(sa.text("DELETE FROM grants WHERE action IN ('DEPLOY', 'MANAGE_REPOSITORY')"))
    with op.batch_alter_table("deploy_runs") as batch_op:
        batch_op.drop_index(op.f("ix_deploy_runs_status"))
        batch_op.drop_index(op.f("ix_deploy_runs_environment_id"))
    op.drop_table("deploy_runs")
    op.drop_table("environment_deployments")
    with op.batch_alter_table("job_versions") as batch_op:
        batch_op.drop_index(op.f("ix_job_versions_environment_id"))
    op.drop_table("job_versions")
    op.drop_table("repositories")
