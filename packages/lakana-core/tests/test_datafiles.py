"""Tests for listing an Iceberg table's current data files."""

from pathlib import Path

from pyiceberg.schema import Schema
from pyiceberg.types import LongType, NestedField
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization
from lakana_core.bootstrap import ensure_bootstrap_principal
from lakana_core.catalog.datafiles import list_data_files
from lakana_core.catalog.service import CatalogService
from lakana_core.config import StorageConfig
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.storage.resolver import StorageResolver


def test_a_table_with_no_snapshot_has_no_data_files(tmp_path: Path, session: Session) -> None:
    """A table that has never been written to has no data to list."""
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    # Persisted, not a bare `PrincipalRef`:
    # `create_environment` now records an `AuditEvent` whose `principal_id`
    # is a foreign key onto `principals.id`, enforced even though nullable.
    principal = ensure_bootstrap_principal(session, "admin")
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="sales")
    created = catalog.create_table(
        principal, ref, schema=Schema(NestedField(1, "id", LongType(), required=True))
    )
    resolved = StorageResolver(session).for_environment("dev")

    assert list_data_files(created.metadata, resolved.file_io) == []
