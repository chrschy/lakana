"""The incremental-append watermark."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pyarrow as pa
import pytest
from pyiceberg.manifest import DataFileContent
from pyiceberg.schema import Schema
from pyiceberg.types import DoubleType, IntegerType, LongType, NestedField, StringType
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.service import CatalogService
from lakana_core.catalog.watermark import (
    Watermark,
    check_incremental_key,
    compute_watermark,
    wrap_incremental,
)
from lakana_core.config import StorageConfig
from lakana_core.errors import UnsupportedTableError, ValidationError
from lakana_core.identifiers import TableRef
from lakana_core.models import Namespace, Table, TableType, WriteMode
from lakana_core.storage.resolver import StorageResolver


def _catalog(session: Session, principal: PrincipalRef, tmp_path: Path) -> CatalogService:
    """Create `dev` at `tmp_path` and return an allow-all catalog.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    tmp_path
        The storage root.

    Returns
    -------
    CatalogService
        The catalog.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    return catalog


def _watermark(
    catalog: CatalogService, principal: PrincipalRef, session: Session, key: str
) -> Watermark | None:
    """Compute `gold.events`' watermark over `key`.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.
    session
        Database session.
    key
        The incremental key.

    Returns
    -------
    Watermark or None
        The watermark.
    """
    loaded = catalog.load_table(principal, TableRef.from_dotted("dev", "gold.events"))
    file_io = StorageResolver(session).for_environment("dev").file_io
    return compute_watermark(loaded.metadata, file_io, key)


def _write(
    catalog: CatalogService, principal: PrincipalRef, mode: WriteMode, data: pa.Table
) -> None:
    """Write `data` into `gold.events`.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.
    mode
        The write mode.
    data
        The rows.
    """
    catalog.write_batches(
        principal,
        TableRef.from_dotted("dev", "gold.events"),
        mode=mode,
        schema=data.schema,
        batches=data.to_batches(),
    )


@pytest.mark.parametrize(
    "watermark",
    [
        Watermark("int", 7),
        Watermark("long", 2**62),
        Watermark("decimal", Decimal("12.50")),
        Watermark("date", dt.date(2026, 9, 29)),
        Watermark("timestamp", dt.datetime(2026, 9, 29, 10, 0, 0, 123456)),
        Watermark("timestamptz", dt.datetime(2026, 9, 29, 10, 0, tzinfo=dt.UTC)),
    ],
)
def test_a_watermark_round_trips_through_its_tagged_string(watermark: Watermark) -> None:
    """The stored form is `<tag>:<value>` and decodes to the same value."""
    encoded = watermark.encode()

    assert encoded.startswith(f"{watermark.tag}:")
    assert Watermark.decode(encoded) == watermark


def test_decode_refuses_an_unknown_tag() -> None:
    """A tampered or future tag fails loudly, never as a string comparison."""
    with pytest.raises(ValidationError):
        Watermark.decode("string:abc")


def test_the_key_must_be_a_top_level_orderable_column() -> None:
    """Missing and unorderable keys fail naming the key and the table."""
    schema = Schema(
        NestedField(1, "id", LongType(), required=False),
        NestedField(2, "name", StringType(), required=False),
        NestedField(3, "score", DoubleType(), required=False),
    )

    assert check_incremental_key(schema, "id", table_name="gold.t").name == "id"
    with pytest.raises(ValidationError, match=r"'ts' is not a column of gold\.t"):
        check_incremental_key(schema, "ts", table_name="gold.t")
    for key in ("name", "score"):
        with pytest.raises(ValidationError, match="must be an int, long"):
            check_incremental_key(schema, key, table_name="gold.t")


def test_wrap_incremental_quotes_the_key_and_reads_the_watermark_as_a_variable() -> None:
    """The key is a quoted identifier and the watermark a variable, never text or a placeholder."""
    wrapped = wrap_incremental('SELECT 1 AS "my ""key"""', 'my "key"')

    assert wrapped.parameter == "v_watermark"
    assert wrapped.sql == (
        'SELECT * FROM (SELECT 1 AS "my ""key""") AS __lakana_incremental '
        'WHERE "my ""key""" > getvariable(\'v_watermark\')'
    )
    assert "?" not in wrapped.sql
    assert "$" not in wrapped.sql


def test_an_empty_table_has_no_watermark(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Spec: with no watermark, nothing is wrapped."""
    catalog = _catalog(session, principal, tmp_path)
    _write(catalog, principal, WriteMode.CREATE, pa.table({"id": pa.array([], pa.int64())}))

    assert _watermark(catalog, principal, session, "id") is None


def test_the_watermark_is_the_max_over_every_live_file(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Upper bounds across several files give the table's max."""
    catalog = _catalog(session, principal, tmp_path)
    _write(catalog, principal, WriteMode.CREATE, pa.table({"id": pa.array([5, 3], pa.int64())}))
    _write(catalog, principal, WriteMode.APPEND, pa.table({"id": pa.array([9, 1], pa.int64())}))

    assert _watermark(catalog, principal, session, "id") == Watermark("long", 9)


def test_an_all_null_key_has_no_watermark(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Spec: the key all NULL means no watermark."""
    catalog = _catalog(session, principal, tmp_path)
    _write(
        catalog, principal, WriteMode.CREATE, pa.table({"id": pa.array([None, None], pa.int64())})
    )

    assert _watermark(catalog, principal, session, "id") is None


@pytest.mark.parametrize(
    ("column", "expected"),
    [
        (
            pa.array([dt.date(2026, 1, 1), dt.date(2026, 1, 3)]),
            Watermark("date", dt.date(2026, 1, 3)),
        ),
        (
            pa.array([dt.datetime(2026, 1, 1), dt.datetime(2026, 1, 2, 3)], pa.timestamp("us")),
            Watermark("timestamp", dt.datetime(2026, 1, 2, 3)),
        ),
        (
            pa.array([dt.datetime(2026, 1, 2, 3, tzinfo=dt.UTC)], pa.timestamp("us", tz="UTC")),
            Watermark("timestamptz", dt.datetime(2026, 1, 2, 3, tzinfo=dt.UTC)),
        ),
        (
            pa.array([Decimal("1.50"), Decimal("12.25")], pa.decimal128(10, 2)),
            Watermark("decimal", Decimal("12.25")),
        ),
    ],
)
def test_date_timestamp_and_decimal_keys_decode_to_python_values(
    session: Session,
    principal: PrincipalRef,
    tmp_path: Path,
    column: pa.Array,
    expected: Watermark,
) -> None:
    """Bounds decode to the values DuckDB compares against (a variable)."""
    catalog = _catalog(session, principal, tmp_path)
    _write(catalog, principal, WriteMode.CREATE, pa.table({"id": column}))

    assert _watermark(catalog, principal, session, "id") == expected


def test_check_incremental_key_recognizes_an_int_typed_column() -> None:
    """The `int` (Iceberg `IntegerType`) key type is accepted.

    `IntegerType` is never produced by a batch's Arrow schema in this
    module's other tests (which all use `int64`, mapping to `LongType`), so
    this exercises `_tag`'s `int` branch directly against a hand-built
    schema.
    """
    schema = Schema(NestedField(1, "id", IntegerType(), required=False))

    assert check_incremental_key(schema, "id", table_name="gold.t").name == "id"


def test_compute_watermark_refuses_a_table_with_delete_files() -> None:
    """A live row-level delete file makes the watermark unreadable.

    Mirrors `test_live_data_files_refuses_a_table_with_delete_files`
    (`lakana_core.catalog.writes`) for `compute_watermark`'s own manifest walk.
    """
    schema = Schema(NestedField(1, "id", LongType(), required=False))
    fake_entry = mock.Mock()
    fake_entry.data_file.content = DataFileContent.POSITION_DELETES
    fake_manifest = mock.Mock()
    fake_manifest.fetch_manifest_entry.return_value = [fake_entry]
    fake_snapshot = mock.Mock()
    fake_snapshot.manifests.return_value = [fake_manifest]
    fake_metadata = mock.Mock()
    fake_metadata.schema.return_value = schema
    fake_metadata.current_snapshot.return_value = fake_snapshot

    with pytest.raises(UnsupportedTableError, match="row-level delete"):
        compute_watermark(fake_metadata, mock.Mock(), "id")


def test_compute_watermark_fails_when_a_file_has_values_but_no_recorded_bound() -> None:
    """Column metrics disabled: a file has non-null values but no upper bound."""
    schema = Schema(NestedField(1, "id", LongType(), required=False))
    fake_entry = mock.Mock()
    fake_entry.data_file.content = DataFileContent.DATA
    fake_entry.data_file.upper_bounds = {}
    fake_entry.data_file.value_counts = {1: 5}
    fake_entry.data_file.null_value_counts = {1: 0}
    fake_manifest = mock.Mock()
    fake_manifest.fetch_manifest_entry.return_value = [fake_entry]
    fake_snapshot = mock.Mock()
    fake_snapshot.manifests.return_value = [fake_manifest]
    fake_metadata = mock.Mock()
    fake_metadata.schema.return_value = schema
    fake_metadata.current_snapshot.return_value = fake_snapshot

    with pytest.raises(ValidationError, match="no statistics"):
        compute_watermark(fake_metadata, mock.Mock(), "id")


def test_read_watermark_refuses_a_target_that_is_not_a_managed_table(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Defence in depth: `submit_write` already refuses this at submission."""
    catalog = _catalog(session, principal, tmp_path)
    namespace = session.execute(select(Namespace).where(Namespace.path == "gold")).scalar_one()
    session.add(
        Table(id=uuid.uuid4(), namespace_id=namespace.id, name="v", table_type=TableType.VIEW)
    )
    session.commit()

    with pytest.raises(ValidationError, match="not a managed table"):
        catalog.read_watermark(principal, TableRef.from_dotted("dev", "gold.v"), "id")
