"""connections: source systems per environment, their tests, and the secret use CONNECTION.

Revision ID: a7c3e9f1b2d4
Revises: f1a2b3c4d5e6
Create Date: 2026-10-23 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c3e9f1b2d4"
down_revision: str | Sequence[str] | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled out here, not imported, so that a later change to the model cannot change this revision.
# `use_kind IS NOT NULL` keeps a NULL kind from making a branch NULL: a CHECK passes on NULL.
_BEFORE = (
    "(use_kind IS NULL AND use_target_id IS NULL)"
    " OR (use_kind IS NOT NULL AND use_kind = 'ENVIRONMENT' AND use_target_id IS NULL)"
    " OR (use_kind IS NOT NULL AND use_kind IN ('PACKAGE_INDEX', 'REPOSITORY')"
    " AND use_target_id IS NOT NULL)"
)
_AFTER = (
    "(use_kind IS NULL AND use_target_id IS NULL)"
    " OR (use_kind IS NOT NULL AND use_kind = 'ENVIRONMENT' AND use_target_id IS NULL)"
    " OR (use_kind IS NOT NULL"
    " AND use_kind IN ('PACKAGE_INDEX', 'REPOSITORY', 'CONNECTION')"
    " AND use_target_id IS NOT NULL)"
)
_CHECK = "ck_secrets_use_consistent"
_KINDS = (
    "POSTGRESQL",
    "MARIADB",
    "SQLITE_FILE",
    "HTTP",
    "S3",
    "GCS",
    "AZURE",
    "DIRECTORY",
    "TCP",
)
_STATES = ("QUEUED", "RUNNING", "DONE")
_OUTCOMES = (
    "OK",
    "DNS",
    "REFUSED",
    "TIMEOUT",
    "TLS",
    "CREDENTIALS",
    "NOT_FOUND",
    "NOT_ALLOWED",
    "ROOT_UNAVAILABLE",
    "FAILED",
)


def upgrade() -> None:
    """Upgrade schema.

    Creates `connections` and `connection_tests`, both empty; lets a secret be bound to a
    connection (the check `ck_secrets_use_consistent` accepts `CONNECTION` with a target id);
    adds `lineage_edges.source_kind`, ``table`` on every existing edge, since every edge so far
    names a table. No data migration: no connection exists before this revision.
    """
    op.create_table(
        "connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=63), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(*_KINDS, name="connectionkind", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("address", sa.JSON(), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("secret_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_connections_environment_id_environments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["secret_id"],
            ["secrets.id"],
            name=op.f("fk_connections_secret_id_secrets"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["principals.id"],
            name=op.f("fk_connections_created_by_principals"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connections")),
        sa.UniqueConstraint("environment_id", "name", name=op.f("uq_connections_environment_name")),
    )
    op.create_index(
        op.f("ix_connections_environment_id"), "connections", ["environment_id"], unique=False
    )
    op.create_table(
        "connection_tests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(*_STATES, name="connectionteststate", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column(
            "outcome",
            sa.Enum(*_OUTCOMES, name="connectiontestoutcome", native_enum=False, length=16),
            nullable=True,
        ),
        sa.Column("message", sa.String(length=1000), nullable=True),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            name=op.f("fk_connection_tests_connection_id_connections"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_connection_tests_environment_id_environments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["principals.id"],
            name=op.f("fk_connection_tests_requested_by_principals"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connection_tests")),
    )
    op.create_index(
        op.f("ix_connection_tests_connection_id"),
        "connection_tests",
        ["connection_id"],
        unique=False,
    )
    op.create_index(
        "ix_connection_tests_claim",
        "connection_tests",
        ["state", "lease_expires_at"],
        unique=False,
    )
    with op.batch_alter_table("secrets") as batch_op:
        batch_op.drop_constraint(op.f(_CHECK), type_="check")
        batch_op.create_check_constraint(op.f(_CHECK), _AFTER)
    with op.batch_alter_table("lineage_edges") as batch_op:
        batch_op.add_column(
            sa.Column("source_kind", sa.String(length=16), server_default="table", nullable=False)
        )


def downgrade() -> None:
    """Downgrade schema.

    Unbinds every secret bound to a connection (the secrets stay, unbound: the previous
    release knows no such use), restores the earlier check, deletes the lineage edges whose
    source is a connection (the previous release would read them as tables of that name),
    drops `source_kind` and both tables. The connections themselves are gone; create them
    again after an upgrade.
    """
    op.execute(sa.text("DELETE FROM lineage_edges WHERE source_kind = 'connection'"))
    op.execute(
        sa.text(
            "UPDATE secrets SET use_kind = NULL, use_target_id = NULL WHERE use_kind = 'CONNECTION'"
        )
    )
    with op.batch_alter_table("secrets") as batch_op:
        batch_op.drop_constraint(op.f(_CHECK), type_="check")
        batch_op.create_check_constraint(op.f(_CHECK), _BEFORE)
    with op.batch_alter_table("lineage_edges") as batch_op:
        batch_op.drop_column("source_kind")
    op.drop_index("ix_connection_tests_claim", table_name="connection_tests")
    op.drop_index(op.f("ix_connection_tests_connection_id"), table_name="connection_tests")
    op.drop_table("connection_tests")
    op.drop_index(op.f("ix_connections_environment_id"), table_name="connections")
    op.drop_table("connections")
