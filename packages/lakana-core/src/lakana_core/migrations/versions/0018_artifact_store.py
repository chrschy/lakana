"""artifact_versions, artifact_uploads, artifact_blobs: the artifact store.

Revision ID: e2a7c4f1b6d8
Revises: c8e1a5d7f3b9
Create Date: 2026-10-06 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2a7c4f1b6d8"
down_revision: str | Sequence[str] | None = "c8e1a5d7f3b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds the three tables. `tables.table_type` (VARCHAR(8)) already fits
    ``ARTIFACT``, and `audit_events.event_type` (VARCHAR(64)) the five new
    event types.
    """
    op.create_table(
        "artifact_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("is_directory", sa.Boolean(), nullable=False),
        sa.Column("task_run_id", sa.Uuid(), nullable=True),
        sa.Column("claim", sa.Integer(), nullable=True),
        sa.Column("job_version_id", sa.Uuid(), nullable=True),
        sa.Column("commit_sha", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["tables.id"],
            name=op.f("fk_artifact_versions_artifact_id_tables"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_run_id"],
            ["task_runs.id"],
            name=op.f("fk_artifact_versions_task_run_id_task_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["job_version_id"],
            ["job_versions.id"],
            name=op.f("fk_artifact_versions_job_version_id_job_versions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["principals.id"],
            name=op.f("fk_artifact_versions_created_by_principals"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["deleted_by"],
            ["principals.id"],
            name=op.f("fk_artifact_versions_deleted_by_principals"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_artifact_versions")),
        sa.UniqueConstraint(
            "artifact_id", "version", name=op.f("uq_artifact_versions_artifact_id_version")
        ),
    )
    with op.batch_alter_table("artifact_versions") as batch_op:
        batch_op.create_index(op.f("ix_artifact_versions_artifact_id"), ["artifact_id"])
    op.create_table(
        "artifact_uploads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("task_run_id", sa.Uuid(), nullable=True),
        sa.Column("claim", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("committed_version", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["tables.id"],
            name=op.f("fk_artifact_uploads_artifact_id_tables"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name=op.f("fk_artifact_uploads_principal_id_principals"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_run_id"],
            ["task_runs.id"],
            name=op.f("fk_artifact_uploads_task_run_id_task_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_artifact_uploads")),
    )
    with op.batch_alter_table("artifact_uploads") as batch_op:
        batch_op.create_index(op.f("ix_artifact_uploads_artifact_id"), ["artifact_id"])
        batch_op.create_index(op.f("ix_artifact_uploads_expires_at"), ["expires_at"])
    op.create_table(
        "artifact_blobs",
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["tables.id"],
            name=op.f("fk_artifact_blobs_artifact_id_tables"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("artifact_id", "sha256", name=op.f("pk_artifact_blobs")),
    )


def downgrade() -> None:
    """Downgrade schema.

    Deletes every `ARTIFACT` row of `tables` first: code from before this
    revision has no `TableType.ARTIFACT`, so a leftover row would raise
    `LookupError` wherever tables are loaded. The stored bytes under
    ``artifacts/`` in each environment's storage are left alone.

    Grants, row filters and column masks on a deleted artifact keep their
    rows with a null table id (`table_id` is ``SET NULL``), so they match
    nothing.

    Left behind, as for `0012`: `ARTIFACT_*` audit rows (audit rows are never
    deleted; the old audit listing cannot load them) and lineage edges that
    name a deleted artifact (they have no foreign key, by design).
    """
    op.drop_table("artifact_blobs")
    op.drop_table("artifact_uploads")
    op.drop_table("artifact_versions")
    op.execute(sa.text("DELETE FROM tables WHERE table_type = 'ARTIFACT'"))
