"""A write whose commit landed but whose acknowledgement was lost keeps its files.

The database applied the commit and only the way back failed: the connection dropped after the
`COMMIT` was applied, or code after `session.commit()` returned raised. The table's new snapshot
names the write's data files, so the clean-up that follows a failed write must not delete them.
These tests run on a SQLite file and look at the result through a second connection, as another
process would.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from collections.abc import Callable, Generator, Iterator
from pathlib import Path
from unittest import mock

import pyarrow as pa
import pytest
from pyiceberg.table import StaticTable
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from test_table_writes import _catalog, _files, _orders, _ref, _write

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog import service as service_module
from lakana_core.catalog.service import CatalogService
from lakana_core.catalog.writes import WriteResult
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.errors import ConflictError, WriteOutcomeUnknownError
from lakana_core.models import Base, Table, TableLocation, WriteMode
from lakana_core.runs.lockdown import ProtectedWriteGuard
from lakana_core.storage.resolver import StorageResolver


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    """Provide the writer's session on a SQLite file (overrides the in-memory one).

    Parameters
    ----------
    tmp_path
        Holds the database file and, under `store/`, the environment's storage.

    Yields
    ------
    Session
        A session whose schema has been created from the models.
    """
    engine = create_engine(DatabaseConfig(url=f"sqlite:///{tmp_path / 'catalog.db'}"))
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """Return the environment's storage root, apart from the database file.

    Parameters
    ----------
    tmp_path
        The test's directory.

    Returns
    -------
    Path
        The directory.
    """
    root = tmp_path / "store"
    root.mkdir()
    return root


@pytest.fixture
def other(session: Session) -> Iterator[Session]:
    """Provide a second connection to the same file: another process looking at the table.

    Parameters
    ----------
    session
        The writer's session; its engine is shared, its connection is not.

    Yields
    ------
    Session
        A session on its own connection.
    """
    with Session(session.get_bind()) as s:
        yield s


def _pointer(observer: Session, name: str) -> str | None:
    """Read a table's metadata pointer through the observer's connection.

    Parameters
    ----------
    observer
        The second connection.
    name
        The table's name.

    Returns
    -------
    str or None
        The stored `metadata_location`, or `None` when there is no such table.
    """
    observer.rollback()
    return observer.execute(
        select(Table.metadata_location).where(Table.name == name)
    ).scalar_one_or_none()


def _readable(observer: Session, principal: PrincipalRef, dotted: str) -> list[int]:
    """Read the table's current rows by the files its pointer names, through the observer.

    Parameters
    ----------
    observer
        The second connection.
    principal
        The acting principal.
    dotted
        The table.

    Returns
    -------
    list of int
        The `id` column, sorted.

    Raises
    ------
    FileNotFoundError
        If a file the current snapshot names is gone.
    """
    observer.rollback()
    catalog = CatalogService(observer, StorageResolver(observer), AllowAllAuthorization())
    location = catalog.load_table(principal, _ref(dotted)).metadata_location
    table = StaticTable.from_metadata(location)
    return sorted(table.scan().to_arrow().column("id").to_pylist())


def _claims(session: Session) -> bool:
    """Say whether the session is about to commit a create's claim of its location.

    Parameters
    ----------
    session
        The writer's session.

    Returns
    -------
    bool
        Whether a new `TableLocation` is pending.
    """
    return any(isinstance(item, TableLocation) for item in session.new)


def _failing_commit(session: Session, error: Exception) -> mock._patch[Callable[[], None]]:
    """Patch the session's `commit` to raise `error`, except for a create's claim.

    Parameters
    ----------
    session
        The writer's session.
    error
        What the commit raises.

    Returns
    -------
    unittest.mock._patch
        A patch to use as a context manager.
    """
    real = session.commit

    def commit() -> None:
        if not _claims(session):
            raise error
        real()

    return mock.patch.object(session, "commit", commit)


def _acknowledgement_lost(session: Session) -> mock._patch[Callable[[], None]]:
    """Patch the session's `commit` to apply the commit and then fail, once.

    Parameters
    ----------
    session
        The writer's session.

    Returns
    -------
    unittest.mock._patch
        A patch to use as a context manager.
    """
    real = session.commit
    calls: list[int] = []

    def commit() -> None:
        if _claims(session):
            real()  # a create's claim of its location, before any file: not the commit under test
            return
        real()
        if not calls:
            calls.append(1)
            raise OperationalError("COMMIT", None, ConnectionResetError("connection reset"))

    return mock.patch.object(session, "commit", commit)


@contextlib.contextmanager
def _code_after_commit_fails(session: Session) -> Generator[None]:
    """Make the first `LoadedTable` built after the session's commit returned raise.

    Parameters
    ----------
    session
        The writer's session.

    Yields
    ------
    None
        While the patches are in place.
    """
    real_commit, real_loaded = session.commit, service_module.LoadedTable
    committed: list[int] = []

    def commit() -> None:
        real_commit()
        committed.append(1)

    def loaded(*args: object, **kwargs: object) -> object:
        if committed:
            raise RuntimeError("after the commit")
        return real_loaded(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    with (
        mock.patch.object(session, "commit", commit),
        mock.patch.object(service_module, "LoadedTable", loaded),
    ):
        yield


_INJECTIONS = {
    "the connection fails after the commit was applied": _acknowledgement_lost,
    "code after the commit raises": _code_after_commit_fails,
}


def _setup(
    session: Session, principal: PrincipalRef, store: Path, mode: WriteMode
) -> tuple[CatalogService, str | None]:
    """Prepare the catalog and, for a write into an existing table, that table.

    Parameters
    ----------
    session
        The writer's session.
    principal
        The acting principal.
    store
        The storage root.
    mode
        The write mode under test.

    Returns
    -------
    tuple of (CatalogService, str or None)
        The catalog and the table's pointer before the write (`None` for a create).
    """
    catalog = _catalog(session, principal, store)
    if mode is WriteMode.CREATE:
        return catalog, None
    _write(catalog, principal, "gold.daily", WriteMode.CREATE, _orders([1], ["eu"]))
    row = session.execute(select(Table.metadata_location).where(Table.name == "daily")).scalar_one()
    session.commit()
    return catalog, row


@pytest.mark.parametrize("injection", list(_INJECTIONS))
@pytest.mark.parametrize("mode", [WriteMode.CREATE, WriteMode.APPEND, WriteMode.REPLACE])
def test_a_commit_that_landed_keeps_every_file_the_snapshot_names(
    session: Session,
    other: Session,
    principal: PrincipalRef,
    store: Path,
    mode: WriteMode,
    injection: str,
) -> None:
    """The pointer moved, so the snapshot's files stay and the write is reported as committed."""
    catalog, before = _setup(session, principal, store, mode)
    data = _orders([5, 6], ["us", "us"])
    outcome: WriteResult | None = None
    with _INJECTIONS[injection](session):
        try:
            outcome = _write(catalog, principal, "gold.daily", mode, data)
        except Exception:
            outcome = None

    assert _pointer(other, "daily") != before  # the database applied the commit
    expected = [5, 6] if mode is not WriteMode.APPEND else [1, 5, 6]
    assert _readable(other, principal, "gold.daily") == expected  # every file is still there
    assert outcome is not None, "the write was reported as failed although it was committed"
    assert outcome.rows == 2


@pytest.mark.parametrize("injection", list(_INJECTIONS))
def test_an_arrow_append_whose_commit_landed_keeps_its_files(
    session: Session,
    other: Session,
    principal: PrincipalRef,
    store: Path,
    injection: str,
) -> None:
    """`CatalogService.append`, the SDK's direct upload, has the same protection."""
    catalog, before = _setup(session, principal, store, WriteMode.APPEND)
    outcome = None
    with _INJECTIONS[injection](session):
        try:
            outcome = catalog.append(principal, _ref("gold.daily"), _orders([5, 6], ["us", "us"]))
        except Exception:
            outcome = None

    assert _pointer(other, "daily") != before
    assert _readable(other, principal, "gold.daily") == [1, 5, 6]
    assert outcome is not None, "the append was reported as failed although it was committed"
    assert outcome.rows == 2


@pytest.mark.parametrize("mode", [WriteMode.APPEND, WriteMode.REPLACE])
def test_a_commit_that_did_not_land_still_deletes_the_files_it_wrote(
    session: Session, other: Session, principal: PrincipalRef, store: Path, mode: WriteMode
) -> None:
    """The check says "not committed": the clean-up is what it always was."""
    catalog, before = _setup(session, principal, store, mode)
    files_before = _files(store)
    with (
        mock.patch.object(session, "commit", side_effect=RuntimeError("before the commit")),
        pytest.raises(RuntimeError, match="before the commit"),
    ):
        _write(catalog, principal, "gold.daily", mode, _orders([5, 6], ["us", "us"]))

    assert _pointer(other, "daily") == before
    assert _files(store) == files_before


def test_a_create_that_failed_inside_its_commit_keeps_its_files_and_says_unknown(
    session: Session, other: Session, principal: PrincipalRef, store: Path
) -> None:
    """Nothing shows a table that is still inside its commit: the location is not emptied."""
    catalog, _ = _setup(session, principal, store, WriteMode.CREATE)
    with (
        _failing_commit(session, RuntimeError("inside the commit")),
        pytest.raises(WriteOutcomeUnknownError) as raised,
    ):
        _write(catalog, principal, "gold.daily", WriteMode.CREATE, _orders([5, 6], ["us", "us"]))

    assert _pointer(other, "daily") is None
    assert any(path.startswith("managed/") for path in _files(store))
    message = str(raised.value)
    assert "may or may not" in message
    assert "proven that the commit did not land" in message  # the location sweep decides
    assert not session.in_transaction()


def test_a_create_that_failed_before_its_commit_was_sent_empties_its_location(
    session: Session, other: Session, principal: PrincipalRef, store: Path
) -> None:
    """Deleting stays for failures raised before the COMMIT."""
    catalog, _ = _setup(session, principal, store, WriteMode.CREATE)
    with (
        mock.patch.object(
            ProtectedWriteGuard, "record_pending", side_effect=RuntimeError("before the commit")
        ),
        pytest.raises(RuntimeError, match="before the commit"),
    ):
        _write(catalog, principal, "gold.daily", WriteMode.CREATE, _orders([5, 6], ["us", "us"]))

    assert _pointer(other, "daily") is None
    assert [path for path in _files(store) if path.startswith("managed/")] == []


def test_the_check_reads_the_pointer_with_a_locking_read_on_postgres(
    session: Session, principal: PrincipalRef, store: Path
) -> None:
    """A plain read cannot see a backend still inside its commit; `FOR SHARE` waits for it."""
    catalog, _ = _setup(session, principal, store, WriteMode.APPEND)
    seen: list[str] = []
    real = session.execute

    def spy(statement: object, *args: object, **kwargs: object) -> object:
        compiled = getattr(statement, "compile", None)
        if compiled is not None:
            seen.append(str(compiled(dialect=postgresql.dialect())))
        return real(statement, *args, **kwargs)  # ty: ignore[no-matching-overload]

    with _acknowledgement_lost(session), mock.patch.object(session, "execute", spy):
        _write(catalog, principal, "gold.daily", WriteMode.APPEND, _orders([5], ["us"]))

    reads = [sql for sql in seen if "SELECT tables.metadata_location" in sql and "FOR SHARE" in sql]
    assert len(reads) == 1, seen
    assert "WHERE tables.id" in reads[0]


def test_the_check_leaves_no_transaction_open(
    session: Session, principal: PrincipalRef, store: Path
) -> None:
    """Landed or not, the session is not left inside a transaction by the check."""
    catalog, _ = _setup(session, principal, store, WriteMode.APPEND)
    with _acknowledgement_lost(session):
        _write(catalog, principal, "gold.daily", WriteMode.APPEND, _orders([5], ["us"]))
    assert not session.in_transaction()


def test_another_writers_commit_does_not_make_my_failed_write_landed(
    session: Session, other: Session, principal: PrincipalRef, store: Path
) -> None:
    """My commit fails before it applies; theirs lands meanwhile: mine is deleted, theirs stays."""
    catalog, before = _setup(session, principal, store, WriteMode.APPEND)
    theirs = CatalogService(other, StorageResolver(other), AllowAllAuthorization())

    def lose_to_another_writer() -> None:
        session.rollback()  # my transaction never applies ...
        theirs.append(principal, _ref("gold.daily"), _orders([7], ["xx"]))  # ... theirs does
        raise OperationalError("COMMIT", None, ConnectionResetError("connection reset"))

    with (
        mock.patch.object(session, "commit", lose_to_another_writer),
        pytest.raises(OperationalError),
    ):
        _write(catalog, principal, "gold.daily", WriteMode.APPEND, _orders([5, 6], ["us", "us"]))

    assert _pointer(other, "daily") != before
    assert _readable(other, principal, "gold.daily") == [1, 7]  # theirs is readable
    assert len(list(store.rglob("*.parquet"))) == 2  # the created one and theirs; mine is gone


def test_an_outcome_that_cannot_be_checked_keeps_the_files_and_says_so(
    session: Session,
    other: Session,
    principal: PrincipalRef,
    store: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The database cannot be asked after the failure: nothing is deleted, the error says why."""
    catalog, before = _setup(session, principal, store, WriteMode.APPEND)
    files_before = _files(store)
    real = session.execute
    asked: list[object] = []

    def execute(statement: object, *args: object, **kwargs: object) -> object:
        if asked:
            raise OperationalError("SELECT", None, ConnectionResetError("database is gone"))
        return real(statement, *args, **kwargs)  # ty: ignore[no-matching-overload]

    def lose_acknowledgement() -> None:
        asked.append(1)
        raise OperationalError("COMMIT", None, ConnectionResetError("connection reset"))

    caplog.set_level(logging.WARNING)
    with (
        mock.patch.object(session, "execute", execute),
        mock.patch.object(session, "commit", lose_acknowledgement),
        pytest.raises(WriteOutcomeUnknownError) as raised,
    ):
        _write(catalog, principal, "gold.daily", WriteMode.APPEND, _orders([5, 6], ["us", "us"]))

    message = str(raised.value)
    assert "may or may not" in message
    assert "lakana query status" in message
    assert "lakana table maintain" in message
    assert "database is gone" not in message  # the driver's text is not shown to a client
    assert _pointer(other, "daily") == before  # this commit did not land ...
    assert _files(store) > files_before  # ... and the files are kept anyway: an orphan is harmless


def test_the_check_reads_the_database_again_not_the_failed_sessions_picture(
    session: Session, other: Session, principal: PrincipalRef, store: Path
) -> None:
    """The writer's identity map still holds the old pointer; the check must not use it."""
    catalog, before = _setup(session, principal, store, WriteMode.APPEND)
    row = session.execute(select(Table).where(Table.name == "daily")).scalar_one()
    assert row.metadata_location == before
    with _acknowledgement_lost(session):
        result = _write(catalog, principal, "gold.daily", WriteMode.APPEND, _orders([5], ["us"]))

    assert result.rows == 1
    assert _readable(other, principal, "gold.daily") == [1, 5]


def test_a_lakana_error_raised_before_the_commit_is_not_checked_and_cleans_up(
    session: Session, principal: PrincipalRef, store: Path
) -> None:
    """A domain refusal comes before any commit: no extra read, files deleted, same error."""
    catalog, _ = _setup(session, principal, store, WriteMode.APPEND)
    files_before = _files(store)
    with (
        mock.patch.object(CatalogService, "_commit_landed", side_effect=AssertionError("asked")),
        mock.patch.object(
            service_module, "commit_metadata_pointer", side_effect=ConflictError("x")
        ),
        pytest.raises(ConflictError),
    ):
        _write(catalog, principal, "gold.daily", WriteMode.APPEND, _orders([5], ["us"]))

    assert _files(store) == files_before


def test_an_empty_append_that_fails_deletes_nothing_it_did_not_write(
    session: Session, other: Session, principal: PrincipalRef, store: Path
) -> None:
    """An append of no rows has no snapshot to look for: no check is made, nothing is deleted."""
    catalog, before = _setup(session, principal, store, WriteMode.APPEND)
    files_before = _files(store)
    with (
        mock.patch.object(CatalogService, "_commit_landed", side_effect=AssertionError("asked")),
        mock.patch.object(session, "commit", side_effect=RuntimeError("before the commit")),
        pytest.raises(RuntimeError),
    ):
        _write(
            catalog,
            principal,
            "gold.daily",
            WriteMode.APPEND,
            pa.table({"id": pa.array([], pa.int64()), "region": pa.array([], pa.string())}),
        )

    assert _pointer(other, "daily") == before
    assert _files(store) == files_before


def test_a_table_that_is_gone_is_not_landed(
    session: Session, principal: PrincipalRef, store: Path
) -> None:
    """A row that no longer exists names no snapshot of this write."""
    catalog, _ = _setup(session, principal, store, WriteMode.APPEND)
    assert not catalog._commit_landed(
        _ref("gold.daily"), table_id=uuid.uuid4(), snapshot_id=1, new_table=False
    )
    assert not session.in_transaction()


def test_on_postgres_the_check_sets_a_lock_timeout_first(
    session: Session, principal: PrincipalRef, store: Path
) -> None:
    """`SET LOCAL lock_timeout` is the first statement of the check, on Postgres only."""
    catalog, _ = _setup(session, principal, store, WriteMode.APPEND)
    real = session.execute
    sent: list[str] = []

    def execute(statement: object, *args: object, **kwargs: object) -> object:
        if str(statement).startswith("SET LOCAL"):
            sent.append(str(statement))
            return None
        return real(statement, *args, **kwargs)  # ty: ignore[no-matching-overload]

    with (
        mock.patch.object(session.get_bind().dialect, "name", "postgresql"),
        mock.patch.object(session, "execute", execute),
    ):
        catalog._commit_landed(
            _ref("gold.daily"), table_id=uuid.uuid4(), snapshot_id=1, new_table=False
        )
    assert sent == [f"SET LOCAL lock_timeout = '{service_module.LOCK_TIMEOUT_SECONDS}s'"]
