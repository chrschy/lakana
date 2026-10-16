"""m5a materialization: write-run columns, views, lineage edges.

Revision ID: c8d4e2f1a6b3
Revises: 75c7a7bb948a
Create Date: 2026-09-29 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8d4e2f1a6b3"
down_revision: str | Sequence[str] | None = "75c7a7bb948a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WRITE_MODE = sa.Enum(
    "CREATE",
    "REPLACE",
    "APPEND",
    "VIEW",
    "MAINTAIN",
    name="writemode",
    native_enum=False,
    length=16,
)


def upgrade() -> None:
    """Upgrade schema.

    Adds `lineage_edges`, the write-run columns on `query_runs` and
    `tables.view_definition`. `tables.table_type` needs no change: it is a
    non-native enum stored as VARCHAR(8) with no CHECK constraint, and
    ``VIEW`` fits. `grants.action` (VARCHAR(18)) already fits
    ``MAINTAIN_TABLE`` and `audit_events.event_type` (VARCHAR(64)) the new
    event types.
    """
    op.create_table(
        "lineage_edges",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("source_table_id", sa.Uuid(), nullable=False),
        sa.Column("source_name", sa.String(length=1024), nullable=False),
        sa.Column("source_snapshot_id", sa.BigInteger(), nullable=True),
        sa.Column("target_table_id", sa.Uuid(), nullable=False),
        sa.Column("target_name", sa.String(length=1024), nullable=False),
        sa.Column("target_snapshot_id", sa.BigInteger(), nullable=True),
        sa.Column("query_run_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_lineage_edges_environment_id_environments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["query_run_id"],
            ["query_runs.id"],
            name=op.f("fk_lineage_edges_query_run_id_query_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lineage_edges")),
    )
    with op.batch_alter_table("lineage_edges") as batch_op:
        batch_op.create_index(op.f("ix_lineage_edges_environment_id"), ["environment_id"])
        batch_op.create_index(op.f("ix_lineage_edges_source_table_id"), ["source_table_id"])
        batch_op.create_index(op.f("ix_lineage_edges_target_table_id"), ["target_table_id"])

    with op.batch_alter_table("query_runs") as batch_op:
        batch_op.add_column(sa.Column("write_mode", _WRITE_MODE, nullable=True))
        batch_op.add_column(sa.Column("target_name", sa.String(length=1024), nullable=True))
        batch_op.add_column(sa.Column("target_table_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("incremental_key", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("evolve_schema", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("watermark", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("rows_written", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("snapshot_id", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("schema_diff", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("maintenance", sa.JSON(), nullable=True))
        batch_op.create_index(op.f("ix_query_runs_target_table_id"), ["target_table_id"])

    with op.batch_alter_table("tables") as batch_op:
        batch_op.add_column(sa.Column("view_definition", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    Drops every view row and every `MAINTAIN_TABLE` grant first, revoked
    or not: code from before this revision has no `TableType.VIEW` or
    `Action.MAINTAIN_TABLE` member, so a leftover row would raise
    `LookupError` on load — for a grant, on every authorization of its
    subject (`GrantAuthorization` loads all of a subject's active grants)
    and every grant listing. Deleted rather than revoked because a revoked
    row is still loaded by any query that doesn't filter on `revoked_at`,
    `revoke` itself among them; the grant stays in the audit log.

    Left behind, as for `0011`: `TABLE_WRITE` and `TABLE_MAINTAINED` audit
    rows (audit rows are never deleted; the old audit listing can't load
    them), and a write or maintenance run still `QUEUED`, which old code
    would run as a plain read. Drain the queue before downgrading.
    """
    op.execute(sa.text("DELETE FROM tables WHERE table_type = 'VIEW'"))
    op.execute(sa.text("DELETE FROM grants WHERE action = 'MAINTAIN_TABLE'"))
    with op.batch_alter_table("tables") as batch_op:
        batch_op.drop_column("view_definition")

    with op.batch_alter_table("query_runs") as batch_op:
        batch_op.drop_index(op.f("ix_query_runs_target_table_id"))
        for column in (
            "maintenance",
            "schema_diff",
            "snapshot_id",
            "rows_written",
            "watermark",
            "evolve_schema",
            "incremental_key",
            "target_table_id",
            "target_name",
            "write_mode",
        ):
            batch_op.drop_column(column)

    op.drop_table("lineage_edges")
