"""A write pinned to the target snapshot it read conflicts when the target moved on.

An incremental append computes its watermark from one
snapshot of its target, and a refresh that reads its own target computes its
result from one. If another writer commits in between, writing on top of the
newer snapshot duplicates rows (incremental) or drops them unread (replace).
The write must conflict instead.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pyarrow as pa
import pytest
from pyiceberg.table import StaticTable
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog import service as service_module
from lakana_core.catalog.service import CatalogService
from lakana_core.catalog.writes import SnapshotPin
from lakana_core.config import DatabaseConfig, StorageConfig
from lakana_core.db import create_engine
from lakana_core.errors import ConflictError
from lakana_core.identifiers import TableRef
from lakana_core.models import Base, Principal, PrincipalType, WriteMode
from lakana_core.storage.resolver import StorageResolver


def _ref(dotted: str) -> TableRef:
    """Address a table in `dev`.

    Parameters
    ----------
    dotted
        The dotted name.

    Returns
    -------
    TableRef
        The reference.
    """
    return TableRef.from_dotted("dev", dotted)


def _ids(ids: list[int]) -> pa.Table:
    """Build a one-column result.

    Parameters
    ----------
    ids
        The `id` values.

    Returns
    -------
    pa.Table
        The rows.
    """
    return pa.table({"id": pa.array(ids, pa.int64())})


def _write(
    catalog: CatalogService,
    principal: PrincipalRef,
    dotted: str,
    mode: WriteMode,
    data: pa.Table,
    pin: SnapshotPin | None = None,
) -> int | None:
    """Write `data` and return the snapshot committed.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The writer.
    dotted
        The target.
    mode
        The write mode.
    data
        The rows.
    pin
        Passed through.

    Returns
    -------
    int or None
        The snapshot committed.
    """
    return catalog.write_batches(
        principal,
        _ref(dotted),
        mode=mode,
        schema=data.schema,
        batches=data.to_batches(),
        pin=pin,
    ).snapshot_id


def _ids_of(catalog: CatalogService, principal: PrincipalRef, dotted: str) -> list[int]:
    """Read a table's ids back, sorted.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The reader.
    dotted
        The table.

    Returns
    -------
    list of int
        The ids.
    """
    loaded = catalog.load_table(principal, _ref(dotted))
    rows = StaticTable.from_metadata(loaded.metadata_location).scan().to_arrow().to_pylist()
    return sorted(row["id"] for row in rows)


def _files(root: Path) -> set[str]:
    """List every data and manifest file under the storage root.

    Parameters
    ----------
    root
        The storage root.

    Returns
    -------
    set of str
        Root-relative paths, ignoring metadata JSON.
    """
    return {
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and not path.name.endswith(".metadata.json")
    }


@pytest.fixture
def two_sessions(tmp_path: Path) -> Iterator[tuple[Session, Session, PrincipalRef, Path]]:
    """Provide two sessions on one SQLite file database, and `dev` at a local root.

    Parameters
    ----------
    tmp_path
        Holds the database file and the storage root.

    Yields
    ------
    tuple
        Session A, session B, the principal and the storage root.
    """
    engine = create_engine(DatabaseConfig(url=f"sqlite:///{tmp_path / 'catalog.db'}"))
    Base.metadata.create_all(engine)
    root = tmp_path / "lake"
    root.mkdir()
    with Session(engine) as a, Session(engine) as b:
        principal_id = uuid.uuid4()
        a.add(Principal(id=principal_id, type=PrincipalType.USER, name="admin"))
        a.commit()
        principal = PrincipalRef(id=principal_id, name="admin", type=PrincipalType.USER)
        CatalogService(a, StorageResolver(a), AllowAllAuthorization()).create_environment(
            principal,
            name="dev",
            storage=StorageConfig(scheme="file", root=f"file://{root}", options={}),
        )
        yield a, b, principal, root
    engine.dispose()


def _catalogs(session_a: Session, session_b: Session) -> tuple[CatalogService, CatalogService]:
    """Build one allow-all catalog per session.

    Parameters
    ----------
    session_a
        Session A.
    session_b
        Session B.

    Returns
    -------
    tuple of CatalogService
        Catalog A and catalog B.
    """
    return (
        CatalogService(session_a, StorageResolver(session_a), AllowAllAuthorization()),
        CatalogService(session_b, StorageResolver(session_b), AllowAllAuthorization()),
    )


def test_an_incremental_append_conflicts_when_another_writer_appended_after_the_watermark(
    two_sessions: tuple[Session, Session, PrincipalRef, Path],
) -> None:
    """I1, probe 1: A reads the watermark, B appends, A must conflict — no duplicates."""
    session_a, session_b, principal, root = two_sessions
    catalog_a, catalog_b = _catalogs(session_a, session_b)
    _write(catalog_a, principal, "gold.t", WriteMode.CREATE, _ids([1, 2, 3]))
    watermark, pin = catalog_a.read_watermark(principal, _ref("gold.t"), "id")
    assert watermark is not None
    assert watermark.value == 3

    _write(catalog_b, principal, "gold.t", WriteMode.APPEND, _ids([4, 5]))
    before = _files(root)

    with pytest.raises(ConflictError, match=r"gold\.t changed during the write; retry"):
        _write(catalog_a, principal, "gold.t", WriteMode.APPEND, _ids([4, 5]), pin=pin)

    assert _ids_of(catalog_a, principal, "gold.t") == [1, 2, 3, 4, 5]
    assert _files(root) == before


def test_an_incremental_append_into_an_empty_table_conflicts_after_a_concurrent_first_write(
    two_sessions: tuple[Session, Session, PrincipalRef, Path],
) -> None:
    """Two first runs into an empty target must not both take every row."""
    session_a, session_b, principal, _ = two_sessions
    catalog_a, catalog_b = _catalogs(session_a, session_b)
    _write(catalog_a, principal, "gold.t", WriteMode.CREATE, _ids([]))
    watermark, pin = catalog_a.read_watermark(principal, _ref("gold.t"), "id")
    assert watermark is None
    assert pin.snapshot_id is None

    _write(catalog_b, principal, "gold.t", WriteMode.APPEND, _ids([1, 2]))

    with pytest.raises(ConflictError, match="changed during the write; retry"):
        _write(catalog_a, principal, "gold.t", WriteMode.APPEND, _ids([1, 2]), pin=pin)

    assert _ids_of(catalog_a, principal, "gold.t") == [1, 2]


def test_a_replace_pinned_to_what_it_read_conflicts_with_a_concurrent_append(
    two_sessions: tuple[Session, Session, PrincipalRef, Path],
) -> None:
    """A refresh over its own target must not delete rows it never read."""
    session_a, session_b, principal, _ = two_sessions
    catalog_a, catalog_b = _catalogs(session_a, session_b)
    snapshot = _write(catalog_a, principal, "gold.t", WriteMode.CREATE, _ids([1, 2, 3]))
    table_id = catalog_a.load_table(principal, _ref("gold.t")).uuid
    pin = SnapshotPin(table_id=table_id, snapshot_id=snapshot)

    _write(catalog_b, principal, "gold.t", WriteMode.APPEND, _ids([4]))

    with pytest.raises(ConflictError, match="changed during the write; retry"):
        _write(catalog_a, principal, "gold.t", WriteMode.REPLACE, _ids([2, 3]), pin=pin)

    assert _ids_of(catalog_a, principal, "gold.t") == [1, 2, 3, 4]


def test_a_pin_to_a_dropped_and_recreated_table_conflicts(
    two_sessions: tuple[Session, Session, PrincipalRef, Path],
) -> None:
    """A pin names a table, not just a snapshot: a re-created namesake is a change."""
    session_a, _, principal, _ = two_sessions
    catalog_a = CatalogService(session_a, StorageResolver(session_a), AllowAllAuthorization())
    snapshot = _write(catalog_a, principal, "gold.t", WriteMode.CREATE, _ids([1]))

    with pytest.raises(ConflictError, match="changed during the write; retry"):
        _write(
            catalog_a,
            principal,
            "gold.t",
            WriteMode.APPEND,
            _ids([2]),
            pin=SnapshotPin(table_id=uuid.uuid4(), snapshot_id=snapshot),
        )

    assert _ids_of(catalog_a, principal, "gold.t") == [1]


def test_a_pinned_write_that_still_matches_commits(
    two_sessions: tuple[Session, Session, PrincipalRef, Path],
) -> None:
    """With no concurrent writer the pin is satisfied and the append lands."""
    session_a, _, principal, _ = two_sessions
    catalog_a = CatalogService(session_a, StorageResolver(session_a), AllowAllAuthorization())
    _write(catalog_a, principal, "gold.t", WriteMode.CREATE, _ids([1]))
    _, pin = catalog_a.read_watermark(principal, _ref("gold.t"), "id")

    _write(catalog_a, principal, "gold.t", WriteMode.APPEND, _ids([2]), pin=pin)

    assert _ids_of(catalog_a, principal, "gold.t") == [1, 2]


def test_a_concurrent_plain_append_is_still_allowed(
    two_sessions: tuple[Session, Session, PrincipalRef, Path],
) -> None:
    """An unpinned append doesn't depend on the target's rows, so it still lands."""
    session_a, session_b, principal, _ = two_sessions
    catalog_a, catalog_b = _catalogs(session_a, session_b)
    _write(catalog_a, principal, "gold.t", WriteMode.CREATE, _ids([1]))
    catalog_a.load_table(principal, _ref("gold.t"))

    _write(catalog_b, principal, "gold.t", WriteMode.APPEND, _ids([2]))
    _write(catalog_a, principal, "gold.t", WriteMode.APPEND, _ids([3]))

    assert _ids_of(catalog_a, principal, "gold.t") == [1, 2, 3]


def test_a_target_that_moves_between_staging_and_commit_conflicts_and_leaves_no_files(
    two_sessions: tuple[Session, Session, PrincipalRef, Path],
) -> None:
    """The commit-time race: the message says what happened, and every file is removed."""
    session_a, session_b, principal, root = two_sessions
    catalog_a, catalog_b = _catalogs(session_a, session_b)
    _write(catalog_a, principal, "gold.t", WriteMode.CREATE, _ids([1]))
    real_stage = service_module.stage_snapshot
    before_a = _files(root)
    staged_by_a: set[str] = set()
    written_by_b: set[str] = set()

    def stage_then_race(*args: object, **kwargs: object) -> object:
        staged = real_stage(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        if staged_by_a:
            return staged  # B's own write, staged through the same patch
        before_b = _files(root)
        staged_by_a.update(before_b - before_a)
        _write(catalog_b, principal, "gold.t", WriteMode.APPEND, _ids([2]))
        written_by_b.update(_files(root) - before_b)
        return staged

    with (
        mock.patch.object(service_module, "stage_snapshot", side_effect=stage_then_race),
        pytest.raises(ConflictError, match=r"gold\.t changed during the write; retry"),
    ):
        _write(catalog_a, principal, "gold.t", WriteMode.APPEND, _ids([3]))

    assert _ids_of(catalog_a, principal, "gold.t") == [1, 2]
    assert staged_by_a
    assert _files(root) == before_a | written_by_b
