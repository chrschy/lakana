"""m3f security fixes: id bindings, external claims, task-token run link.

Revision ID: 75c7a7bb948a
Revises: a3f7c2e9b4d1
Create Date: 2026-09-24 12:00:00.000000

"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "75c7a7bb948a"
down_revision: str | Sequence[str] | None = "a3f7c2e9b4d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_EVENT_TYPE_LENGTH = 24
_NEW_EVENT_TYPE_LENGTH = 64


def upgrade() -> None:
    """Upgrade schema.

    Adds the id-binding columns for M3f (`grants`,
    `row_filters` and `column_masks` gain `environment_id`/`table_id`),
    `tables.external_claim`, `query_runs.task_token_id`
    (deliberately with no foreign key so a deleted token stays
    detectable), and widens `audit_events.event_type` for the new event
    types added alongside this revision. Existing rows are then backfilled
    by :func:`_backfill_ids`, which also revokes rows whose target no
    longer resolves ('s stale grants).
    """
    with op.batch_alter_table("grants") as batch_op:
        batch_op.add_column(sa.Column("environment_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("table_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            op.f("fk_grants_environment_id_environments"),
            "environments",
            ["environment_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            op.f("fk_grants_table_id_tables"),
            "tables",
            ["table_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(op.f("ix_grants_environment_id"), ["environment_id"])
        batch_op.create_index(op.f("ix_grants_table_id"), ["table_id"])

    with op.batch_alter_table("row_filters") as batch_op:
        batch_op.add_column(sa.Column("environment_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("table_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            op.f("fk_row_filters_environment_id_environments"),
            "environments",
            ["environment_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            op.f("fk_row_filters_table_id_tables"),
            "tables",
            ["table_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(op.f("ix_row_filters_environment_id"), ["environment_id"])
        batch_op.create_index(op.f("ix_row_filters_table_id"), ["table_id"])

    with op.batch_alter_table("column_masks") as batch_op:
        batch_op.add_column(sa.Column("environment_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("table_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            op.f("fk_column_masks_environment_id_environments"),
            "environments",
            ["environment_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            op.f("fk_column_masks_table_id_tables"),
            "tables",
            ["table_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(op.f("ix_column_masks_environment_id"), ["environment_id"])
        batch_op.create_index(op.f("ix_column_masks_table_id"), ["table_id"])

    with op.batch_alter_table("tables") as batch_op:
        batch_op.add_column(sa.Column("external_claim", sa.String(length=1024), nullable=True))

    with op.batch_alter_table("query_runs") as batch_op:
        batch_op.add_column(sa.Column("task_token_id", sa.Uuid(), nullable=True))
        batch_op.create_index(op.f("ix_query_runs_task_token_id"), ["task_token_id"])

    with op.batch_alter_table("audit_events") as batch_op:
        batch_op.alter_column(
            "event_type",
            existing_type=sa.String(length=_OLD_EVENT_TYPE_LENGTH),
            type_=sa.String(length=_NEW_EVENT_TYPE_LENGTH),
            existing_nullable=False,
        )

    _backfill_ids()
    _backfill_external_claims()


_RAW_PREFIX = "raw"
_MAX_CLAIM_LENGTH = 1024
_WILDCARD_CHARACTERS = frozenset("*?[")


def _parse_external_claim(pattern: str) -> str | None:
    """Duplicate, minimally, `lakana_core.catalog.external_location.parse_external_location`.

    Migrations must not import application code: a future change to
    `lakana_core.catalog.external_location` (a bug fix, a rule change) must
    not silently change what an already-applied, historical migration did.
    This inlines just enough of the same `raw/<dir...>/<basename-glob>`
    confinement rule to decide whether a pre-existing
    row's stored pattern would parse under it, for the one-time backfill in
    :func:`_backfill_external_claims`. Kept in sync with
    `parse_external_location`'s rules by
    `test_0011_backfills_external_claim_for_parseable_legacy_rows`; it does
    not need to track that function forever; the accepted shape at the
    time of M3f is fixed history for anyone reading this migration later.

    Parameters
    ----------
    pattern
        A stored `external_config["location_pattern"]` value.

    Returns
    -------
    str or None
        The directory claim, if `pattern` parses under the confinement
        rule; `None` if it does not, so the caller leaves the row's
        `external_claim` at its default `NULL` rather than guessing.
    """
    if not pattern or pattern.startswith("/") or "**" in pattern:
        return None
    components = pattern.split("/")
    if len(components) < 3:
        return None
    if any(component in ("", ".", "..") for component in components):
        return None
    if components[0] != _RAW_PREFIX:
        return None
    if any(_WILDCARD_CHARACTERS & set(component) for component in components[1:-1]):
        return None
    claim = "/".join(components[:-1])
    if len(claim) > _MAX_CLAIM_LENGTH:
        return None
    return claim


def _backfill_external_claims() -> None:
    """Set `tables.external_claim` for pre-existing `EXTERNAL` rows that parse.

    `external_claim` is added as a new, nullable column earlier in this
    same revision, so every row that predates it — every `EXTERNAL` table
    created before this M3f security fix — starts `NULL`. A `NULL` claim
    is a fail-closed state at both create time (`check_claim_free`) and
    query time (`lakana_worker.pipeline` refuses to read any `EXTERNAL` row
    without a matching `external_claim`), so this backfills the claim for
    every existing row whose already-stored `location_pattern` happens to
    parse under the new confinement rule, sparing it that fail-closed
    state. A row whose pattern does not parse is left `NULL`: it cannot be
    queried until an administrator re-creates it with a conforming
    pattern, but the backfill has no safe claim to guess for it.
    """
    bind = op.get_bind()
    tables = sa.table(
        "tables",
        sa.column("id", sa.Uuid()),
        sa.column("table_type", sa.String()),
        sa.column("external_config", sa.JSON()),
        sa.column("external_claim", sa.String()),
    )
    rows = bind.execute(
        sa.select(tables.c.id, tables.c.external_config).where(tables.c.table_type == "EXTERNAL")
    ).all()
    for row in rows:
        config = row.external_config or {}
        pattern = config.get("location_pattern")
        if not isinstance(pattern, str):
            continue
        claim = _parse_external_claim(pattern)
        if claim is not None:
            bind.execute(tables.update().where(tables.c.id == row.id).values(external_claim=claim))


def _backfill_ids() -> None:
    """Resolve every grant's, row filter's and column mask's ids from its names.

    `environment`/`namespace`/`table` stay as display columns; this only
    populates the new `environment_id`/`table_id` columns from them.
    `INSTANCE`-kind grants have no target and are left untouched.
    `ENVIRONMENT`-kind grants only need `environment_id`. `NAMESPACE`-kind
    grants need `environment_id` plus a `namespaces` row matching both
    `environment_id` and `path` (no `namespace_id` column is added, since
    namespace matching stays a path-prefix rule at decision time — see the
    design — this only checks existence for the backfill).
    `TABLE`-kind grants, row filters and column masks are always
    table-scoped and need both `environment_id` and `table_id`. A row
    whose required target no longer resolves is revoked (`revoked_at` set
    to now, `revoked_by` left `NULL`) if it was not already revoked,
    rather than left dangling.

    Every `SELECT` here is materialized with `.all()` before any `UPDATE`
    is issued against the same table, so the update statements never run
    while a result cursor from the same connection is still open.
    """
    bind = op.get_bind()
    now = datetime.now(UTC)

    environments = sa.table(
        "environments", sa.column("id", sa.Uuid()), sa.column("name", sa.String())
    )
    namespaces = sa.table(
        "namespaces",
        sa.column("id", sa.Uuid()),
        sa.column("environment_id", sa.Uuid()),
        sa.column("path", sa.String()),
    )
    tables = sa.table(
        "tables",
        sa.column("id", sa.Uuid()),
        sa.column("namespace_id", sa.Uuid()),
        sa.column("name", sa.String()),
    )

    environment_id_by_name: dict[str, uuid.UUID] = {
        row.name: row.id
        for row in bind.execute(sa.select(environments.c.id, environments.c.name)).all()
    }
    namespace_keys: set[tuple[uuid.UUID, str]] = {
        (row.environment_id, row.path)
        for row in bind.execute(sa.select(namespaces.c.environment_id, namespaces.c.path)).all()
    }
    table_id_by_key: dict[tuple[uuid.UUID, str, str], uuid.UUID] = {
        (row.environment_id, row.path, row.name): row.id
        for row in bind.execute(
            sa.select(
                namespaces.c.environment_id, namespaces.c.path, tables.c.name, tables.c.id
            ).select_from(tables.join(namespaces, tables.c.namespace_id == namespaces.c.id))
        ).all()
    }

    grants = sa.table(
        "grants",
        sa.column("id", sa.Uuid()),
        sa.column("kind", sa.String()),
        sa.column("environment", sa.String()),
        sa.column("namespace", sa.String()),
        sa.column("table", sa.String()),
        sa.column("revoked_at", sa.DateTime(timezone=True)),
        sa.column("environment_id", sa.Uuid()),
        sa.column("table_id", sa.Uuid()),
    )
    grant_rows = bind.execute(
        sa.select(
            grants.c.id,
            grants.c.kind,
            grants.c.environment,
            grants.c.namespace,
            grants.c.table,
            grants.c.revoked_at,
        )
    ).all()
    for row in grant_rows:
        if row.kind == "INSTANCE":
            continue
        environment_id = environment_id_by_name.get(row.environment)
        table_id = None
        if row.kind == "TABLE":
            if environment_id is not None:
                table_id = table_id_by_key.get((environment_id, row.namespace, row.table))
            resolved = environment_id is not None and table_id is not None
        elif row.kind == "NAMESPACE":
            namespace_key = (environment_id, row.namespace)
            resolved = environment_id is not None and namespace_key in namespace_keys
        else:  # ENVIRONMENT
            resolved = environment_id is not None
        if resolved:
            bind.execute(
                grants.update()
                .where(grants.c.id == row.id)
                .values(environment_id=environment_id, table_id=table_id)
            )
        elif row.revoked_at is None:
            bind.execute(grants.update().where(grants.c.id == row.id).values(revoked_at=now))

    for table_name in ("row_filters", "column_masks"):
        target = sa.table(
            table_name,
            sa.column("id", sa.Uuid()),
            sa.column("environment", sa.String()),
            sa.column("namespace", sa.String()),
            sa.column("table", sa.String()),
            sa.column("revoked_at", sa.DateTime(timezone=True)),
            sa.column("environment_id", sa.Uuid()),
            sa.column("table_id", sa.Uuid()),
        )
        target_rows = bind.execute(
            sa.select(
                target.c.id,
                target.c.environment,
                target.c.namespace,
                target.c.table,
                target.c.revoked_at,
            )
        ).all()
        for row in target_rows:
            environment_id = environment_id_by_name.get(row.environment)
            table_id = (
                table_id_by_key.get((environment_id, row.namespace, row.table))
                if environment_id is not None
                else None
            )
            if environment_id is not None and table_id is not None:
                bind.execute(
                    target.update()
                    .where(target.c.id == row.id)
                    .values(environment_id=environment_id, table_id=table_id)
                )
            elif row.revoked_at is None:
                bind.execute(target.update().where(target.c.id == row.id).values(revoked_at=now))


def downgrade() -> None:
    """Downgrade schema.

    `audit_events.event_type` is deliberately left widened at
    `String(64)` rather than narrowed back to `String(24)`. Rows may
    already have been recorded with one of the M3f event types longer
    than 24 characters (for example `ENVIRONMENT_PROTECTION_CHANGED`, 30
    characters); narrowing the column would fail outright on Postgres if
    any such row exists, and audit rows are never deleted just to make
    room for a schema downgrade. Code from before this revision only
    validates `event_type` against the `AuditEventType` enum in Python,
    never against the column's SQL length, so leaving the column widened
    does not affect it.
    """
    with op.batch_alter_table("query_runs") as batch_op:
        batch_op.drop_index(op.f("ix_query_runs_task_token_id"))
        batch_op.drop_column("task_token_id")

    with op.batch_alter_table("tables") as batch_op:
        batch_op.drop_column("external_claim")

    with op.batch_alter_table("column_masks") as batch_op:
        batch_op.drop_index(op.f("ix_column_masks_table_id"))
        batch_op.drop_index(op.f("ix_column_masks_environment_id"))
        batch_op.drop_constraint(op.f("fk_column_masks_table_id_tables"), type_="foreignkey")
        batch_op.drop_constraint(
            op.f("fk_column_masks_environment_id_environments"), type_="foreignkey"
        )
        batch_op.drop_column("table_id")
        batch_op.drop_column("environment_id")

    with op.batch_alter_table("row_filters") as batch_op:
        batch_op.drop_index(op.f("ix_row_filters_table_id"))
        batch_op.drop_index(op.f("ix_row_filters_environment_id"))
        batch_op.drop_constraint(op.f("fk_row_filters_table_id_tables"), type_="foreignkey")
        batch_op.drop_constraint(
            op.f("fk_row_filters_environment_id_environments"), type_="foreignkey"
        )
        batch_op.drop_column("table_id")
        batch_op.drop_column("environment_id")

    with op.batch_alter_table("grants") as batch_op:
        batch_op.drop_index(op.f("ix_grants_table_id"))
        batch_op.drop_index(op.f("ix_grants_environment_id"))
        batch_op.drop_constraint(op.f("fk_grants_table_id_tables"), type_="foreignkey")
        batch_op.drop_constraint(op.f("fk_grants_environment_id_environments"), type_="foreignkey")
        batch_op.drop_column("table_id")
        batch_op.drop_column("environment_id")
