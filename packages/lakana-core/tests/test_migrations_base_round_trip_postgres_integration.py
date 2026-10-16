"""Postgres migration round trip: `upgrade head`, `downgrade base`, `upgrade head`.

`test_migrations_postgres_integration.py` covers `upgrade head` -> `downgrade -1`
-> `upgrade head` with seeded data, but that only ever exercises one migration's
`downgrade()` — 0011's. Migrations 0001-0010's `downgrade()` functions have never run
against a real Postgres: nothing in CI walked all the way back down to an empty schema.

This test does that, schema only (a full `downgrade base` drops every application row, so
there is no data to round-trip). It upgrades to head, downgrades one revision at a time all
the way to `base`, and upgrades back to head, then asserts the schema after the second
upgrade is identical to the schema after the first — same tables, columns (type and
nullability), indexes, unique constraints, foreign keys, and enum types. A downgrade that
forgets to drop something (an enum type, an index, a constraint) would otherwise leave the
database in a state where the second `upgrade head` either fails outright or silently
recreates an object that already exists with a slightly different definition; comparing the
full schema snapshot is what catches the silent-drift case that a bare "did it raise"
check would miss.

Downgrading one revision at a time, rather than a single `downgrade base` call, is what lets
a broken migration's revision id show up in the assertion message instead of just "something
in the chain failed."
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
import sqlalchemy
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.script.base import Script
from testcontainers.postgres import PostgresContainer

from lakana_core import migrate
from lakana_core.models import Base

pytestmark = pytest.mark.integration

_SchemaSnapshot = dict[str, object]


def test_upgrade_head_downgrade_base_upgrade_head_reproduces_the_schema() -> None:
    """`upgrade head` -> `downgrade base` -> `upgrade head` reproduces the same schema.

    Walks the downgrade one revision at a time from head to `base`, asserting after each
    step that it landed on the expected revision (naming the offending revision id in the
    assertion message if not), then asserts that no application table survived the trip
    down to `base`, and finally that the schema produced by the second `upgrade head` is
    identical — tables, columns (type and nullability), indexes, unique constraints,
    foreign keys, and enum types — to the schema produced by the first.

    Raises
    ------
    AssertionError
        If any downgrade step lands on the wrong revision, if an application table
        survives the trip to `base`, or if the two schema snapshots differ.
    """
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        head = _head_revision(url)
        revisions = _revisions_head_to_base(url)
        engine = sqlalchemy.create_engine(url)

        migrate.upgrade(url)
        assert _current_revision(engine) == head
        schema_after_first_upgrade = _schema_snapshot(engine)

        _downgrade_one_revision_at_a_time(url, engine, revisions)
        _assert_only_alembic_version_table_remains(engine)

        migrate.upgrade(url)
        assert _current_revision(engine) == head
        schema_after_second_upgrade = _schema_snapshot(engine)

        engine.dispose()

    assert schema_after_second_upgrade == schema_after_first_upgrade


def test_upgraded_schema_matches_the_orm_models_on_postgres() -> None:
    """`upgrade head` on Postgres yields exactly the schema the models declare.

    The SQLite twin (`test_migrations.py`) cannot see a difference that only Postgres
    reflects: server defaults, types, check constraints and index details.

    Raises
    ------
    AssertionError
        If alembic's comparison of the migrated schema with `Base.metadata` is not empty.
    """
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url)
        engine = sqlalchemy.create_engine(url)
        try:
            with engine.connect() as connection:
                diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
        finally:
            engine.dispose()

    assert diff == []


def _head_revision(url: str) -> str:
    """Return the migrations script directory's single head revision.

    Reading the head from the script directory, rather than hard-coding a revision id,
    means a newly added migration is picked up automatically instead of silently not
    being exercised by this test.

    Parameters
    ----------
    url
        SQLAlchemy URL passed only to build the Alembic `Config`; no connection is made.

    Returns
    -------
    str
        The head revision id.

    Raises
    ------
    AssertionError
        If the script directory has no head revision.
    """
    script = ScriptDirectory.from_config(migrate.alembic_config(url))
    head = script.get_current_head()
    assert head is not None, "migrations script directory has no head revision"
    return head


def _revisions_head_to_base(url: str) -> list[Script]:
    """Return every migration revision, ordered from head down to `base`.

    Parameters
    ----------
    url
        SQLAlchemy URL passed only to build the Alembic `Config`; no connection is made.

    Returns
    -------
    list of Script
        Revisions in downgrade order: the current head first, the revision immediately
        above `base` last.
    """
    script = ScriptDirectory.from_config(migrate.alembic_config(url))
    return list(script.walk_revisions("base", "head"))


def _downgrade_one_revision_at_a_time(
    url: str, engine: sqlalchemy.Engine, revisions: Sequence[Script]
) -> None:
    """Downgrade the database one migration at a time, from head to `base`.

    Parameters
    ----------
    url
        SQLAlchemy URL of the metadata database.
    engine
        Engine connected to the same database, used to check the revision landed on
        after each step.
    revisions
        Revisions in downgrade order, as returned by `_revisions_head_to_base`.

    Raises
    ------
    AssertionError
        If a revision's `downgrade()` raises, or if it leaves the database on a
        revision other than the one immediately below it. Either failure names the
        offending revision id.
    """
    for rev in revisions:
        down_revision = rev.down_revision
        target = down_revision if isinstance(down_revision, str) else "base"
        try:
            migrate.downgrade(url, target)
        except Exception as exc:
            raise AssertionError(
                f"downgrade of revision {rev.revision} to {target!r} raised: {exc}"
            ) from exc
        actual = _current_revision(engine)
        expected = None if target == "base" else target
        assert actual == expected, (
            f"downgrade of revision {rev.revision} to {target!r} left the database on "
            f"revision {actual!r}, expected {expected!r}"
        )


def _current_revision(engine: sqlalchemy.Engine) -> str | None:
    """Return the database's current Alembic revision, or `None` if there isn't one.

    Parameters
    ----------
    engine
        Engine connected to the metadata database.

    Returns
    -------
    str or None
        The `alembic_version.version_num` value, or `None` if the `alembic_version`
        table is absent or holds no row — both are valid post-`downgrade base` states.
    """
    inspector = sqlalchemy.inspect(engine)
    if "alembic_version" not in inspector.get_table_names():
        return None
    with engine.connect() as conn:
        return conn.execute(
            sqlalchemy.text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()


def _assert_only_alembic_version_table_remains(engine: sqlalchemy.Engine) -> None:
    """Assert that `downgrade base` left no application table behind.

    Parameters
    ----------
    engine
        Engine connected to the metadata database.

    Raises
    ------
    AssertionError
        If any table other than `alembic_version` remains.
    """
    inspector = sqlalchemy.inspect(engine)
    tables = set(inspector.get_table_names())
    assert tables <= {"alembic_version"}, (
        f"application tables survived downgrade to base: {sorted(tables - {'alembic_version'})}"
    )


def _schema_snapshot(engine: sqlalchemy.Engine) -> _SchemaSnapshot:
    """Capture the reflected schema: tables, columns, indexes, constraints, and enums.

    Parameters
    ----------
    engine
        Engine connected to the metadata database.

    Returns
    -------
    dict
        A JSON-comparable snapshot: `{"tables": {name: {...}}, "enums": [...]}`. Two
        snapshots taken against schemas produced by the same migrations compare equal
        only if every reflected column type, nullability, index, unique constraint,
        foreign key, and enum type matches.
    """
    inspector = sqlalchemy.inspect(engine)
    tables: dict[str, object] = {}
    for table_name in sorted(inspector.get_table_names()):
        if table_name == "alembic_version":
            continue
        tables[table_name] = {
            "columns": sorted(
                (col["name"], str(col["type"]), bool(col["nullable"]))
                for col in inspector.get_columns(table_name)
            ),
            "indexes": sorted(
                (idx["name"], tuple(idx["column_names"]), bool(idx["unique"]))
                for idx in inspector.get_indexes(table_name)
            ),
            "unique_constraints": sorted(
                (uc["name"], tuple(uc["column_names"]))
                for uc in inspector.get_unique_constraints(table_name)
            ),
            "foreign_keys": sorted(
                (
                    fk["name"],
                    tuple(fk["constrained_columns"]),
                    fk["referred_table"],
                    tuple(fk["referred_columns"]),
                )
                for fk in inspector.get_foreign_keys(table_name)
            ),
        }
    return {"tables": tables, "enums": _enum_types(engine)}


def _enum_types(engine: sqlalchemy.Engine) -> list[tuple[str, tuple[str, ...]]]:
    """Return every native Postgres enum type defined in the `public` schema.

    None of today's migrations declare a native enum (every `sa.Enum(...)` passes
    `native_enum=False`, so enums are plain `VARCHAR` plus a `CHECK` constraint), but a
    downgrade that forgot to `DROP TYPE` after a future migration added one would only
    show up here — `Inspector.get_columns`/`get_table_names` don't reflect stray
    `pg_type` rows for a type no column references any more.

    Parameters
    ----------
    engine
        Engine connected to the metadata database.

    Returns
    -------
    list of tuple
        `(type_name, labels)` pairs, sorted by type name, for every enum type in the
        `public` schema.
    """
    query = sqlalchemy.text(
        "SELECT t.typname, array_agg(e.enumlabel ORDER BY e.enumsortorder) "
        "FROM pg_type t "
        "JOIN pg_enum e ON e.enumtypid = t.oid "
        "JOIN pg_namespace n ON n.oid = t.typnamespace "
        "WHERE n.nspname = 'public' "
        "GROUP BY t.typname "
        "ORDER BY t.typname"
    )
    with engine.connect() as conn:
        return [(name, tuple(labels)) for name, labels in conn.execute(query)]
