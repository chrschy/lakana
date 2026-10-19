"""table_locations: the record of each managed table's storage location.

Revision ID: b1e8f4c7a3d9
Revises: a0d7e3b6f2c8
Create Date: 2026-10-19 18:00:00.000000

"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1e8f4c7a3d9"
down_revision: str | Sequence[str] | None = "a0d7e3b6f2c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_METADATA_DIRECTORY = "/metadata/"


def upgrade() -> None:
    """Upgrade schema.

    Creates `table_locations` and backfills one `LIVE` record for every managed (Iceberg) table
    that has a metadata file, with the table's root location derived from that file's location
    as `lakana_core.storage.resolver.table_root_from_metadata_location` derives it. External
    tables, views and artifacts get none. Two older tables that share a location (created
    before locations carried the table's id) get one record between them; either protects the
    directory. Nothing is deleted.
    """
    op.create_table(
        "table_locations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=True),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("location", sa.String(length=1024), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "CLAIMED",
                "LIVE",
                "DROPPED",
                "ABANDONED",
                name="tablelocationstate",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("holder_run_id", sa.Uuid(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dropped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purge_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_policy_ids", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_table_locations_environment_id_environments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_table_locations")),
        sa.UniqueConstraint("location", name=op.f("uq_table_locations_location")),
    )
    op.create_index(
        op.f("ix_table_locations_environment_id"),
        "table_locations",
        ["environment_id"],
        unique=False,
    )
    op.create_index(op.f("ix_table_locations_state"), "table_locations", ["state"], unique=False)
    op.create_index(
        op.f("ix_table_locations_table_id"), "table_locations", ["table_id"], unique=False
    )
    _backfill()


def _backfill() -> None:
    """Insert a `LIVE` record for every managed table that has a metadata file."""
    connection = op.get_bind()
    tables = sa.table(
        "tables",
        sa.column("id", sa.Uuid()),
        sa.column("namespace_id", sa.Uuid()),
        sa.column("table_type", sa.String()),
        sa.column("metadata_location", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    namespaces = sa.table(
        "namespaces", sa.column("id", sa.Uuid()), sa.column("environment_id", sa.Uuid())
    )
    locations = sa.table(
        "table_locations",
        sa.column("id", sa.Uuid()),
        sa.column("table_id", sa.Uuid()),
        sa.column("environment_id", sa.Uuid()),
        sa.column("location", sa.String()),
        sa.column("state", sa.String()),
        sa.column("claimed_at", sa.DateTime(timezone=True)),
        sa.column("revoked_policy_ids", sa.JSON()),
    )
    rows = connection.execute(
        sa.select(
            tables.c.id,
            namespaces.c.environment_id,
            tables.c.metadata_location,
            tables.c.created_at,
        )
        .join(namespaces, tables.c.namespace_id == namespaces.c.id)
        .where(tables.c.table_type == "ICEBERG", tables.c.metadata_location.is_not(None))
        .order_by(tables.c.created_at, tables.c.id)
    ).all()
    seen: set[str] = set()
    for table_id, environment_id, metadata_location, created_at in rows:
        index = metadata_location.rfind(_METADATA_DIRECTORY)
        if index == -1:
            continue
        location = metadata_location[:index]
        if location in seen:
            continue
        seen.add(location)
        connection.execute(
            locations.insert().values(
                id=uuid.uuid4(),
                table_id=table_id,
                environment_id=environment_id,
                location=location,
                state="LIVE",
                claimed_at=created_at,
                revoked_policy_ids=[],
            )
        )


def downgrade() -> None:
    """Downgrade schema.

    Drops the records. No file is touched; the previous release keeps no record of locations.
    """
    op.drop_index(op.f("ix_table_locations_table_id"), table_name="table_locations")
    op.drop_index(op.f("ix_table_locations_state"), table_name="table_locations")
    op.drop_index(op.f("ix_table_locations_environment_id"), table_name="table_locations")
    op.drop_table("table_locations")
