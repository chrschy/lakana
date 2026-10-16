"""Reading a table at the snapshot a check result names.

`CatalogService.load_table_at_check` decides such a read and `list_data_files` lists the files
of the named snapshot. The rows themselves are read by the query path, in the worker's tests.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pytest
from core_test_helpers import ActionAllowList, python_task_run
from pyiceberg.io import FileIO
from pyiceberg.manifest import DataFileContent
from pyiceberg.table.metadata import TableMetadata
from pyiceberg.table.snapshots import Snapshot
from pyiceberg.table.update import RemoveSnapshotRefUpdate
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    PrincipalRef,
    RunContext,
    Securable,
    TaskScope,
)
from lakana_core.catalog import service as service_module
from lakana_core.catalog.datafiles import SNAPSHOT_NOT_IN_METADATA, list_data_files
from lakana_core.catalog.json_schema import ColumnSpec
from lakana_core.catalog.metadata import read_metadata
from lakana_core.catalog.service import (
    FAILED_SNAPSHOT_KEEP_DAYS,
    CatalogService,
    LoadedAt,
    LoadedTable,
    failed_snapshot_tag,
)
from lakana_core.config import StorageConfig
from lakana_core.errors import (
    CheckedSnapshotGoneError,
    CheckResultGoneError,
    LakanaError,
    NoCheckedSnapshotError,
    NotPublishedError,
    PermissionDenied,
    UnsupportedTableError,
    ValidationError,
)
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    CheckOutcome,
    CheckResult,
    RollbackOutcome,
    Table,
    TableType,
    WriteMode,
)
from lakana_core.projects.checks import CheckSpec
from lakana_core.runs.checks import (
    CHECK_ROWS_NOT_FOR_A_TASK,
    check_result_gone_message,
    checked_snapshot_gone_message,
    no_checked_snapshot_message,
    no_snapshot_could_not_run_message,
    no_snapshot_not_read_message,
    no_snapshot_of_a_view_message,
    record_result,
)
from lakana_core.runs.tasks import Written
from lakana_core.storage.resolver import StorageResolver

_TABLE = "gold.orders"
_REF = TableRef.from_dotted("dev", _TABLE)
_RUN = uuid.UUID("6f1c2a52-3b8e-4d0a-9a55-0c1f5f1f7a10")


def _gone(run_id: uuid.UUID, table: str = _TABLE) -> str:
    """Write out the text of a checked snapshot that is gone, for a table in ``dev``.

    Parameters
    ----------
    run_id
        The run of the check result.
    table
        The table's dotted name.

    Returns
    -------
    str
        The whole text a user reads.
    """
    return (
        "The rows of this check can no longer be read: the snapshot it checked is no longer in "
        f"`{table}`. Maintenance removes a failed snapshot after 7 days, and a table that is "
        "dropped and built again starts anew. The number of rows the check found remains: "
        f"`lakana check list --run {run_id} --env dev`."
    )


def _not_this_tables(table: str = _TABLE) -> str:
    """Write out the one text for a check result that is not bound to the named table.

    Parameters
    ----------
    table
        The table's dotted name, as the caller addressed it.

    Returns
    -------
    str
        The text; it names the table the caller named and nothing of the result.
    """
    return (
        f"The rows of this check cannot be read from `{table}`: the check result is not one of "
        "this table as it exists now, or was removed with its run."
    )


def _anywhere(path: str) -> bool:
    """Accept every path: a path check for a test that is not about confinement.

    Parameters
    ----------
    path
        The path asked about.

    Returns
    -------
    bool
        Always `True`.
    """
    return True


def _catalog(
    session: Session,
    principal: PrincipalRef,
    tmp_path: Path,
) -> CatalogService:
    """Create `dev` at `tmp_path` and return an allow-all catalog over it.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    tmp_path
        The environment's storage root.

    Returns
    -------
    CatalogService
        A catalog over the prepared environment.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    return catalog


def _under(session: Session, authz: AllowAllAuthorization | ActionAllowList) -> CatalogService:
    """Return a catalog over the same database under another authorization.

    Parameters
    ----------
    session
        Database session.
    authz
        What decides.

    Returns
    -------
    CatalogService
        The catalog.
    """
    return CatalogService(session, StorageResolver(session), authz)


def _orders(ids: list[int], **more: list[str]) -> pa.Table:
    """Build rows of orders: an `id` column and any string columns.

    Parameters
    ----------
    ids
        The `id` values.
    **more
        Further string columns by name.

    Returns
    -------
    pa.Table
        The rows, every column nullable as DuckDB would give them.
    """
    return pa.table(
        {"id": pa.array(ids, pa.int64())}
        | {name: pa.array(values, pa.string()) for name, values in more.items()}
    )


def _write(
    catalog: CatalogService,
    principal: PrincipalRef,
    rows: pa.Table,
    mode: WriteMode,
    *,
    ref: TableRef = _REF,
    evolve_schema: bool = False,
) -> int:
    """Write rows into a table and return the snapshot the write made.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.
    rows
        The rows.
    mode
        The write mode.
    ref
        The target.
    evolve_schema
        Passed through.

    Returns
    -------
    int
        The id of the new snapshot.
    """
    result = catalog.write_batches(
        principal,
        ref,
        mode=mode,
        schema=rows.schema,
        batches=rows.to_batches(),
        evolve_schema=evolve_schema,
    )
    assert result.snapshot_id is not None
    return result.snapshot_id


def _row(session: Session, name: str = "orders") -> Table:
    """Read a table row afresh.

    Parameters
    ----------
    session
        Database session.
    name
        The table's name.

    Returns
    -------
    Table
        The row, never a cached one.
    """
    session.rollback()
    session.expire_all()
    return session.scalars(select(Table).where(Table.name == name)).one()


def _checked(
    session: Session,
    principal: PrincipalRef,
    *,
    table_id: uuid.UUID | None,
    snapshot_id: int | None,
    table: str = _TABLE,
    environment: str = "dev",
    outcome: CheckOutcome = CheckOutcome.FAILED,
) -> CheckResult:
    """Record a check result as the worker does, for a new run's task of the table.

    Parameters
    ----------
    session
        Database session; commits.
    principal
        The run's principal.
    table_id
        The id of the table the check read, as the worker resolved it.
    snapshot_id
        The snapshot the check read.
    table
        The checked table's dotted name.
    environment
        The run's environment.
    outcome
        How the check ended.

    Returns
    -------
    CheckResult
        The stored result of the check ``unique_id``.
    """
    task = python_task_run(session, principal.id, asset=table, kind="sql", environment=environment)
    task.has_checks = True
    session.commit()
    recorded = record_result(
        session,
        task_run_id=task.id,
        worker="w1",
        claim=task.claim_count,
        spec=CheckSpec(
            name=f"{table}/unique_id",
            table=table,
            check="unique_id",
            path=f"checks/{table.replace('.', '/')}/unique_id.sql",
            on_failure="block",
            description=None,
            sql="SELECT id FROM t GROUP BY id HAVING count(*) > 1",
        ),
        outcome=outcome,
        failing_rows=None if outcome is CheckOutcome.ERRORED else 1,
        error="the check's query failed" if outcome is CheckOutcome.ERRORED else None,
        table_id=table_id,
        checked_snapshot_id=snapshot_id,
        policy_applied=False,
        query_run_id=None,
        started_at=datetime(2026, 10, 12, 9, 0, tzinfo=UTC),
    )
    assert recorded is True
    return session.scalars(select(CheckResult).where(CheckResult.task_run_id == task.id)).one()


def _fail_the_check(
    catalog: CatalogService,
    session: Session,
    principal: PrincipalRef,
    *,
    before: int | None,
    after: int,
) -> CheckResult:
    """Record a failed blocking check of ``gold.orders`` and roll the table back.

    Parameters
    ----------
    catalog
        The catalog.
    session
        Database session; commits.
    principal
        The run's principal.
    before
        The snapshot before the task's write; `None` for a first build.
    after
        The snapshot the write made, the one the check read.

    Returns
    -------
    CheckResult
        The result; the tag of the failed snapshot is named after its run.
    """
    table_id = _row(session).id
    result = _checked(session, principal, table_id=table_id, snapshot_id=after)
    rolled = catalog.roll_back(
        principal,
        _REF,
        run_id=result.run_id,
        task_run_id=result.task_run_id,
        written=Written(table_id, before, after, publishes=before is None),
        failed_checks=[result.check_name],
    )
    assert rolled.outcome in (RollbackOutcome.ROLLED_BACK, RollbackOutcome.UNPUBLISHED)
    return session.scalars(select(CheckResult).where(CheckResult.id == result.id)).one()


def _keep(session: Session, table_id: uuid.UUID) -> None:
    """Put the note of a failed `keep` check on a table.

    Parameters
    ----------
    session
        Database session; commits.
    table_id
        The table.
    """
    session.execute(
        update(Table)
        .where(Table.id == table_id)
        .values(
            kept_run_id=uuid.uuid4(),
            kept_task_run_id=uuid.uuid4(),
            kept_check="gold.orders/unique_id",
        )
    )
    session.commit()


def _file_io(session: Session) -> FileIO:
    """Return the file IO of `dev`.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    FileIO
        What reads the environment's storage.
    """
    return StorageResolver(session).for_environment("dev").file_io


# --- the listing: the files of a named snapshot ---


def test_the_files_of_a_named_snapshot_are_listed(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Two appends: the first snapshot has one file, the current one both."""
    catalog = _catalog(session, principal, tmp_path)
    first = _write(catalog, principal, _orders([1]), WriteMode.CREATE)
    second = _write(catalog, principal, _orders([2]), WriteMode.APPEND)
    metadata = catalog.load_table(principal, _REF).metadata
    io = _file_io(session)

    current = list_data_files(metadata, io)
    at_first = list_data_files(metadata, io, snapshot_id=first)

    assert len(current) == 2
    assert len(at_first) == 1
    assert set(at_first) < set(current)
    assert sorted(list_data_files(metadata, io, snapshot_id=second)) == sorted(current)
    assert list_data_files(metadata, io, snapshot_id=None) == current


def test_a_snapshot_named_only_by_a_tag_is_listed(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """After a failed first build no branch is left; the tag's snapshot is still listed."""
    catalog = _catalog(session, principal, tmp_path)
    failed = _write(catalog, principal, _orders([1, 1]), WriteMode.CREATE)
    result = _fail_the_check(catalog, session, principal, before=None, after=failed)
    location = _row(session).metadata_location
    assert location is not None
    io = _file_io(session)
    metadata = read_metadata(io, location)
    assert metadata.current_snapshot_id is None
    assert metadata.refs[failed_snapshot_tag(result.run_id)].snapshot_id == failed

    assert list_data_files(metadata, io) == []
    listed = list_data_files(metadata, io, snapshot_id=failed)

    assert len(listed) == 1
    assert listed[0].endswith(".parquet")


def test_a_snapshot_that_is_not_there_is_an_error_not_a_stop_iteration(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A snapshot that maintenance removed is refused with a message, with or without a branch."""
    catalog = _catalog(session, principal, tmp_path)
    _write(catalog, principal, _orders([1]), WriteMode.CREATE)
    metadata = catalog.load_table(principal, _REF).metadata
    catalog.create_table(
        principal,
        TableRef.from_dotted("dev", "gold.empty"),
        schema=metadata.schema(),
    )
    empty = catalog.load_table(principal, TableRef.from_dotted("dev", "gold.empty")).metadata

    for held in (metadata, empty):
        with pytest.raises(CheckedSnapshotGoneError) as gone:
            list_data_files(held, _file_io(session), snapshot_id=1)
        assert str(gone.value) == SNAPSHOT_NOT_IN_METADATA
    assert SNAPSHOT_NOT_IN_METADATA == (
        "The snapshot to read is no longer in the table's metadata."
    )


def test_the_manifest_list_of_a_named_snapshot_is_confined_like_the_current_one(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Naming a snapshot is no way around the check of where its manifests lie."""
    catalog = _catalog(session, principal, tmp_path)
    first = _write(catalog, principal, _orders([1]), WriteMode.CREATE)
    _write(catalog, principal, _orders([2]), WriteMode.APPEND)
    metadata = catalog.load_table(principal, _REF).metadata
    asked: list[str] = []

    def nowhere(path: str) -> bool:
        """Record the path and refuse it.

        Parameters
        ----------
        path
            The path asked about.

        Returns
        -------
        bool
            Always `False`.
        """
        asked.append(path)
        return False

    with pytest.raises(ValidationError) as named:
        list_data_files(metadata, _file_io(session), is_valid_path=nowhere, snapshot_id=first)

    snapshot = metadata.snapshot_by_id(first)
    assert snapshot is not None
    assert asked == [snapshot.manifest_list]
    assert str(named.value) == (
        "The manifest list of the snapshot to read lies outside the table's location."
    )
    with pytest.raises(ValidationError) as current:
        list_data_files(metadata, _file_io(session), is_valid_path=nowhere)
    assert str(current.value) == (
        "A table's current snapshot manifest list lies outside its location."
    )


def test_a_manifest_of_a_named_snapshot_is_confined_and_named_as_such(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The refusal of a manifest says which snapshot it belongs to, and nothing false."""
    catalog = _catalog(session, principal, tmp_path)
    first = _write(catalog, principal, _orders([1]), WriteMode.CREATE)
    metadata = catalog.load_table(principal, _REF).metadata
    snapshot = metadata.snapshot_by_id(first)
    assert snapshot is not None

    def only_the_list(path: str) -> bool:
        """Accept the manifest list alone.

        Parameters
        ----------
        path
            The path asked about.

        Returns
        -------
        bool
            Whether it is the snapshot's manifest list.
        """
        return path == snapshot.manifest_list

    with pytest.raises(ValidationError) as named:
        list_data_files(metadata, _file_io(session), is_valid_path=only_the_list, snapshot_id=first)
    assert str(named.value) == (
        "A manifest of the snapshot to read lies outside the table's location."
    )
    with pytest.raises(ValidationError) as current:
        list_data_files(metadata, _file_io(session), is_valid_path=only_the_list)
    assert str(current.value) == "A table's manifest lies outside its location."


def test_a_delete_file_of_a_named_snapshot_is_refused_and_named_as_such(
    session: Session, principal: PrincipalRef, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal of row-level deletes covers a named snapshot and does not call it current.

    The manifest is a stand-in: only the content of its one entry is read. A real delete file
    committed through the catalog is refused in the worker's tests.
    """
    catalog = _catalog(session, principal, tmp_path)
    first = _write(catalog, principal, _orders([1]), WriteMode.CREATE)
    metadata = catalog.load_table(principal, _REF).metadata
    entry = SimpleNamespace(data_file=SimpleNamespace(content=DataFileContent.POSITION_DELETES))
    manifest = SimpleNamespace(
        manifest_path="unused", fetch_manifest_entry=lambda io, discard_deleted: [entry]
    )
    monkeypatch.setattr(Snapshot, "manifests", lambda self, io: [manifest])

    with pytest.raises(UnsupportedTableError) as named:
        list_data_files(metadata, _file_io(session), snapshot_id=first)
    assert str(named.value) == (
        "The snapshot to read has a live row-level delete file; Lakana's query engine does not "
        "apply deletes yet."
    )
    with pytest.raises(UnsupportedTableError) as current:
        list_data_files(metadata, _file_io(session))
    assert str(current.value) == (
        "A table's current snapshot has a live row-level delete file; Lakana's query engine "
        "does not apply deletes yet."
    )


# --- the read: one snapshot, named by a stored check result ---


def test_a_published_table_is_read_at_the_snapshot_of_the_result(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """What is returned names the snapshot and its columns, and is not a loaded table."""
    catalog = _catalog(session, principal, tmp_path)
    first = _write(catalog, principal, _orders([1], region=["eu"]), WriteMode.CREATE)
    _write(catalog, principal, _orders([2], region=["us"]), WriteMode.APPEND)
    plain = catalog.load_table(principal, _REF)
    result = _checked(
        session, principal, table_id=plain.uuid, snapshot_id=first, outcome=CheckOutcome.WARNED
    )

    loaded = catalog.load_table_at_check(principal, _REF, check_result_id=result.id)

    assert isinstance(loaded, LoadedAt)
    assert not isinstance(loaded, LoadedTable)
    assert not issubclass(LoadedAt, LoadedTable)
    assert (loaded.ref, loaded.snapshot_id) == (_REF, first)
    assert [f.name for f in dataclasses.fields(LoadedAt)] == [
        "ref",
        "metadata",
        "snapshot_id",
        "columns",
    ]
    assert loaded.metadata == plain.metadata
    assert loaded.metadata.current_snapshot_id != first
    assert [(c.name, str(c.field_type)) for c in loaded.columns] == [
        ("id", "long"),
        ("region", "string"),
    ]
    io = _file_io(session)
    listed = loaded.data_files(io, is_valid_path=_anywhere)
    assert listed == list_data_files(plain.metadata, io, snapshot_id=first)
    assert len(listed) == 1
    assert len(list_data_files(plain.metadata, io)) == 2


def test_the_files_of_the_read_are_confined_by_the_callers_path_check(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """`data_files` hands the path check on to the listing."""
    catalog = _catalog(session, principal, tmp_path)
    first = _write(catalog, principal, _orders([1]), WriteMode.CREATE)
    result = _checked(session, principal, table_id=_row(session).id, snapshot_id=first)
    loaded = catalog.load_table_at_check(principal, _REF, check_result_id=result.id)

    with pytest.raises(ValidationError):
        loaded.data_files(_file_io(session), is_valid_path=lambda path: False)
    with pytest.raises(TypeError, match="is_valid_path"):  # confinement is not optional
        loaded.data_files(_file_io(session))  # ty: ignore[missing-argument]


def test_the_columns_are_those_of_the_snapshot(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A write that added a column and was rolled back: the failed snapshot has the column."""
    catalog = _catalog(session, principal, tmp_path)
    before = _write(catalog, principal, _orders([1]), WriteMode.CREATE)
    passed = _checked(
        session,
        principal,
        table_id=_row(session).id,
        snapshot_id=before,
        outcome=CheckOutcome.PASSED,
    )
    failed = _write(
        catalog,
        principal,
        _orders([2], email=["a@example.com"]),
        WriteMode.APPEND,
        evolve_schema=True,
    )
    result = _fail_the_check(catalog, session, principal, before=before, after=failed)
    current = catalog.load_table(principal, _REF).metadata
    assert [f.name for f in current.schema().fields] == ["id"]
    assert current.current_snapshot_id == before

    loaded = catalog.load_table_at_check(principal, _REF, check_result_id=result.id)

    assert [c.name for c in loaded.columns] == ["id", "email"]
    assert loaded.snapshot_id == failed
    earlier = catalog.load_table_at_check(principal, _REF, check_result_id=passed.id)
    assert [c.name for c in earlier.columns] == ["id"]


@pytest.mark.parametrize("schema_id", [None, 99])
def test_a_snapshot_whose_schema_is_not_known_is_gone(
    session: Session,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schema_id: int | None,
) -> None:
    """No guessing of columns: a snapshot that names no schema the table holds is not read."""
    catalog = _catalog(session, principal, tmp_path)
    snapshot = _write(catalog, principal, _orders([1]), WriteMode.CREATE)
    result = _checked(session, principal, table_id=_row(session).id, snapshot_id=snapshot)
    assert catalog.load_table_at_check(principal, _REF, check_result_id=result.id).columns

    def without_the_schema(file_io: FileIO, location: str) -> TableMetadata:
        """Read the metadata and take the schema id of every snapshot away.

        Parameters
        ----------
        file_io
            What reads the file.
        location
            The metadata file.

        Returns
        -------
        TableMetadata
            The metadata, its snapshots naming `schema_id`.
        """
        real = read_metadata(file_io, location)
        return real.model_copy(
            update={
                "snapshots": [s.model_copy(update={"schema_id": schema_id}) for s in real.snapshots]
            }
        )

    monkeypatch.setattr(service_module, "read_metadata", without_the_schema)

    with pytest.raises(CheckedSnapshotGoneError) as gone:
        catalog.load_table_at_check(principal, _REF, check_result_id=result.id)

    assert str(gone.value) == _gone(result.run_id)


def test_only_the_checked_snapshot_of_a_table_that_is_not_published_is_readable(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A first build that failed a blocking check: no reader loads it, the named read does."""
    catalog = _catalog(session, principal, tmp_path)
    failed = _write(catalog, principal, _orders([1, 1]), WriteMode.CREATE)
    result = _fail_the_check(catalog, session, principal, before=None, after=failed)
    row = _row(session)
    assert row.unpublished_run_id == result.run_id
    with pytest.raises(NotPublishedError):
        catalog.load_table(principal, _REF)

    loaded = catalog.load_table_at_check(principal, _REF, check_result_id=result.id)

    assert loaded.snapshot_id == failed
    assert loaded.metadata.current_snapshot_id is None
    assert [c.name for c in loaded.columns] == ["id"]
    assert len(loaded.data_files(_file_io(session), is_valid_path=_anywhere)) == 1
    with pytest.raises(NotPublishedError):  # the named read publishes nothing
        catalog.load_table(principal, _REF)
    assert _row(session).unpublished_run_id == result.run_id


def test_the_read_grant_is_checked_first(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Without `READ_TABLE` every question gets the same refusal, so nothing is learned."""
    catalog = _catalog(session, principal, tmp_path)
    snapshot = _write(catalog, principal, _orders([1]), WriteMode.CREATE)
    table_id = _row(session).id
    real = _checked(session, principal, table_id=table_id, snapshot_id=snapshot)
    stale = _checked(session, principal, table_id=table_id, snapshot_id=1)
    rebuilt = _checked(session, principal, table_id=uuid.uuid4(), snapshot_id=snapshot)
    unchecked = _checked(session, principal, table_id=table_id, snapshot_id=None)
    authz = ActionAllowList()
    denied = _under(session, authz)
    absent = TableRef.from_dotted("dev", "gold.absent")
    as_task = PrincipalRef(
        id=principal.id,
        name=principal.name,
        type=principal.type,
        run_context=RunContext(task_run_id=real.task_run_id, claim=1),
    )
    questions = [
        (principal, _REF, real.id),  # all true
        (principal, _REF, stale.id),  # a snapshot that is gone
        (principal, _REF, rebuilt.id),  # a table built again
        (principal, _REF, unchecked.id),  # a result without a snapshot
        (principal, _REF, uuid.uuid4()),  # no such result
        (as_task, _REF, real.id),  # a task
        (principal, absent, real.id),  # a name nothing has
    ]

    texts = set()
    for who, ref, asked in questions:
        with pytest.raises(PermissionDenied) as refused:
            denied.load_table_at_check(who, ref, check_result_id=asked)
        texts.add(str(refused.value))

    assert texts == {"may not READ_TABLE"}
    gold = NamespacePath.from_parts(["gold"])
    assert authz.seen == [(Action.READ_TABLE, Securable.table_scope("dev", gold, "orders"))] * 6 + [
        (Action.READ_TABLE, Securable.table_scope("dev", gold, "absent"))
    ]


def test_a_denied_caller_learns_neither_that_a_table_is_not_published_nor_that_it_is_kept(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The two states of a table answer a caller without the grant exactly as any table does."""
    catalog = _catalog(session, principal, tmp_path)
    failed = _write(catalog, principal, _orders([1, 1]), WriteMode.CREATE)
    unpublished = _fail_the_check(catalog, session, principal, before=None, after=failed)
    assert _row(session).unpublished_run_id is not None
    other = TableRef.from_dotted("dev", "gold.kept")
    kept_snapshot = _write(catalog, principal, _orders([2]), WriteMode.CREATE, ref=other)
    kept_id = _row(session, "kept").id
    _keep(session, kept_id)
    kept = _checked(
        session, principal, table_id=kept_id, snapshot_id=kept_snapshot, table="gold.kept"
    )
    denied = _under(session, ActionAllowList())

    texts = set()
    for ref, asked in ((_REF, unpublished.id), (other, kept.id)):
        with pytest.raises(PermissionDenied) as refused:
            denied.load_table_at_check(principal, ref, check_result_id=asked)
        texts.add(str(refused.value))

    assert texts == {"may not READ_TABLE"}
    assert catalog.load_table_at_check(principal, other, check_result_id=kept.id).snapshot_id == (
        kept_snapshot
    )  # a person with the grant reads the kept table


def test_a_result_that_is_gone_is_an_error_of_its_own(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A result removed with its run: the read says so, for a table that is there or not."""
    catalog = _catalog(session, principal, tmp_path)
    _write(catalog, principal, _orders([1]), WriteMode.CREATE)

    for dotted in (_TABLE, "gold.absent", "x.y"):
        with pytest.raises(CheckResultGoneError) as gone:
            catalog.load_table_at_check(
                principal, TableRef.from_dotted("dev", dotted), check_result_id=uuid.uuid4()
            )
        assert str(gone.value) == _not_this_tables(dotted)

    assert check_result_gone_message("gold.orders") == _not_this_tables()
    assert "`gold.or\\nders`" in check_result_gone_message("gold.or\nders")
    assert issubclass(CheckResultGoneError, LakanaError)


def test_a_result_of_another_table_reads_nothing_of_either(
    session: Session, principal: PrincipalRef, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller who may read one table passes the result of another: nothing of it is told."""
    catalog = _catalog(session, principal, tmp_path)
    secret = _write(catalog, principal, _orders([1]), WriteMode.CREATE)
    other = TableRef.from_dotted("dev", "gold.other")
    _write(catalog, principal, _orders([2]), WriteMode.CREATE, ref=other)
    of_the_secret = _checked(session, principal, table_id=_row(session).id, snapshot_id=secret)
    read: list[str] = []
    monkeypatch.setattr(
        service_module, "read_metadata", lambda file_io, location: read.append(location)
    )

    with pytest.raises(CheckResultGoneError) as gone:
        catalog.load_table_at_check(principal, other, check_result_id=of_the_secret.id)

    assert str(gone.value) == _not_this_tables("gold.other")
    assert str(of_the_secret.run_id) not in str(gone.value)
    assert "orders" not in str(gone.value)
    assert read == []


def test_every_result_that_is_not_this_tables_gets_one_answer(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Missing, of another table, without a table, of the table before it was built again.

    One type and one text, byte for byte, with or without a checked snapshot: a caller who
    holds the id of a result of a table it may not read cannot tell these apart, and learns
    neither the run nor the check.
    """
    catalog = _catalog(session, principal, tmp_path)
    old_snapshot = _write(catalog, principal, _orders([1]), WriteMode.CREATE)
    old_id = _row(session).id
    before_the_rebuild = _checked(session, principal, table_id=old_id, snapshot_id=old_snapshot)
    catalog.drop_table(principal, _REF)
    _write(catalog, principal, _orders([7]), WriteMode.CREATE)
    assert _row(session).id != old_id
    secret_ref = TableRef.from_dotted("dev", "gold.secret")
    secret = _write(catalog, principal, _orders([2]), WriteMode.CREATE, ref=secret_ref)
    secret_id = _row(session, "secret").id
    foreign = [
        _checked(session, principal, table_id=secret_id, snapshot_id=secret, table="gold.secret"),
        _checked(session, principal, table_id=secret_id, snapshot_id=None, table="gold.secret"),
        _checked(session, principal, table_id=None, snapshot_id=secret, table="gold.secret"),
        _checked(session, principal, table_id=None, snapshot_id=None, table="gold.secret"),
        before_the_rebuild,
    ]

    answers = set()
    for asked in [uuid.uuid4(), *(result.id for result in foreign)]:
        with pytest.raises(LakanaError) as refused:
            catalog.load_table_at_check(principal, _REF, check_result_id=asked)
        answers.add((type(refused.value), str(refused.value)))

    assert answers == {(CheckResultGoneError, _not_this_tables())}
    for result in foreign:
        assert str(result.run_id) not in _not_this_tables()
    assert "secret" not in _not_this_tables()
    assert "unique_id" not in _not_this_tables()


def test_a_result_is_bound_by_the_tables_id_and_not_by_its_name(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A renamed table is still the checked one; the old name with a new table is not."""
    catalog = _catalog(session, principal, tmp_path)
    snapshot = _write(catalog, principal, _orders([1]), WriteMode.CREATE)
    result = _checked(session, principal, table_id=_row(session).id, snapshot_id=snapshot)
    moved = TableRef.from_dotted("dev", "gold.moved")
    catalog.rename_table(principal, _REF, moved)
    assert result.table_name == _TABLE

    loaded = catalog.load_table_at_check(principal, moved, check_result_id=result.id)

    assert loaded.snapshot_id == snapshot
    again = _write(catalog, principal, _orders([7]), WriteMode.CREATE)
    forged = _checked(session, principal, table_id=_row(session, "moved").id, snapshot_id=again)
    assert forged.table_name == _TABLE  # the name matches the new table, the id does not
    with pytest.raises(CheckResultGoneError) as not_bound:
        catalog.load_table_at_check(principal, _REF, check_result_id=forged.id)
    assert str(not_bound.value) == _not_this_tables()


def test_a_result_of_another_environment_is_no_result_here(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A result is looked up in the environment of the table that is named."""
    catalog = _catalog(session, principal, tmp_path / "dev")
    catalog.create_environment(
        principal,
        name="prod",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path / 'prod'}", options={}),
    )
    snapshot = _write(catalog, principal, _orders([1]), WriteMode.CREATE)
    elsewhere = _checked(
        session, principal, table_id=_row(session).id, snapshot_id=snapshot, environment="prod"
    )

    with pytest.raises(CheckResultGoneError) as gone:
        catalog.load_table_at_check(principal, _REF, check_result_id=elsewhere.id)

    assert str(gone.value) == _not_this_tables()


def test_a_result_of_this_table_without_a_checked_snapshot_has_no_rows(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The table's own result names no snapshot: nothing was checked that could be read."""
    catalog = _catalog(session, principal, tmp_path)
    _write(catalog, principal, _orders([1]), WriteMode.CREATE)
    result = _checked(
        session,
        principal,
        table_id=_row(session).id,
        snapshot_id=None,
        outcome=CheckOutcome.ERRORED,
    )

    with pytest.raises(NoCheckedSnapshotError) as none:
        catalog.load_table_at_check(principal, _REF, check_result_id=result.id)

    assert str(none.value) == (
        f"This check has no rows to show from run {result.run_id}: no snapshot of the table was "
        "checked."
    )


def test_an_unbound_task_token_does_not_read_the_rows_of_a_check(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A task token bound to no task has no run context and is still a task."""
    catalog = _catalog(session, principal, tmp_path)
    snapshot = _write(catalog, principal, _orders([1]), WriteMode.CREATE)
    result = _checked(session, principal, table_id=_row(session).id, snapshot_id=snapshot)
    unbound = PrincipalRef(
        id=principal.id,
        name=principal.name,
        type=principal.type,
        task_scope=TaskScope(environment="dev", asset_scope=[], token_id=uuid.uuid4()),
    )

    for asked in (result.id, uuid.uuid4()):
        with pytest.raises(PermissionDenied) as refused:
            catalog.load_table_at_check(unbound, _REF, check_result_id=asked)
        assert str(refused.value) == CHECK_ROWS_NOT_FOR_A_TASK


@pytest.mark.parametrize("form", ["claim", "query_run"])
def test_a_task_does_not_read_the_rows_of_a_check(
    session: Session, principal: PrincipalRef, tmp_path: Path, form: str
) -> None:
    """A principal with a run context, in either form, is refused, kept table or not."""
    catalog = _catalog(session, principal, tmp_path)
    snapshot = _write(catalog, principal, _orders([1]), WriteMode.CREATE)
    result = _checked(session, principal, table_id=_row(session).id, snapshot_id=snapshot)
    context = (
        RunContext(task_run_id=result.task_run_id, claim=1)
        if form == "claim"
        else RunContext(task_run_id=result.task_run_id, query_run_id=uuid.uuid4())
    )
    as_task = PrincipalRef(
        id=principal.id, name=principal.name, type=principal.type, run_context=context
    )
    assert catalog.load_table(as_task, _REF).uuid == result.table_id  # an ordinary read passes

    for asked in (result.id, uuid.uuid4()):  # the same answer whether the result exists or not
        with pytest.raises(PermissionDenied) as refused:
            catalog.load_table_at_check(as_task, _REF, check_result_id=asked)
        assert str(refused.value) == CHECK_ROWS_NOT_FOR_A_TASK

    assert CHECK_ROWS_NOT_FOR_A_TASK == (
        "A task cannot read the rows a check found: they are shown to a person's own query, "
        "never to a pipeline. Run `lakana check rows` yourself, outside the task."
    )
    assert (
        catalog.load_table_at_check(principal, _REF, check_result_id=result.id).snapshot_id
        == snapshot
    )


def test_a_table_built_again_under_the_name_is_not_the_checked_one(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Dropped, and dropped and created again: the name no longer holds the checked table."""
    catalog = _catalog(session, principal, tmp_path)
    snapshot = _write(catalog, principal, _orders([1]), WriteMode.CREATE)
    table_id = _row(session).id
    result = _checked(session, principal, table_id=table_id, snapshot_id=snapshot)
    catalog.drop_table(principal, _REF)

    with pytest.raises(CheckResultGoneError) as dropped:
        catalog.load_table_at_check(principal, _REF, check_result_id=result.id)
    assert str(dropped.value) == _not_this_tables()

    again = _write(catalog, principal, _orders([7]), WriteMode.CREATE)
    assert _row(session).id != table_id
    same_number = _checked(session, principal, table_id=table_id, snapshot_id=again)
    for asked in (result, same_number):
        with pytest.raises(CheckResultGoneError) as rebuilt:
            catalog.load_table_at_check(principal, _REF, check_result_id=asked.id)
        assert str(rebuilt.value) == _not_this_tables()


def test_a_namespace_that_is_gone_is_answered_like_a_table_that_is_gone(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The result exists and the named namespace does not: no table there is the checked one."""
    catalog = _catalog(session, principal, tmp_path)
    snapshot = _write(catalog, principal, _orders([1]), WriteMode.CREATE)
    result = _checked(session, principal, table_id=_row(session).id, snapshot_id=snapshot)
    nowhere = TableRef.from_dotted("dev", "nowhere.orders")

    with pytest.raises(CheckResultGoneError) as gone:
        catalog.load_table_at_check(principal, nowhere, check_result_id=result.id)

    assert str(gone.value) == _not_this_tables("nowhere.orders")


def test_a_snapshot_that_maintenance_removed_is_gone(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The failed snapshot of a real rollback, expired by a real maintenance run."""
    catalog = _catalog(session, principal, tmp_path)
    before = _write(catalog, principal, _orders([1]), WriteMode.CREATE)
    failed = _write(catalog, principal, _orders([2, 2]), WriteMode.APPEND)
    result = _fail_the_check(catalog, session, principal, before=before, after=failed)
    assert (
        catalog.load_table_at_check(principal, _REF, check_result_id=result.id).snapshot_id
        == failed
    )
    # Maintenance keeps every snapshot a tag names, and nothing removes the tag of a failed
    # snapshot yet, so the test removes it; and it keeps the newest snapshot, so the table is
    # built again first, as it would be after a failure.
    _write(catalog, principal, _orders([3]), WriteMode.APPEND)
    catalog.commit_table(
        principal,
        _REF,
        requirements=[],
        updates=[RemoveSnapshotRefUpdate(ref_name=failed_snapshot_tag(result.run_id))],
        action=Action.MAINTAIN_TABLE,
    )
    report = catalog.maintain(
        principal, _REF, keep_days=0, keep_last=1, now=datetime.now(UTC) + timedelta(days=8)
    )
    assert failed in report.snapshots_expired
    metadata = catalog.load_table(principal, _REF).metadata
    assert metadata.snapshot_by_id(failed) is None

    with pytest.raises(CheckedSnapshotGoneError) as gone:
        catalog.load_table_at_check(principal, _REF, check_result_id=result.id)

    assert str(gone.value) == _gone(result.run_id)
    assert isinstance(gone.value, LakanaError)


def test_a_view_under_the_name_has_no_snapshot(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A row that is not a managed table has no snapshot to read, whatever its id."""
    catalog = _catalog(session, principal, tmp_path)
    catalog.create_or_replace_view(
        principal,
        _REF,
        sql="SELECT 1 AS id",
        references=[],
        columns=[ColumnSpec(name="id", type="long", nullable=True)],
    )
    result = _checked(session, principal, table_id=_row(session).id, snapshot_id=1)

    with pytest.raises(CheckedSnapshotGoneError) as gone:
        catalog.load_table_at_check(principal, _REF, check_result_id=result.id)

    assert str(gone.value) == _gone(result.run_id)


def test_a_row_of_another_kind_is_not_read_even_with_a_metadata_pointer(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The kind decides on its own: only a managed table is read at a snapshot."""
    catalog = _catalog(session, principal, tmp_path)
    snapshot = _write(catalog, principal, _orders([1]), WriteMode.CREATE)
    table_id = _row(session).id
    result = _checked(session, principal, table_id=table_id, snapshot_id=snapshot)
    session.execute(update(Table).where(Table.id == table_id).values(table_type=TableType.VIEW))
    session.commit()

    with pytest.raises(CheckedSnapshotGoneError) as gone:
        catalog.load_table_at_check(principal, _REF, check_result_id=result.id)

    assert str(gone.value) == _gone(result.run_id)


def test_a_table_without_metadata_has_no_snapshot(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A row whose metadata pointer is not set is answered like any other missing snapshot."""
    catalog = _catalog(session, principal, tmp_path)
    snapshot = _write(catalog, principal, _orders([1]), WriteMode.CREATE)
    table_id = _row(session).id
    result = _checked(session, principal, table_id=table_id, snapshot_id=snapshot)
    session.execute(update(Table).where(Table.id == table_id).values(metadata_location=None))
    session.commit()

    with pytest.raises(CheckedSnapshotGoneError) as gone:
        catalog.load_table_at_check(principal, _REF, check_result_id=result.id)

    assert str(gone.value) == _gone(result.run_id)


# --- the texts ---


def test_the_text_of_a_snapshot_that_is_gone() -> None:
    """One text: the cause, and where the number of rows remains."""
    assert checked_snapshot_gone_message("gold.orders", _RUN, "dev") == (
        "The rows of this check can no longer be read: the snapshot it checked is no longer in "
        "`gold.orders`. Maintenance removes a failed snapshot after 7 days, and a table that is "
        "dropped and built again starts anew. The number of rows the check found remains: "
        "`lakana check list --run 6f1c2a52-3b8e-4d0a-9a55-0c1f5f1f7a10 --env dev`."
    )
    assert checked_snapshot_gone_message("gold.orders", _RUN, "dev") == _gone(_RUN)
    assert f"after {FAILED_SNAPSHOT_KEEP_DAYS} days" in _gone(_RUN)
    assert issubclass(CheckedSnapshotGoneError, LakanaError)


def test_the_texts_of_a_check_without_a_checked_snapshot() -> None:
    """Three reasons why a result names no snapshot, each with its next step, and a plain one."""
    start = "This check has no rows to show from run 6f1c2a52-3b8e-4d0a-9a55-0c1f5f1f7a10: "
    path = "checks/gold/orders/unique_id.sql"

    assert no_snapshot_of_a_view_message(_RUN, "gold.orders", path) == (
        start + "`gold.orders` is a view, so no snapshot was checked. Run the check's SQL "
        "yourself: `checks/gold/orders/unique_id.sql` in the project."
    )
    assert no_snapshot_not_read_message(_RUN, "gold.orders", path) == (
        start + "its SQL does not read `gold.orders`, so no snapshot of it was checked. Run the "
        "check's SQL yourself: `checks/gold/orders/unique_id.sql` in the project."
    )
    assert no_snapshot_could_not_run_message(_RUN, "dev") == (
        start + "the check could not run. Its error is in "
        "`lakana check list --run 6f1c2a52-3b8e-4d0a-9a55-0c1f5f1f7a10 --env dev`."
    )
    assert no_checked_snapshot_message(_RUN) == start + "no snapshot of the table was checked."
    assert issubclass(NoCheckedSnapshotError, LakanaError)


def test_a_name_in_a_text_is_escaped() -> None:
    """A name a manifest or a request gave cannot break out of the line it is shown in."""
    gone = checked_snapshot_gone_message("gold.or\nders", _RUN, "d\nev")
    assert "`gold.or\\nders`" in gone
    assert gone.endswith("--env d\\nev`.")
    assert "`a\\nb` is a view" in no_snapshot_of_a_view_message(_RUN, "a\nb", "p\nq")
    assert "`p\\nq` in the project." in no_snapshot_of_a_view_message(_RUN, "a\nb", "p\nq")
    assert "does not read `a\\nb`" in no_snapshot_not_read_message(_RUN, "a\nb", "p\nq")
    assert "`p\\nq` in the project." in no_snapshot_not_read_message(_RUN, "a\nb", "p\nq")
    assert no_snapshot_could_not_run_message(_RUN, "d\nev").endswith("--env d\\nev`.")
