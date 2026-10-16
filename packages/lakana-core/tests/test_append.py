"""Tests for the governed append write path."""

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from core_test_helpers import _config, _RecordThenDeny
from pyiceberg.io import FileIO
from pyiceberg.schema import Schema
from pyiceberg.table.snapshots import Snapshot
from pyiceberg.types import LongType, NestedField, StringType, TimestampType, TimestamptzType
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    PrincipalRef,
    Securable,
    SecurableKind,
    TaskScope,
)
from lakana_core.catalog import service as service_module
from lakana_core.catalog.append import AppendResult, _types_compatible, root_relative_path
from lakana_core.catalog.append import new_data_file_paths as _new_data_file_paths
from lakana_core.catalog.datafiles import list_data_files
from lakana_core.catalog.service import CatalogService
from lakana_core.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import AuditEvent, AuditEventType, Grant
from lakana_core.storage.fsspec_storage import FsspecStorage
from lakana_core.storage.resolver import StorageResolver, is_within_location

_SALES_ARROW_SCHEMA = pa.schema(
    [pa.field("id", pa.int64(), nullable=False), pa.field("region", pa.string())]
)


def _sales_schema() -> Schema:
    """Build the Iceberg schema `bronze.sales` is created with in these tests.

    Returns
    -------
    Schema
        A required `id` and an optional `region`.
    """
    return Schema(
        NestedField(1, "id", LongType(), required=True),
        NestedField(2, "region", StringType(), required=False),
    )


def _sales_data(ids: list[int], regions: list[str]) -> pa.Table:
    """Build Arrow data matching `_sales_schema`'s columns exactly.

    Parameters
    ----------
    ids
        Values for the `id` column.
    regions
        Values for the `region` column.

    Returns
    -------
    pa.Table
        A table with `id` (non-nullable int64) and `region` (nullable string).
    """
    return pa.table(
        {"id": pa.array(ids, pa.int64()), "region": pa.array(regions, pa.string())},
        schema=_SALES_ARROW_SCHEMA,
    )


def _prepared_sales_ref(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> TableRef:
    """Create `dev`/`bronze`/`sales` and return a reference to it.

    Parameters
    ----------
    catalog
        The catalog to create the environment and table through.
    principal
        The acting principal.
    tmp_path
        Directory backing the environment's storage.

    Returns
    -------
    TableRef
        A reference to the created table.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["bronze"]), name="sales")
    catalog.create_table(principal, ref, schema=_sales_schema())
    return ref


def _read_rows(paths: list[str]) -> pa.Table:
    """Read and concatenate a table's data files from a `file://` location.

    Parameters
    ----------
    paths
        Absolute `file://` URIs of the data files to read.

    Returns
    -------
    pa.Table
        The concatenated rows.
    """
    return pa.concat_tables([pq.read_table(Path(path.removeprefix("file://"))) for path in paths])


def test_append_writes_rows_readable_from_the_new_snapshot(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, session: Session
) -> None:
    """Appended rows land under the table's location, in a new, readable snapshot."""
    ref = _prepared_sales_ref(catalog, principal, tmp_path)

    result = catalog.append(principal, ref, _sales_data([1, 2], ["eu", "us"]))

    assert result.rows == 2
    assert result.files >= 1
    assert result.snapshot_id is not None

    loaded = catalog.load_table(principal, ref)
    assert loaded.metadata.current_snapshot_id == result.snapshot_id

    file_io = StorageResolver(session).for_environment("dev").file_io
    paths = list_data_files(loaded.metadata, file_io)
    assert len(paths) == result.files
    for path in paths:
        assert is_within_location(loaded.metadata.location, path)

    rows = _read_rows(paths)
    assert sorted(rows.column("id").to_pylist()) == [1, 2]
    assert sorted(rows.column("region").to_pylist()) == ["eu", "us"]


def test_append_twice_accumulates(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Two appends produce two snapshots, the second's parent being the first."""
    ref = _prepared_sales_ref(catalog, principal, tmp_path)

    first = catalog.append(principal, ref, _sales_data([1], ["eu"]))
    second = catalog.append(principal, ref, _sales_data([2], ["us"]))

    loaded = catalog.load_table(principal, ref)
    assert loaded.metadata.current_snapshot_id == second.snapshot_id
    snapshot = next(s for s in loaded.metadata.snapshots if s.snapshot_id == second.snapshot_id)
    assert snapshot.parent_snapshot_id == first.snapshot_id


def test_append_requires_write_table(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Appending authorizes WRITE_TABLE on the table, before anything else."""
    setup = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    ref = _prepared_sales_ref(setup, principal, tmp_path)
    deny = _RecordThenDeny()
    catalog = CatalogService(session, StorageResolver(session), deny)

    with pytest.raises(PermissionDenied):
        catalog.append(principal, ref, _sales_data([1], ["eu"]))

    assert deny.seen == (
        Action.WRITE_TABLE,
        Securable.table_scope(ref.environment, ref.namespace, ref.name),
    )


def test_check_can_append_succeeds_silently_for_a_writable_iceberg_table(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A principal with WRITE_TABLE on an ICEBERG table passes the pre-check."""
    ref = _prepared_sales_ref(catalog, principal, tmp_path)

    assert catalog.check_can_append(principal, ref) is None


def test_check_can_append_requires_write_table(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The pre-check authorizes WRITE_TABLE on the table, the same as append() itself."""
    setup = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    ref = _prepared_sales_ref(setup, principal, tmp_path)
    deny = _RecordThenDeny()
    catalog = CatalogService(session, StorageResolver(session), deny)

    with pytest.raises(PermissionDenied):
        catalog.check_can_append(principal, ref)

    assert deny.seen == (
        Action.WRITE_TABLE,
        Securable.table_scope(ref.environment, ref.namespace, ref.name),
    )


def test_check_can_append_missing_table_is_not_found(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A table that does not exist is a NotFoundError, before any body is ever read."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["bronze"]), name="nope")

    with pytest.raises(NotFoundError):
        catalog.check_can_append(principal, ref)


def test_check_can_append_requires_iceberg_table(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """An EXTERNAL (raw-zone) table fails the pre-check the same way append() itself refuses it."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    (tmp_path / "raw" / "events").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), tmp_path / "raw" / "events" / "part-0.parquet")
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["raw"]), name="events")
    catalog.create_external_table(
        principal, ref, location_pattern="raw/events/*.parquet", format="parquet"
    )

    with pytest.raises(ValidationError):
        catalog.check_can_append(principal, ref)


@pytest.mark.parametrize(
    ("build_data", "expected_match"),
    [
        pytest.param(
            lambda: pa.table({"id": pa.array([1], pa.int64()), "region": ["eu"], "extra": [1]}),
            r"extra",
            id="extra_column",
        ),
        pytest.param(
            lambda: pa.table({"id": pa.array([1], pa.int64())}),
            r"region",
            id="missing_column",
        ),
        pytest.param(
            lambda: pa.table({"id": pa.array([1], pa.int32()), "region": ["eu"]}),
            r"'id'",
            id="wrong_type",
        ),
        pytest.param(
            lambda: pa.table(
                {"id": [1], "region": ["eu"]},
                schema=pa.schema(
                    [pa.field("id", pa.int64(), nullable=True), pa.field("region", pa.string())]
                ),
            ),
            r"'id'",
            id="nullable_mismatch",
        ),
        pytest.param(
            lambda: pa.Table.from_arrays(
                [pa.array([1], pa.int64()), pa.array(["eu"])],
                schema=pa.schema(
                    [pa.field("id", pa.int64(), nullable=False), pa.field("id", pa.string())]
                ),
            ),
            r"'id'",
            id="duplicate_column",
        ),
        pytest.param(
            lambda: pa.table(
                {"id": [1, None], "region": ["eu", "us"]},
                schema=pa.schema(
                    [pa.field("id", pa.int64(), nullable=False), pa.field("region", pa.string())]
                ),
            ),
            r"'id'",
            id="null_in_required_column",
        ),
    ],
)
def test_append_schema_mismatch_names_the_column(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    build_data: Callable[[], pa.Table],
    expected_match: str,
) -> None:
    """An extra, missing, mistyped, or wrongly-nullable column is refused by name."""
    ref = _prepared_sales_ref(catalog, principal, tmp_path)

    with pytest.raises(ValidationError, match=expected_match):
        catalog.append(principal, ref, build_data())


def test_append_ignores_swapped_field_id_metadata(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, session: Session
) -> None:
    """Arrow field-id metadata that disagrees with column name/order never redirects a write.

    pyiceberg's writer honours a `PARQUET:field_id` metadata entry on a
    field when present, writing that column under its *declared* id
    rather than by name. The reviewer reproduced two same-type columns
    with swapped field ids committing `a=99, b=1` under the wrong Iceberg
    columns, even though both validated correctly by name against the
    table's schema -- reading the committed
    data back through Iceberg's own field-id-keyed parquet reader (not a
    raw, name-based read, which would not reveal the swap) is what
    `strip_field_metadata` fixes.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["bronze"]), name="ab")
    schema = Schema(
        NestedField(1, "a", LongType(), required=False),
        NestedField(2, "b", LongType(), required=False),
    )
    catalog.create_table(principal, ref, schema=schema)

    # "a"'s Arrow field claims field-id 2 (Iceberg's "b"), and "b"'s claims
    # field-id 1 (Iceberg's "a") -- swapped. By name and type, this still
    # validates cleanly against the table's schema.
    swapped_schema = pa.schema(
        [
            pa.field("a", pa.int64(), metadata={b"PARQUET:field_id": b"2"}),
            pa.field("b", pa.int64(), metadata={b"PARQUET:field_id": b"1"}),
        ]
    )
    data = pa.table({"a": [99], "b": [1]}, schema=swapped_schema)

    result = catalog.append(principal, ref, data)
    assert result.rows == 1

    loaded = catalog.load_table(principal, ref)
    file_io = StorageResolver(session).for_environment("dev").file_io
    paths = list_data_files(loaded.metadata, file_io)
    written = pq.ParquetFile(Path(paths[0].removeprefix("file://")))
    field_id_to_index = {
        int(field.metadata[b"PARQUET:field_id"]): index
        for index, field in enumerate(written.schema_arrow)
    }
    rows = written.read()
    assert rows.column(field_id_to_index[1]).to_pylist() == [99]  # Iceberg field 1 is "a"
    assert rows.column(field_id_to_index[2]).to_pylist() == [1]  # Iceberg field 2 is "b"


def test_append_field_id_metadata_pointing_at_a_mismatched_type_never_500s(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Field-id metadata pointing at a column of a different type never crashes.

    Before the fix, a caller-supplied `field_id` metadata entry that
    disagreed with the table's own field-id-to-type mapping could
    misdirect pyiceberg's writer into treating data as the wrong
    (mismatched) Iceberg type, previously reproduced raising an unhandled
    exception rather than a clean 4xx. With metadata stripped first, this
    is now an ordinary, correctly-typed append by name -- never a 500, whether or not it succeeds.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["bronze"]), name="ac")
    schema = Schema(
        NestedField(1, "a", LongType(), required=False),
        NestedField(2, "c", StringType(), required=False),
    )
    catalog.create_table(principal, ref, schema=schema)

    # "a"'s Arrow field (int64, matching by name) carries metadata claiming
    # it is field-id 2 -- Iceberg's "c", a string column.
    misdirected_schema = pa.schema(
        [
            pa.field("a", pa.int64(), metadata={b"PARQUET:field_id": b"2"}),
            pa.field("c", pa.string(), metadata={b"PARQUET:field_id": b"1"}),
        ]
    )
    data = pa.table({"a": [42], "c": ["x"]}, schema=misdirected_schema)

    result = catalog.append(principal, ref, data)

    assert result.rows == 1


def test_append_timestamp_timezone_matching(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A timestamptz column refuses a naive value and vice versa."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["bronze"]), name="events")
    catalog.create_table(
        principal,
        ref,
        schema=Schema(
            NestedField(1, "id", LongType(), required=True),
            NestedField(2, "at", TimestamptzType(), required=False),
            NestedField(3, "naive", TimestampType(), required=False),
        ),
    )

    id_field = pa.field("id", pa.int64(), nullable=False)

    good = pa.table(
        {
            "id": [1],
            "at": pa.array([datetime(2024, 1, 1, tzinfo=UTC)], pa.timestamp("us", tz="UTC")),
            "naive": pa.array([datetime(2024, 1, 1)], pa.timestamp("us")),
        },
        schema=pa.schema(
            [
                id_field,
                pa.field("at", pa.timestamp("us", tz="UTC")),
                pa.field("naive", pa.timestamp("us")),
            ]
        ),
    )
    catalog.append(principal, ref, good)

    naive_for_timestamptz = pa.table(
        {
            "id": [1],
            "at": pa.array([datetime(2024, 1, 1)], pa.timestamp("us")),
            "naive": pa.array([datetime(2024, 1, 1)], pa.timestamp("us")),
        },
        schema=pa.schema(
            [id_field, pa.field("at", pa.timestamp("us")), pa.field("naive", pa.timestamp("us"))]
        ),
    )
    with pytest.raises(ValidationError, match=r"'at'"):
        catalog.append(principal, ref, naive_for_timestamptz)

    tz_aware_for_timestamp = pa.table(
        {
            "id": [1],
            "at": pa.array([datetime(2024, 1, 1, tzinfo=UTC)], pa.timestamp("us", tz="UTC")),
            "naive": pa.array([datetime(2024, 1, 1, tzinfo=UTC)], pa.timestamp("us", tz="UTC")),
        },
        schema=pa.schema(
            [
                id_field,
                pa.field("at", pa.timestamp("us", tz="UTC")),
                pa.field("naive", pa.timestamp("us", tz="UTC")),
            ]
        ),
    )
    with pytest.raises(ValidationError, match=r"'naive'"):
        catalog.append(principal, ref, tz_aware_for_timestamp)

    for row_id, tz_alias in ((1, "+00:00"), (2, "Etc/UTC")):
        alias_good = pa.table(
            {
                "id": [row_id],
                "at": pa.array([datetime(2024, 1, 1, tzinfo=UTC)], pa.timestamp("us", tz=tz_alias)),
                "naive": pa.array([datetime(2024, 1, 1)], pa.timestamp("us")),
            },
            schema=pa.schema(
                [
                    id_field,
                    pa.field("at", pa.timestamp("us", tz=tz_alias)),
                    pa.field("naive", pa.timestamp("us")),
                ]
            ),
        )
        catalog.append(principal, ref, alias_good)


def test_append_empty_table_is_a_noop(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Appending zero rows commits nothing and leaves the current snapshot alone."""
    ref = _prepared_sales_ref(catalog, principal, tmp_path)
    before = catalog.load_table(principal, ref)

    result = catalog.append(principal, ref, _sales_data([], []))

    assert result == AppendResult(rows=0, files=0, snapshot_id=None)
    after = catalog.load_table(principal, ref)
    assert after.metadata_location == before.metadata_location
    assert after.metadata.current_snapshot_id == before.metadata.current_snapshot_id


def test_append_to_external_table_is_refused(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """An EXTERNAL (raw-zone) table has no Iceberg metadata to append to."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    (tmp_path / "raw" / "events").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), tmp_path / "raw" / "events" / "part-0.parquet")
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["raw"]), name="events")
    catalog.create_external_table(
        principal, ref, location_pattern="raw/events/*.parquet", format="parquet"
    )

    with pytest.raises(ValidationError):
        catalog.append(principal, ref, _sales_data([1], ["eu"]))


def test_append_records_table_appended_audit_without_values(
    session: Session, catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Exactly one TABLE_APPENDED event is recorded, carrying no data values."""
    ref = _prepared_sales_ref(catalog, principal, tmp_path)

    result = catalog.append(principal, ref, _sales_data([1, 2], ["eu", "us"]))

    events = (
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.TABLE_APPENDED)
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    event = events[0]
    assert event.detail == {"rows": 2, "files": result.files, "snapshot_id": result.snapshot_id}
    assert event.securable_table == "sales"
    serialized = json.dumps(event.detail)
    assert "eu" not in serialized
    assert "us" not in serialized


def test_append_task_token_outside_scope_is_denied(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A task token scoped to silver.* cannot append to bronze.sales, even with a matching grant."""
    setup = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    ref = _prepared_sales_ref(setup, principal, tmp_path)
    session.add(
        Grant(
            id=uuid.uuid4(),
            principal_id=principal.id,
            role=Role.DATA_SCIENTIST,
            kind=SecurableKind.INSTANCE,
            granted_by=principal.id,
            granted_at=datetime.now(UTC),
        )
    )
    session.commit()
    task_principal = PrincipalRef(
        id=principal.id,
        name=principal.name,
        type=principal.type,
        task_scope=TaskScope(environment="dev", asset_scope=["silver.*"], token_id=uuid.uuid4()),
    )
    catalog = CatalogService(session, StorageResolver(session), GrantAuthorization(session))

    with pytest.raises(PermissionDenied):
        catalog.append(task_principal, ref, _sales_data([1], ["eu"]))


def test_append_conflict_deletes_the_data_files_it_wrote(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A lost commit race cleans up the data files this append wrote, then re-raises."""
    ref = _prepared_sales_ref(catalog, principal, tmp_path)

    with (
        mock.patch.object(catalog, "commit_table", side_effect=ConflictError("lost the race")),
        pytest.raises(ConflictError),
    ):
        catalog.append(principal, ref, _sales_data([1], ["eu"]))

    written = list((tmp_path / "managed" / "bronze").glob("sales-*/data/*.parquet"))
    assert written == []


def test_append_conflict_skips_a_written_path_outside_the_storage_root(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Cleanup skips a reported path `root_relative_path` can't place under the root."""
    ref = _prepared_sales_ref(catalog, principal, tmp_path)

    def _with_bogus_extra(new_snapshot: Snapshot, file_io: FileIO) -> list[str]:
        real_paths = _new_data_file_paths(new_snapshot, file_io)
        return [*real_paths, "file:///elsewhere/data/bogus.parquet"]

    with (
        mock.patch.object(service_module, "new_data_file_paths", side_effect=_with_bogus_extra),
        mock.patch.object(catalog, "commit_table", side_effect=ConflictError("lost the race")),
        pytest.raises(ConflictError),
    ):
        catalog.append(principal, ref, _sales_data([1], ["eu"]))

    written = list((tmp_path / "managed" / "bronze").glob("sales-*/data/*.parquet"))
    assert written == []


def test_append_with_manifest_merge_counts_files_correctly_and_cleans_up_only_the_new_one(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, session: Session
) -> None:
    """With manifest merging on, each append's file count is its own, not the table's total.

    Regression: `commit.manifest-merge.enabled` folds an
    older manifest's `EXISTING` entries into the new snapshot's own (possibly merged)
    manifest. Counting/deleting by manifest (`added_snapshot_id`) rather than by manifest
    *entry* used to overcount `AppendResult.files` and would have deleted earlier, already
    committed data files on a failed third commit.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["bronze"]), name="sales")
    catalog.create_table(
        principal,
        ref,
        schema=_sales_schema(),
        properties={
            "commit.manifest-merge.enabled": "true",
            "commit.manifest.min-count-to-merge": "2",
        },
    )

    first = catalog.append(principal, ref, _sales_data([1], ["eu"]))
    second = catalog.append(principal, ref, _sales_data([2], ["us"]))
    assert first.files == 1
    assert second.files == 1

    data_dir = tmp_path / "managed" / "bronze"
    before = set(data_dir.glob("sales-*/data/*.parquet"))
    assert len(before) == 2

    with (
        mock.patch.object(
            service_module, "commit_metadata_pointer", side_effect=ConflictError("lost the race")
        ),
        pytest.raises(ConflictError),
    ):
        catalog.append(principal, ref, _sales_data([3], ["de"]))

    after = set(data_dir.glob("sales-*/data/*.parquet"))
    assert after == before

    loaded = catalog.load_table(principal, ref)
    assert loaded.metadata.current_snapshot_id == second.snapshot_id
    file_io = StorageResolver(session).for_environment("dev").file_io
    paths = list_data_files(loaded.metadata, file_io)
    assert len(paths) == 2
    rows = _read_rows(paths)
    assert sorted(rows.column("id").to_pylist()) == [1, 2]


def test_append_conflict_never_deletes_a_prior_appends_manifest(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, session: Session
) -> None:
    """A failed second append cleans up only its own file and manifest, never the first's.

    Under the default (fast-append, no merging) write path, a second append's staged
    snapshot references *both* its own brand-new manifest and the first append's
    unchanged, already-committed one. Cleanup must delete only the former.
    """
    ref = _prepared_sales_ref(catalog, principal, tmp_path)
    first = catalog.append(principal, ref, _sales_data([1], ["eu"]))

    table_dir = tmp_path / "managed" / "bronze"
    data_before = set(table_dir.glob("sales-*/data/*.parquet"))
    manifests_before = set(table_dir.glob("sales-*/metadata/*.avro"))
    assert len(data_before) == 1

    with (
        mock.patch.object(catalog, "commit_table", side_effect=ConflictError("lost the race")),
        pytest.raises(ConflictError),
    ):
        catalog.append(principal, ref, _sales_data([2], ["us"]))

    assert set(table_dir.glob("sales-*/data/*.parquet")) == data_before
    assert set(table_dir.glob("sales-*/metadata/*.avro")) == manifests_before

    loaded = catalog.load_table(principal, ref)
    assert loaded.metadata.current_snapshot_id == first.snapshot_id
    file_io = StorageResolver(session).for_environment("dev").file_io
    paths = list_data_files(loaded.metadata, file_io)
    rows = _read_rows(paths)
    assert rows.column("id").to_pylist() == [1]


def test_append_failure_rolls_back_the_pending_audit_row(
    session: Session, catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A commit_table failure rolls back the pending TABLE_APPENDED row, not just the pointer.

    Regression: `commit_table`'s own `_find_table` select
    autoflushes the pending audit insert before the CAS itself fails, so without an explicit
    rollback the row would survive in the open transaction and land falsely on whatever this
    session next commits.
    """
    ref = _prepared_sales_ref(catalog, principal, tmp_path)

    with (
        mock.patch.object(
            service_module, "commit_metadata_pointer", side_effect=ConflictError("lost the race")
        ),
        pytest.raises(ConflictError),
    ):
        catalog.append(principal, ref, _sales_data([1], ["eu"]))

    session.commit()
    events = (
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.TABLE_APPENDED)
        )
        .scalars()
        .all()
    )
    assert events == []


def test_append_non_conflict_failure_also_cleans_up(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Cleanup and rollback happen on any commit_table failure, not only ConflictError."""
    ref = _prepared_sales_ref(catalog, principal, tmp_path)

    with (
        mock.patch.object(catalog, "commit_table", side_effect=ValidationError("guard refused")),
        pytest.raises(ValidationError, match="guard refused"),
    ):
        catalog.append(principal, ref, _sales_data([1], ["eu"]))

    written = list((tmp_path / "managed" / "bronze").glob("sales-*/data/*.parquet"))
    assert written == []


def test_append_cleanup_failure_does_not_mask_the_original_error(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A file-deletion failure during cleanup is swallowed; the original error still propagates."""
    ref = _prepared_sales_ref(catalog, principal, tmp_path)

    with (
        mock.patch.object(catalog, "commit_table", side_effect=ConflictError("lost the race")),
        mock.patch.object(FsspecStorage, "delete", side_effect=RuntimeError("disk on fire")),
        pytest.raises(ConflictError),
    ):
        catalog.append(principal, ref, _sales_data([1], ["eu"]))


def test_append_listing_failure_before_commit_aborts_cleanly(
    session: Session, catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A failure listing the transaction's own manifests surfaces as-is, nothing committed.

    Regression: this listing used to happen inside the
    `except` block, so a failure reading the manifests back (the same storage outage that
    may also go on to fail the commit) would have replaced the caller's real error with this
    one, and no data file would ever have been deleted (reproduced: a caller got the wrong
    error class, with a leaked data file on disk). Listing now happens eagerly, before the
    commit is even attempted, so this failure surfaces directly and nothing is left
    inconsistent: no audit row, no pointer move.
    """
    ref = _prepared_sales_ref(catalog, principal, tmp_path)
    before = catalog.load_table(principal, ref)

    with (
        mock.patch.object(
            service_module, "new_manifest_paths", side_effect=OSError("storage outage")
        ),
        pytest.raises(OSError, match="storage outage"),
    ):
        catalog.append(principal, ref, _sales_data([1], ["eu"]))

    after = catalog.load_table(principal, ref)
    assert after.metadata_location == before.metadata_location
    events = (
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.TABLE_APPENDED)
        )
        .scalars()
        .all()
    )
    assert events == []


def test_append_conflict_deletes_data_files_even_if_a_manifest_delete_fails(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A manifest-delete failure during cleanup never stops the data-file delete from running."""
    ref = _prepared_sales_ref(catalog, principal, tmp_path)
    real_delete = FsspecStorage.delete

    def _flaky_delete(self: FsspecStorage, path: str) -> None:
        if "/metadata/" in path:
            raise RuntimeError("disk on fire")
        real_delete(self, path)

    with (
        mock.patch.object(
            service_module, "commit_metadata_pointer", side_effect=ConflictError("lost the race")
        ),
        mock.patch.object(FsspecStorage, "delete", _flaky_delete),
        pytest.raises(ConflictError),
    ):
        catalog.append(principal, ref, _sales_data([1], ["eu"]))

    written = list((tmp_path / "managed" / "bronze").glob("sales-*/data/*.parquet"))
    assert written == []


def test_append_rollback_failure_does_not_mask_the_original_error(
    session: Session, catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A failing rollback after a lost commit race is logged, not raised; cleanup still ran."""
    ref = _prepared_sales_ref(catalog, principal, tmp_path)

    with (
        mock.patch.object(catalog, "commit_table", side_effect=ConflictError("lost the race")),
        mock.patch.object(session, "rollback", side_effect=RuntimeError("connection lost")),
        pytest.raises(ConflictError),
    ):
        catalog.append(principal, ref, _sales_data([1], ["eu"]))

    written = list((tmp_path / "managed" / "bronze").glob("sales-*/data/*.parquet"))
    assert written == []


def test_append_accepts_non_nullable_arrow_for_an_optional_column(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A non-nullable Arrow column may satisfy an optional column; only the reverse is refused."""
    ref = _prepared_sales_ref(catalog, principal, tmp_path)
    schema = pa.schema(
        [
            pa.field("id", pa.int64(), nullable=False),
            pa.field("region", pa.string(), nullable=False),
        ]
    )
    data = pa.table({"id": [1], "region": ["eu"]}, schema=schema)

    result = catalog.append(principal, ref, data)

    assert result.rows == 1


def test_types_compatible_treats_binary_and_large_binary_as_the_same_type() -> None:
    """`binary` and `large_binary` are accepted as the same Iceberg type; other types are not."""
    assert _types_compatible(pa.large_binary(), pa.binary())
    assert _types_compatible(pa.binary(), pa.large_binary())
    assert not _types_compatible(pa.large_binary(), pa.int64())


def test_types_compatible_treats_pyicebergs_utc_aliases_as_the_same_timestamptz() -> None:
    """A timestamptz's timezone is compared against pyiceberg's own UTC_ALIASES, not one string."""
    expected = pa.timestamp("us", tz="UTC")
    for alias in ("UTC", "+00:00", "Etc/UTC", "Z"):
        assert _types_compatible(expected, pa.timestamp("us", tz=alias))
    assert not _types_compatible(expected, pa.timestamp("us"))
    assert not _types_compatible(expected, pa.timestamp("us", tz="America/New_York"))
    assert not _types_compatible(pa.timestamp("us"), pa.timestamp("us", tz="UTC"))
    assert _types_compatible(pa.timestamp("us"), pa.timestamp("us"))
    assert not _types_compatible(expected, pa.timestamp("ms", tz="UTC"))


def test_root_relative_path_returns_none_outside_root() -> None:
    """A path outside the storage root is left for the caller to skip."""
    assert root_relative_path("file:///env", "file:///other/data/x.parquet") is None
    assert root_relative_path("file:///env", "file:///env/data/x.parquet") == "data/x.parquet"
