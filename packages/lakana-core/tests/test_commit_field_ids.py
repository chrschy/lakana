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
    ManifestEntry,
    ManifestEntryStatus,
    write_manifest,
    write_manifest_list,
)
from pyiceberg.schema import Schema
from pyiceberg.table import StaticTable
from pyiceberg.table.refs import SnapshotRefType
from pyiceberg.table.snapshots import Operation, Snapshot, Summary
from pyiceberg.table.update import AddSnapshotUpdate, SetSnapshotRefUpdate
from pyiceberg.typedef import Record
from pyiceberg.types import LongType, NestedField, StringType

from lakana_core.auth import PrincipalRef
from lakana_core.catalog import field_ids as field_ids_module
from lakana_core.catalog.service import CatalogService, LoadedTable
from lakana_core.errors import ValidationError
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


def _data_file(uri: str) -> DataFile:
    """Describe a written file as a manifest entry's data file.

    Parameters
    ----------
    uri
        The file's URI.

    Returns
    -------
    DataFile
        An unpartitioned Parquet data file without column statistics.
    """
    path = Path(uri.removeprefix("file://"))
    return DataFile.from_args(
        content=DataFileContent.DATA,
        file_path=uri,
        file_format=FileFormat.PARQUET,
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


def _commit_files(
    catalog: CatalogService,
    principal: PrincipalRef,
    ref: TableRef,
    table: LoadedTable,
    uris: list[str],
    snapshot_id: int,
) -> LoadedTable:
    """Commit a snapshot that adds `uris` on top of the current one, as an Iceberg client does.

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

    Returns
    -------
    LoadedTable
        The table after the commit.
    """
    io = PyArrowFileIO()
    metadata = table.metadata
    parent = metadata.current_snapshot()
    sequence = 1 if parent is None else (parent.sequence_number or 0) + 1
    manifest_path = f"{metadata.location}/metadata/{snapshot_id}-m0.avro"
    with write_manifest(
        format_version=metadata.format_version,
        spec=metadata.spec(),
        schema=metadata.schema(),
        output_file=io.new_output(manifest_path),
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
                    data_file=_data_file(uri),
                )
            )
    manifests = [*([] if parent is None else parent.manifests(io)), writer.to_manifest_file()]
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
    snapshot = Snapshot(
        snapshot_id=snapshot_id,
        parent_snapshot_id=None if parent is None else parent.snapshot_id,
        sequence_number=sequence,
        timestamp_ms=int(time.time() * 1000),
        manifest_list=list_path,
        summary=Summary(operation=Operation.APPEND),
        schema_id=metadata.current_schema_id,
    )
    return catalog.commit_table(
        principal,
        ref,
        requirements=[],
        updates=[
            AddSnapshotUpdate(snapshot=snapshot),
            SetSnapshotRefUpdate(
                ref_name="main", type=SnapshotRefType.BRANCH, snapshot_id=snapshot_id
            ),
        ],
    )


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
