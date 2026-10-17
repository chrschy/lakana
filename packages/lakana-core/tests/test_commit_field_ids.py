"""A commit through the catalog that adds a data file without Iceberg field ids is refused."""

from __future__ import annotations

import time
from collections import Counter
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from core_test_helpers import _config
from pyiceberg.io.pyarrow import PyArrowFileIO
from pyiceberg.manifest import (
    DataFile,
    DataFileContent,
    FileFormat,
    ManifestContent,
    ManifestEntry,
    ManifestEntryStatus,
    ManifestFile,
    write_manifest,
    write_manifest_list,
)
from pyiceberg.schema import Schema
from pyiceberg.table import StaticTable
from pyiceberg.table.metadata import TableMetadata
from pyiceberg.table.refs import SnapshotRefType
from pyiceberg.table.snapshots import Operation, Snapshot, Summary
from pyiceberg.table.update import AddSnapshotUpdate, SetSnapshotRefUpdate
from pyiceberg.typedef import Record
from pyiceberg.types import LongType, NestedField, StringType

from lakana_core.auth import PrincipalRef
from lakana_core.catalog import field_ids as field_ids_module
from lakana_core.catalog.service import CatalogService, LoadedTable
from lakana_core.errors import StorageUnavailableError, ValidationError
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import WriteMode

_SCHEMA = Schema(
    NestedField(1, "id", LongType(), required=False),
    NestedField(2, "name", StringType(), required=False),
)


def _created(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> tuple[TableRef, LoadedTable]:
    """Create `dev` and an empty `gold.sales`.

    Parameters
    ----------
    catalog
        The catalog under test.
    principal
        The acting principal.
    tmp_path
        The storage root.

    Returns
    -------
    tuple of TableRef and LoadedTable
        The table.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="sales")
    return ref, catalog.create_table(principal, ref, schema=_SCHEMA)


def _parquet(table: LoadedTable, name: str, *, field_ids: bool) -> str:
    """Write a one-row Parquet file under the table's data directory.

    Parameters
    ----------
    table
        The table whose location the file goes under.
    name
        The file's name.
    field_ids
        Whether its columns carry Iceberg field ids.

    Returns
    -------
    str
        The file's URI.
    """

    def column(column_name: str, arrow_type: pa.DataType, field_id: int) -> pa.Field:
        metadata = {b"PARQUET:field_id": str(field_id).encode()} if field_ids else None
        return pa.field(column_name, arrow_type, metadata=metadata)

    uri = f"{table.metadata.location}/data/{name}"
    path = Path(uri.removeprefix("file://"))
    path.parent.mkdir(parents=True, exist_ok=True)
    schema = pa.schema([column("id", pa.int64(), 1), column("name", pa.string(), 2)])
    pq.write_table(pa.table({"id": [1], "name": ["x"]}, schema=schema), path)
    return uri


def _data_file(uri: str, file_format: FileFormat = FileFormat.PARQUET) -> DataFile:
    """Describe a written file as a manifest entry's data file.

    Parameters
    ----------
    uri
        The file's URI.
    file_format
        The format the entry claims.

    Returns
    -------
    DataFile
        An unpartitioned data file without column statistics.
    """
    path = Path(uri.removeprefix("file://"))
    return DataFile.from_args(
        content=DataFileContent.DATA,
        file_path=uri,
        file_format=file_format,
        partition=Record(),
        record_count=1,
        file_size_in_bytes=path.stat().st_size if path.exists() else 1,
        column_sizes={},
        value_counts={},
        null_value_counts={},
        nan_value_counts={},
        lower_bounds={},
        upper_bounds={},
        key_metadata=None,
        split_offsets=None,
        equality_ids=None,
        sort_order_id=None,
    )


def _snapshot(
    metadata: TableMetadata,
    parent: Snapshot | None,
    uris: list[str],
    snapshot_id: int,
    *,
    manifest_content: ManifestContent = ManifestContent.DATA,
    file_format: FileFormat = FileFormat.PARQUET,
    missing_manifest: bool = False,
) -> Snapshot:
    """Write a snapshot that adds `uris` on top of `parent`, as an Iceberg client does.

    Parameters
    ----------
    metadata
        The table's metadata, for its location, spec and schema.
    parent
        The snapshot to build on, or `None` for the first one.
    uris
        The data files to add, in manifest order.
    snapshot_id
        The new snapshot's id.
    manifest_content
        What the manifest list claims the new manifest holds.
    file_format
        The format the entries claim.
    missing_manifest
        Whether the manifest list names a manifest that was never written.

    Returns
    -------
    Snapshot
        The snapshot; its manifest list is written.
    """
    io = PyArrowFileIO()
    sequence = 1 if parent is None else (parent.sequence_number or 0) + 1
    manifest_path = f"{metadata.location}/metadata/{snapshot_id}-m0.avro"
    with write_manifest(
        format_version=metadata.format_version,
        spec=metadata.spec(),
        schema=metadata.schema(),
        output_file=io.new_output(manifest_path if not missing_manifest else manifest_path + ".w"),
        snapshot_id=snapshot_id,
        avro_compression="null",
    ) as writer:
        for uri in uris:
            writer.add(
                ManifestEntry.from_args(
                    status=ManifestEntryStatus.ADDED,
                    snapshot_id=snapshot_id,
                    sequence_number=sequence,
                    file_sequence_number=sequence,
                    data_file=_data_file(uri, file_format),
                )
            )
    new_manifest = writer.to_manifest_file()
    new_manifest[3] = manifest_content
    if missing_manifest:
        new_manifest[0] = manifest_path
    manifests = [*([] if parent is None else parent.manifests(io)), new_manifest]
    list_path = f"{metadata.location}/metadata/snap-{snapshot_id}.avro"
    with write_manifest_list(
        format_version=metadata.format_version,
        output_file=io.new_output(list_path),
        snapshot_id=snapshot_id,
        parent_snapshot_id=None if parent is None else parent.snapshot_id,
        sequence_number=sequence,
        avro_compression="null",
    ) as list_writer:
        list_writer.add_manifests(manifests)
    return Snapshot(
        snapshot_id=snapshot_id,
        parent_snapshot_id=None if parent is None else parent.snapshot_id,
        sequence_number=sequence,
        timestamp_ms=int(time.time() * 1000),
        manifest_list=list_path,
        summary=Summary(operation=Operation.APPEND),
        schema_id=metadata.current_schema_id,
    )


def _commit_snapshots(
    catalog: CatalogService,
    principal: PrincipalRef,
    ref: TableRef,
    snapshots: list[Snapshot],
    *,
    own_data_files: bool = False,
) -> LoadedTable:
    """Commit snapshots, the last one becoming `main`.

    Parameters
    ----------
    catalog
        The catalog under test.
    principal
        The acting principal.
    ref
        The table.
    snapshots
        The snapshots to add, in order.
    own_data_files
        Whether to skip the field-id check, as Lakana's own writes do.

    Returns
    -------
    LoadedTable
        The table after the commit.
    """
    return catalog.commit_table(
        principal,
        ref,
        requirements=[],
        updates=[
            *(AddSnapshotUpdate(snapshot=snapshot) for snapshot in snapshots),
            SetSnapshotRefUpdate(
                ref_name="main", type=SnapshotRefType.BRANCH, snapshot_id=snapshots[-1].snapshot_id
            ),
        ],
        own_data_files=own_data_files,
    )


def _commit_files(
    catalog: CatalogService,
    principal: PrincipalRef,
    ref: TableRef,
    table: LoadedTable,
    uris: list[str],
    snapshot_id: int,
    *,
    manifest_content: ManifestContent = ManifestContent.DATA,
    file_format: FileFormat = FileFormat.PARQUET,
    missing_manifest: bool = False,
) -> LoadedTable:
    """Commit a snapshot that adds `uris` on top of the current one.

    Parameters
    ----------
    catalog
        The catalog under test.
    principal
        The acting principal.
    ref
        The table.
    table
        The table as it is now.
    uris
        The data files to add, in manifest order.
    snapshot_id
        The new snapshot's id.
    manifest_content
        What the manifest list claims the new manifest holds.
    file_format
        The format the entries claim.
    missing_manifest
        Whether the manifest list names a manifest that was never written.

    Returns
    -------
    LoadedTable
        The table after the commit.
    """
    snapshot = _snapshot(
        table.metadata,
        table.metadata.current_snapshot(),
        uris,
        snapshot_id,
        manifest_content=manifest_content,
        file_format=file_format,
        missing_manifest=missing_manifest,
    )
    return _commit_snapshots(catalog, principal, ref, [snapshot])


def _no_field_ids(uri: str) -> str:
    """Write out the refusal of a file without field ids.

    Parameters
    ----------
    uri
        The file.

    Returns
    -------
    str
        The message.
    """
    return (
        f"The commit adds `{uri}` to `gold.sales`, which has no Iceberg field ids. Lakana reads "
        "Iceberg data by field id, as the Iceberg specification requires. Write the file with a "
        "client that sets field ids."
    )


def test_a_commit_that_adds_a_file_without_field_ids_is_refused_whole(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Pyiceberg's `add_files` registers files without field ids; nothing is applied."""
    ref, created = _created(catalog, principal, tmp_path)
    uri = _parquet(created, "plain.parquet", field_ids=False)
    transaction = StaticTable.from_metadata(created.metadata_location).transaction()
    transaction.add_files([uri])

    with pytest.raises(ValidationError) as caught:
        catalog.commit_table(
            principal, ref, requirements=transaction._requirements, updates=transaction._updates
        )

    assert str(caught.value) == _no_field_ids(uri)
    assert catalog.load_table(principal, ref).metadata_location == created.metadata_location


def test_a_commit_that_adds_files_with_field_ids_is_accepted(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uris = [_parquet(created, f"f{i}.parquet", field_ids=True) for i in range(3)]

    committed = _commit_files(catalog, principal, ref, created, uris, snapshot_id=900_000_001)

    assert committed.metadata.current_snapshot_id == 900_000_001


def test_lakanas_own_writes_read_no_footer(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pyiceberg writes field ids for Lakana's own appends and replaces; nothing is read again."""
    ref, _created_table = _created(catalog, principal, tmp_path)

    def refuse(*_args: object) -> bool:
        raise AssertionError("a footer was read for a write Lakana made itself")

    monkeypatch.setattr(field_ids_module, "has_field_ids", refuse)
    rows = pa.table({"id": pa.array([1], pa.int64()), "name": ["x"]})

    catalog.append(principal, ref, rows)
    catalog.write_batches(
        principal, ref, mode=WriteMode.REPLACE, schema=rows.schema, batches=rows.to_batches()
    )

    assert catalog.load_table(principal, ref).metadata.current_snapshot_id is not None


def test_a_commit_of_many_files_names_the_first_without_field_ids_and_reads_each_footer_once(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two hundred files on top of a parent: the parent's file is never read, none twice."""
    ref, created = _created(catalog, principal, tmp_path)
    parent_uri = _parquet(created, "parent.parquet", field_ids=True)
    parent = _commit_files(catalog, principal, ref, created, [parent_uri], snapshot_id=900_000_010)
    uris = [_parquet(created, f"f{i:03}.parquet", field_ids=True) for i in range(200)]
    uris[137] = _parquet(created, "f137-plain.parquet", field_ids=False)
    uris[150] = _parquet(created, "f150-plain.parquet", field_ids=False)
    reads: Counter[str] = Counter()
    real = field_ids_module.has_field_ids

    def counting(file_io: object, path: str) -> bool:
        reads[path] += 1
        return real(file_io, path)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(field_ids_module, "has_field_ids", counting)

    with pytest.raises(ValidationError) as caught:
        _commit_files(catalog, principal, ref, parent, uris, snapshot_id=900_000_011)

    assert str(caught.value) == _no_field_ids(uris[137])
    assert reads[parent_uri] == 0
    assert max(reads.values()) == 1
    assert reads[uris[137]] == 1

    reads.clear()
    good = [_parquet(created, f"g{i:03}.parquet", field_ids=True) for i in range(200)]
    _commit_files(catalog, principal, ref, parent, good, snapshot_id=900_000_012)
    assert sorted(reads) == sorted(good)
    assert set(reads.values()) == {1}


def test_a_file_that_is_not_parquet_is_refused_without_the_readers_error_text(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The message names the file the caller committed and nothing the reader said."""
    ref, created = _created(catalog, principal, tmp_path)
    uri = f"{created.metadata.location}/data/broken.parquet"
    path = Path(uri.removeprefix("file://"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a parquet file")

    with pytest.raises(ValidationError) as caught:
        _commit_files(catalog, principal, ref, created, [uri], snapshot_id=900_000_020)

    assert str(caught.value) == (
        f"The commit adds `{uri}` to `gold.sales`, which could not be read as a Parquet file, so "
        "nothing was applied. Write the data file before the commit, as Parquet with Iceberg "
        "field ids."
    )
    assert "magic" not in str(caught.value).lower()


def test_a_file_outside_the_table_is_never_opened(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The commit check reads only under the table's location; a read refuses the table later."""
    ref, created = _created(catalog, principal, tmp_path)
    outside = tmp_path / "elsewhere" / "x.parquet"
    outside.parent.mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), outside)
    opened: list[str] = []

    def record(_file_io: object, path: str) -> bool:
        opened.append(path)
        return True

    monkeypatch.setattr(field_ids_module, "has_field_ids", record)

    _commit_files(catalog, principal, ref, created, [f"file://{outside}"], snapshot_id=900_000_030)

    assert opened == []


def test_a_data_file_in_a_manifest_labelled_as_deletes_is_refused(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A read walks every manifest, so the check does too, whatever the list entry claims."""
    ref, created = _created(catalog, principal, tmp_path)
    uri = _parquet(created, "hidden.parquet", field_ids=False)

    with pytest.raises(ValidationError) as caught:
        _commit_files(
            catalog,
            principal,
            ref,
            created,
            [uri],
            900_000_040,
            manifest_content=ManifestContent.DELETES,
        )

    assert str(caught.value) == _no_field_ids(uri)


def test_an_orc_file_is_refused(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uri = _parquet(created, "a.orc", field_ids=True)

    with pytest.raises(ValidationError) as caught:
        _commit_files(
            catalog, principal, ref, created, [uri], 900_000_050, file_format=FileFormat.ORC
        )

    assert str(caught.value) == (
        f"The commit adds `{uri}` to `gold.sales` as ORC, so nothing was applied. Lakana reads "
        "Parquet data files only; write the file as Parquet."
    )


def test_a_missing_manifest_is_refused(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uri = _parquet(created, "a.parquet", field_ids=True)

    with pytest.raises(ValidationError) as caught:
        _commit_files(catalog, principal, ref, created, [uri], 900_000_060, missing_manifest=True)

    assert str(caught.value) == (
        "A manifest of the commit's snapshot for `gold.sales` could not be read, so nothing was "
        "applied. Write the manifests and the manifest list before the commit."
    )


def test_a_missing_data_file_is_refused(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uri = f"{created.metadata.location}/data/never-written.parquet"

    with pytest.raises(ValidationError) as caught:
        _commit_files(catalog, principal, ref, created, [uri], 900_000_070)

    assert "could not be read as a Parquet file" in str(caught.value)
    assert uri in str(caught.value)


def test_a_file_name_with_a_line_break_is_escaped_in_the_refusal(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The client's own path is shown as the rest of the codebase shows one, never raw."""
    ref, created = _created(catalog, principal, tmp_path)
    uri = f"{created.metadata.location}/data/a\nb.parquet"

    with pytest.raises(ValidationError) as caught:
        _commit_files(catalog, principal, ref, created, [uri], 900_000_071)

    assert "\n" not in str(caught.value)
    assert "a\\nb.parquet" in str(caught.value)


def test_a_commit_of_too_many_files_is_refused_before_any_footer_is_read(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    monkeypatch.setattr(field_ids_module, "MAX_CHECKED_FILES", 3)
    uris = [_parquet(created, f"f{i}.parquet", field_ids=True) for i in range(4)]

    def refuse(*_args: object) -> bool:
        raise AssertionError("a footer was read")

    monkeypatch.setattr(field_ids_module, "has_field_ids", refuse)

    with pytest.raises(ValidationError) as caught:
        _commit_files(catalog, principal, ref, created, uris, 900_000_080)

    assert str(caught.value) == (
        "The commit adds more than 3 data files to `gold.sales`; Lakana checks at most 3 per "
        "commit. Split the load into smaller commits."
    )


def test_a_check_that_outlasts_its_deadline_refuses_the_commit(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uri = _parquet(created, "a.parquet", field_ids=True)
    ticks = iter([0.0, 1000.0, 1000.0, 1000.0])
    monkeypatch.setattr(field_ids_module, "_clock", lambda: next(ticks))

    with pytest.raises(ValidationError) as caught:
        _commit_files(catalog, principal, ref, created, [uri], 900_000_090)

    assert str(caught.value) == (
        "Checking the data files the commit adds to `gold.sales` for Iceberg field ids took "
        "longer than 60 seconds, so nothing was applied. Split the commit into smaller ones."
    )


def test_a_footer_above_the_limit_is_refused(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uri = _parquet(created, "a.parquet", field_ids=True)
    monkeypatch.setattr(field_ids_module, "MAX_FOOTER_BYTES", 10)

    with pytest.raises(ValidationError) as caught:
        _commit_files(catalog, principal, ref, created, [uri], 900_000_100)

    assert str(caught.value) == (
        f"The commit adds `{uri}` to `gold.sales`, whose Parquet footer is larger than 16 MiB, "
        "so nothing was applied. Write the file with fewer columns and row groups."
    )


def test_a_storage_failure_is_not_blamed_on_the_client(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uri = _parquet(created, "a.parquet", field_ids=True)

    def fail(*_args: object) -> bool:
        raise OSError("AWS Error SLOW_DOWN for https://bucket.example/secret?X-Amz-Signature=abc")

    monkeypatch.setattr(field_ids_module, "has_field_ids", fail)

    with pytest.raises(StorageUnavailableError) as caught:
        _commit_files(catalog, principal, ref, created, [uri], 900_000_110)

    assert "Try the commit again" in str(caught.value)
    assert "secret" not in str(caught.value)


def test_only_the_snapshots_of_the_commit_are_checked(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy file without ids stays unchecked when a commit adds two snapshots on top."""
    ref, created = _created(catalog, principal, tmp_path)
    legacy_uri = _parquet(created, "legacy.parquet", field_ids=False)
    legacy = _snapshot(created.metadata, None, [legacy_uri], 900_000_120)
    after_legacy = _commit_snapshots(catalog, principal, ref, [legacy], own_data_files=True)
    first_uri = _parquet(created, "one.parquet", field_ids=True)
    second_uri = _parquet(created, "two.parquet", field_ids=True)
    parent = after_legacy.metadata.current_snapshot()
    first = _snapshot(after_legacy.metadata, parent, [first_uri], 900_000_121)
    second = _snapshot(after_legacy.metadata, first, [second_uri], 900_000_122)
    reads: Counter[str] = Counter()
    real = field_ids_module.has_field_ids

    def counting(file_io: object, path: str) -> bool:
        reads[path] += 1
        return real(file_io, path)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(field_ids_module, "has_field_ids", counting)

    committed = _commit_snapshots(catalog, principal, ref, [first, second])

    assert committed.metadata.current_snapshot_id == 900_000_122
    assert reads == Counter({first_uri: 1, second_uri: 1})


def _forged_parent(snapshot: Snapshot, parent_id: int) -> Snapshot:
    """Copy a snapshot with another parent id, as a client may commit one.

    Parameters
    ----------
    snapshot
        The snapshot to copy.
    parent_id
        The parent id to claim.

    Returns
    -------
    Snapshot
        The copy; its manifest list is the original's.
    """
    return snapshot.model_copy(update={"parent_snapshot_id": parent_id})


def test_a_snapshot_that_names_itself_as_its_parent_is_checked_in_full(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uri = _parquet(created, "plain.parquet", field_ids=False)
    own = _snapshot(created.metadata, None, [uri], 900_000_130)

    with pytest.raises(ValidationError) as caught:
        _commit_snapshots(catalog, principal, ref, [_forged_parent(own, 900_000_130)])

    assert str(caught.value) == _no_field_ids(uri)


def test_a_snapshot_whose_parent_comes_later_in_the_commit_is_checked_in_full(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uri = _parquet(created, "plain.parquet", field_ids=False)
    other = _parquet(created, "other.parquet", field_ids=True)
    first = _snapshot(created.metadata, None, [uri], 900_000_140)
    second = _snapshot(created.metadata, first, [other], 900_000_141)

    with pytest.raises(ValidationError) as caught:
        _commit_snapshots(catalog, principal, ref, [_forged_parent(first, 900_000_141), second])

    assert str(caught.value) == _no_field_ids(uri)


def test_a_snapshot_with_an_unknown_parent_is_checked_in_full(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uri = _parquet(created, "plain.parquet", field_ids=False)
    own = _snapshot(created.metadata, None, [uri], 900_000_150)

    with pytest.raises(ValidationError) as caught:
        _commit_snapshots(catalog, principal, ref, [_forged_parent(own, 424_242)])

    assert str(caught.value) == _no_field_ids(uri)


@pytest.mark.parametrize(
    "content",
    [
        b"PAR1" + b"x" * 20 + (20).to_bytes(4, "little") + b"PAR1",
        b"PAR1" + (0).to_bytes(4, "little") + b"PAR1",
    ],
    ids=["garbage-footer", "empty-footer"],
)
def test_a_garbled_footer_is_the_files_fault_not_a_storage_failure(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, content: bytes
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uri = f"{created.metadata.location}/data/garbled.parquet"
    path = Path(uri.removeprefix("file://"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)

    with pytest.raises(ValidationError) as caught:
        _commit_files(catalog, principal, ref, created, [uri], 900_000_160)

    assert "could not be read as a Parquet file" in str(caught.value)
    assert uri in str(caught.value)


def test_collecting_stops_once_the_cap_is_passed(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second snapshot's manifest is never opened once the first one passed the cap."""
    ref, created = _created(catalog, principal, tmp_path)
    monkeypatch.setattr(field_ids_module, "MAX_CHECKED_FILES", 2)
    first = _snapshot(
        created.metadata,
        None,
        [_parquet(created, f"a{i}.parquet", field_ids=True) for i in range(3)],
        900_000_170,
    )
    second = _snapshot(
        created.metadata,
        first,
        [_parquet(created, f"b{i}.parquet", field_ids=True) for i in range(3)],
        900_000_171,
    )
    fetched: list[str] = []
    real = ManifestFile.fetch_manifest_entry

    def counting(
        self: ManifestFile, io: object, discard_deleted: bool = True
    ) -> list[ManifestEntry]:
        fetched.append(self.manifest_path)
        return real(self, io, discard_deleted)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(ManifestFile, "fetch_manifest_entry", counting)

    with pytest.raises(ValidationError, match="Lakana checks at most 2 per commit"):
        _commit_snapshots(catalog, principal, ref, [first, second])

    assert len(fetched) == 1


def test_the_deadline_also_covers_the_manifest_reads(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uri = _parquet(created, "a.parquet", field_ids=True)
    ticks = iter([0.0, 1000.0, 1000.0])
    monkeypatch.setattr(field_ids_module, "_clock", lambda: next(ticks))

    opened: list[str] = []

    def record(_file_io: object, path: str) -> bool:
        opened.append(path)
        return True

    monkeypatch.setattr(field_ids_module, "has_field_ids", record)

    with pytest.raises(ValidationError, match="took longer than 60 seconds"):
        _commit_files(catalog, principal, ref, created, [uri], 900_000_180)

    assert opened == []
