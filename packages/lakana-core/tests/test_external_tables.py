"""Tests for raw-zone external table registration."""

from pathlib import Path
from unittest import mock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from core_test_helpers import _config
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.catalog.service import CatalogService
from lakana_core.errors import AlreadyExistsError, ConflictError, NotFoundError, ValidationError
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import TableType
from lakana_core.storage.fsspec_storage import FsspecStorage


def test_create_external_table_infers_schema_from_a_sample_file(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Registering a raw-zone table reads a sample file's schema, not a caller-declared one."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    (tmp_path / "raw" / "events").mkdir(parents=True)
    pq.write_table(
        pa.table({"id": [1, 2], "name": ["a", "b"]}),
        tmp_path / "raw" / "events" / "part-0.parquet",
    )
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["raw"]), name="events")

    table = catalog.create_external_table(
        principal, ref, location_pattern="raw/events/*.parquet", format="parquet"
    )

    assert table.table_type == TableType.EXTERNAL
    assert table.external_config is not None
    assert table.external_config["location_pattern"] == "raw/events/*.parquet"
    assert table.external_config["format"] == "parquet"
    assert set(table.external_config["schema"]) == {"id", "name"}


def test_create_external_table_is_not_restricted_to_the_medallion_namespaces(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Raw is exempt from the naming rule create_table enforces on ICEBERG tables.

    The catalog *namespace* a table is registered under (``landing`` here,
    not one of `bronze`/`silver`/`gold`) is independent of the *storage*
    zone its `location_pattern` claims, which is always confined under
    `raw/`.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["landing"]))
    (tmp_path / "raw" / "landing").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), tmp_path / "raw" / "landing" / "a.parquet")
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["landing"]), name="a")

    table = catalog.create_external_table(
        principal, ref, location_pattern="raw/landing/*.parquet", format="parquet"
    )

    assert table.table_type == TableType.EXTERNAL


def test_create_external_table_confines_the_location_to_the_storage_root(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A location pattern escaping the environment's storage root is refused."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["raw"]), name="evil")

    with pytest.raises(ValidationError):
        catalog.create_external_table(
            principal, ref, location_pattern="../../etc/*.parquet", format="parquet"
        )


def test_create_external_table_raises_not_found_when_no_file_matches_the_pattern(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A syntactically valid glob that matches no file is a 404, not an empty schema."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    (tmp_path / "raw" / "events").mkdir(parents=True)
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["raw"]), name="events")

    with pytest.raises(NotFoundError):
        catalog.create_external_table(
            principal, ref, location_pattern="raw/events/*.parquet", format="parquet"
        )


def test_create_external_table_raises_already_exists_for_a_duplicate_name(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Registering the same name twice is refused by the ordinary pre-existence check.

    Unlike `test_create_external_table_race_on_the_unique_constraint_raises_already_exists`,
    which forces the race path by patching `_lookup_table` to miss an existing row,
    this exercises the direct check with no patching involved.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    (tmp_path / "raw" / "events").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), tmp_path / "raw" / "events" / "part-0.parquet")
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["raw"]), name="events")
    catalog.create_external_table(
        principal, ref, location_pattern="raw/events/*.parquet", format="parquet"
    )

    with pytest.raises(AlreadyExistsError):
        catalog.create_external_table(
            principal, ref, location_pattern="raw/events/*.parquet", format="parquet"
        )


def test_create_external_table_over_another_tables_claim_does_no_storage_io(
    catalog: CatalogService, principal: PrincipalRef, session: Session, tmp_path: Path
) -> None:
    """A create over another table's claim is refused before any storage I/O.

    Before this fix, `create_external_table` listed the claimed directory
    and read a sample file from it *before* ever checking whether the
    claim was already taken — an existence oracle (a 404 "no file matches"
    versus a 422 "claim taken" tells an unprivileged caller whether a
    directory exists at all) and a cross-table read (the sample file read
    to infer schema would be another table's own data, read before the
    caller is ever confirmed to have a claim of their own). `check_claim_free`
    now runs, unlocked, before any storage access; a locked re-check still
    runs immediately before the insert. Spies on `FsspecStorage.list`/
    `.read` (rather than counting bytes on disk) to prove no I/O happens
    at all, not just that it happens to find nothing.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    (tmp_path / "raw" / "customers").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), tmp_path / "raw" / "customers" / "a.parquet")
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["raw"]), name="customers")
    catalog.create_external_table(
        principal, ref, location_pattern="raw/customers/*.parquet", format="parquet"
    )
    other_ref = TableRef(
        environment="dev", namespace=NamespacePath.from_parts(["raw"]), name="loot"
    )

    with (
        mock.patch.object(FsspecStorage, "list", side_effect=AssertionError("must not list")),
        mock.patch.object(FsspecStorage, "read", side_effect=AssertionError("must not read")),
        pytest.raises(ValidationError) as exc_info,
    ):
        catalog.create_external_table(
            principal, other_ref, location_pattern="raw/customers/*.parquet", format="parquet"
        )

    assert str(exc_info.value) == "That directory is already claimed by another table."
    assert "customers" not in str(exc_info.value)


def test_create_external_table_race_on_the_unique_constraint_raises_already_exists(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A concurrent writer that wins the insert race is still refused.

    The pre-existence check only produces a good error message in the
    common case; the ``(namespace_id, name)`` unique constraint is what
    actually arbitrates a genuine race. A real concurrent writer cannot be
    exercised against a single in-process session, so the race is simulated
    by patching the pre-check to miss the row that is, in fact, already
    there, forcing the insert itself to hit the constraint. The second call
    claims a disjoint `raw/` directory (`events-retry`, not `events`) so
    `check_claim_free` never fires — the constraint under test
    here is the name's, not the claim's.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    (tmp_path / "raw" / "events").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), tmp_path / "raw" / "events" / "part-0.parquet")
    (tmp_path / "raw" / "events-retry").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), tmp_path / "raw" / "events-retry" / "part-0.parquet")
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["raw"]), name="events")
    catalog.create_external_table(
        principal, ref, location_pattern="raw/events/*.parquet", format="parquet"
    )

    with (
        mock.patch.object(CatalogService, "_lookup_table", return_value=None),
        pytest.raises(AlreadyExistsError),
    ):
        catalog.create_external_table(
            principal, ref, location_pattern="raw/events-retry/*.parquet", format="parquet"
        )


def test_create_external_table_reports_conflict_when_the_integrity_error_is_not_a_name_collision(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """An `IntegrityError` that is not a name collision must not be misreported as one.

    Dropping the namespace from another session between the pre-check and
    the insert is reachable in production — `drop_namespace` permits
    dropping a namespace that is empty at that instant — and manifests as a
    foreign-key violation, not a unique-constraint violation. A handler
    that assumes every `IntegrityError` here means "this name is taken"
    would tell a caller (or a REST layer mapping this to 409) something
    false. This does not attempt to reproduce the exact foreign-key race;
    it proves the classifying logic itself, by making `commit` fail for an
    unrelated reason while no conflicting row actually exists.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    (tmp_path / "raw" / "events").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), tmp_path / "raw" / "events" / "part-0.parquet")
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["raw"]), name="events")

    def _fail(self: Session) -> None:
        raise IntegrityError(
            "INSERT INTO tables (id, name) VALUES (?, ?)",
            {"id": "secret-uuid", "name": "events"},
            Exception("FOREIGN KEY constraint failed"),
        )

    with (
        mock.patch.object(Session, "commit", _fail),
        pytest.raises(ConflictError) as excinfo,
    ):
        catalog.create_external_table(
            principal, ref, location_pattern="raw/events/*.parquet", format="parquet"
        )

    assert catalog.table_exists(principal, ref) is False
    # The statement and its bound parameters must not leak into the
    # message a caller (eventually a REST client) sees.
    assert "INSERT INTO" not in str(excinfo.value)
    assert "secret-uuid" not in str(excinfo.value)


def test_create_external_table_rejects_a_dotted_name(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A dotted table name is refused for external tables too."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["raw"]), name="a.b")

    with pytest.raises(ValidationError, match=r"'a\.b'"):
        catalog.create_external_table(
            principal, ref, location_pattern="raw/events/*.parquet", format="parquet"
        )
