"""The exit test: a multi-hop query runs, and column lineage is computed for it.

Postgres and SeaweedFS via testcontainers, mirroring lakana-core's and
lakana-server's own `test_lifecycle_integration.py` files.

Named `test_worker_lifecycle_integration.py`, not `test_lifecycle_integration.py`
as the task brief originally specified: neither `lakana-core/tests/` nor
`lakana-worker/tests/` has an `__init__.py`, so pytest's default "prepend" import
mode collects both under the same bare module name `test_lifecycle_integration`
when the whole workspace is collected together (`uv run pytest -m "not
integration"` from the repo root, exactly what CI and this task's own gate
run) — a `_pytest.pathlib.ImportPathMismatchError` at collection time, not a
runtime failure. Adding `__init__.py` to `lakana-worker/tests/` does not fix
this: it only shifts the same collision onto `conftest.py` against
`lakana-server/tests/conftest.py`, since `lakana-server/tests/` already has its
own `__init__.py` and would then share the qualified name `tests.conftest`.
Switching the workspace to `--import-mode=importlib` does not fix it either:
`lakana-core`'s existing test modules do bare `from core_test_helpers import
...`, which relies on "prepend" mode putting `lakana-core/tests/` itself on
`sys.path`. Both alternatives were verified to fail before choosing this
rename.

This file duplicates `_write_one_data_file_and_commit` from
`lakana-worker/tests/test_pipeline.py` rather than importing it from a shared
module: `core_test_helpers.py`'s own module docstring explains why a
top-level test-helper module name is unsafe to share across this repo's
packages (pytest resolves a bare module name first-wins across the whole
workspace's `sys.path`), so duplicating this one small function per test
suite is the established way around that — the same principle this file's
own name just ran into.
"""

import io
from collections.abc import Mapping

import pyarrow as pa
import pyarrow.ipc
import pyarrow.parquet as pq
import pytest
from pyiceberg.schema import Schema
from pyiceberg.table import StaticTable
from pyiceberg.types import LongType, NestedField, StringType
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer
from worker_test_helpers import SeaweedFsContainer

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, Securable
from lakana_core.bootstrap import ensure_bootstrap_principal
from lakana_core.catalog.query_service import QueryService
from lakana_core.catalog.service import CatalogService, LoadedTable
from lakana_core.config import DatabaseConfig, StorageConfig
from lakana_core.db import create_engine
from lakana_core.errors import PermissionDenied
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import Base, QueryRunStatus
from lakana_core.storage.resolver import StorageResolver
from lakana_worker.pipeline import run_one

pytestmark = pytest.mark.integration


def _write_one_data_file_and_commit(
    catalog: CatalogService,
    principal: PrincipalRef,
    ref: TableRef,
    created: LoadedTable,
    data: pa.Table,
    file_io_properties: Mapping[str, str] | None = None,
) -> LoadedTable:
    """Write one real Parquet data file and commit it as the table's current snapshot.

    Parameters
    ----------
    catalog
        The catalog under test.
    principal
        The acting principal.
    ref
        The table to write to.
    created
        The table as `create_table` returned it (a `LoadedTable`).
    data
        The rows to write. Its Arrow schema's nullability must match the
        Iceberg table's schema exactly — a required Iceberg column needs a
        non-nullable Arrow field, or pyiceberg refuses the append with a
        schema-mismatch error.
    file_io_properties
        Properties `StaticTable.from_metadata` needs to build its own
        `FileIO` — for local storage, none are needed, but a real S3
        endpoint (unlike the local filesystem) is not reachable without the
        same `s3.endpoint`/`s3.access-key-id`/`s3.secret-access-key` values
        the environment itself was configured with, since `StaticTable`
        builds an independent `FileIO` rather than reusing the catalog's.

    Returns
    -------
    LoadedTable
        The table at its new metadata location, with the write committed
        through `CatalogService.commit_table`'s normal CAS path — never
        through `StaticTable`'s own commit, which always raises
        `NotImplementedError` (it has no real catalog bound to it).
    """
    static = StaticTable.from_metadata(
        created.metadata_location, properties=dict(file_io_properties or {})
    )
    tx = static.transaction()
    tx.append(data)
    return catalog.commit_table(principal, ref, requirements=tx._requirements, updates=tx._updates)


def _sample_parquet_bytes() -> bytes:
    """Serialize a one-column Arrow table to Parquet bytes, in memory.

    Returns
    -------
    bytes
        A valid Parquet file containing a single `id` column, suitable for
        `Minio.put_object` (the `minio` SDK client works against any
        S3-compatible backend, not just one server).
    """
    buffer = io.BytesIO()
    pq.write_table(pa.table({"id": [1, 2]}), buffer)
    return buffer.getvalue()


def test_a_multi_hop_query_runs_and_produces_lineage() -> None:
    """Two real Iceberg tables, joined by a submitted query, with lineage computed."""
    with (
        PostgresContainer("postgres:16-alpine", driver="pg8000") as pg,
        SeaweedFsContainer() as seaweedfs,
    ):
        engine = create_engine(DatabaseConfig(url=pg.get_connection_url()))
        Base.metadata.create_all(engine)
        client = seaweedfs.get_client()
        client.make_bucket("lakana")
        endpoint = f"http://{seaweedfs.get_config()['endpoint']}"

        with Session(engine) as session:
            principal = ensure_bootstrap_principal(session, "admin")
            catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
            storage_options = {
                "s3.endpoint": endpoint,
                "s3.access-key-id": seaweedfs.access_key,
                "s3.secret-access-key": seaweedfs.secret_key,
            }
            catalog.create_environment(
                principal,
                name="dev",
                storage=StorageConfig(scheme="s3", root="s3://lakana/dev", options=storage_options),
            )
            orders_ref = TableRef(
                environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="orders"
            )
            orders_schema = Schema(
                NestedField(1, "id", LongType(), required=True),
                NestedField(2, "customer_id", LongType(), required=True),
            )
            orders = catalog.create_table(principal, orders_ref, schema=orders_schema)
            orders_arrow_schema = pa.schema(
                [
                    pa.field("id", pa.int64(), nullable=False),
                    pa.field("customer_id", pa.int64(), nullable=False),
                ]
            )
            _write_one_data_file_and_commit(
                catalog,
                principal,
                orders_ref,
                orders,
                pa.table({"id": [1, 2], "customer_id": [10, 11]}, schema=orders_arrow_schema),
                storage_options,
            )

            customers_ref = TableRef(
                environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="customers"
            )
            customers_schema = Schema(
                NestedField(1, "id", LongType(), required=True),
                NestedField(2, "name", StringType(), required=False),
            )
            customers = catalog.create_table(principal, customers_ref, schema=customers_schema)
            customers_arrow_schema = pa.schema(
                [pa.field("id", pa.int64(), nullable=False), pa.field("name", pa.string())]
            )
            _write_one_data_file_and_commit(
                catalog,
                principal,
                customers_ref,
                customers,
                pa.table({"id": [10, 11], "name": ["Alice", "Bob"]}, schema=customers_arrow_schema),
                storage_options,
            )

            query_service = QueryService(session, AllowAllAuthorization())
            run = query_service.submit_query(
                principal,
                "dev",
                "SELECT o.id, c.name FROM gold.orders AS o "
                "JOIN gold.customers AS c ON o.customer_id = c.id",
            )

            run_one(session, run.id, AllowAllAuthorization())

            session.refresh(run)
            assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
            assert run.result_location is not None
            raw = StorageResolver(session).for_environment("dev").storage.read(run.result_location)
            with pyarrow.ipc.open_file(io.BytesIO(raw)) as reader:
                assert reader.read_all().to_pydict() == {"id": [1, 2], "name": ["Alice", "Bob"]}
            assert run.lineage_json is not None
            assert run.lineage_json["name"] == [{"table": "gold.customers", "column": "name"}]
            assert run.lineage_json["id"] == [{"table": "gold.orders", "column": "id"}]

        engine.dispose()


def test_an_unauthorized_table_reference_fails_the_run() -> None:
    """A principal with no READ_TABLE grant on a referenced table gets a failed run, not a leak."""
    with (
        PostgresContainer("postgres:16-alpine", driver="pg8000") as pg,
        SeaweedFsContainer() as seaweedfs,
    ):
        engine = create_engine(DatabaseConfig(url=pg.get_connection_url()))
        Base.metadata.create_all(engine)
        client = seaweedfs.get_client()
        client.make_bucket("lakana")
        endpoint = f"http://{seaweedfs.get_config()['endpoint']}"

        with Session(engine) as session:
            principal = ensure_bootstrap_principal(session, "admin")
            catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
            catalog.create_environment(
                principal,
                name="dev",
                storage=StorageConfig(
                    scheme="s3",
                    root="s3://lakana/dev",
                    options={
                        "s3.endpoint": endpoint,
                        "s3.access-key-id": seaweedfs.access_key,
                        "s3.secret-access-key": seaweedfs.secret_key,
                    },
                ),
            )
            ref = TableRef(
                environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="sales"
            )
            schema = Schema(NestedField(1, "id", LongType(), required=True))
            catalog.create_table(principal, ref, schema=schema)

            query_service = QueryService(session, AllowAllAuthorization())
            run = query_service.submit_query(principal, "dev", "SELECT id FROM gold.sales")

            class _DenyRead:
                """Authorization stub that refuses every check, proving the worker enforces it."""

                def authorize(
                    self, principal: PrincipalRef, action: Action, securable: Securable
                ) -> None:
                    """Refuse unconditionally.

                    Parameters
                    ----------
                    principal
                        The acting principal. Unused.
                    action
                        The action requested. Unused.
                    securable
                        The object acted upon. Unused.

                    Raises
                    ------
                    PermissionDenied
                        Always.
                    """
                    raise PermissionDenied("denied")

                def permits(
                    self,
                    principal: PrincipalRef,
                    action: Action,
                    securable: Securable,
                ) -> bool:
                    """Answer as `authorize` would, as a boolean.

                    Parameters
                    ----------
                    principal
                        The actor requesting the action.
                    action
                        The action requested.
                    securable
                        The object acted upon.

                    Returns
                    -------
                    bool
                        `False` if `authorize` raises `PermissionDenied`, else `True`.
                    """
                    try:
                        self.authorize(principal, action, securable)
                    except PermissionDenied:
                        return False
                    return True

            run_one(session, run.id, _DenyRead())

            session.refresh(run)
            assert run.status == QueryRunStatus.FAILED
            assert run.error_message is not None

        engine.dispose()


def test_a_raw_zone_query_reads_a_registered_external_table() -> None:
    """A table registered via create_external_table is queryable through the same pipeline."""
    with (
        PostgresContainer("postgres:16-alpine", driver="pg8000") as pg,
        SeaweedFsContainer() as seaweedfs,
    ):
        engine = create_engine(DatabaseConfig(url=pg.get_connection_url()))
        Base.metadata.create_all(engine)
        client = seaweedfs.get_client()
        client.make_bucket("lakana")
        endpoint = f"http://{seaweedfs.get_config()['endpoint']}"

        with Session(engine) as session:
            principal = ensure_bootstrap_principal(session, "admin")
            catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
            catalog.create_environment(
                principal,
                name="dev",
                storage=StorageConfig(
                    scheme="s3",
                    root="s3://lakana/dev",
                    options={
                        "s3.endpoint": endpoint,
                        "s3.access-key-id": seaweedfs.access_key,
                        "s3.secret-access-key": seaweedfs.secret_key,
                    },
                ),
            )
            sample_bytes = _sample_parquet_bytes()
            client.put_object(
                "lakana",
                "dev/raw/events/a.parquet",
                io.BytesIO(sample_bytes),
                length=len(sample_bytes),
            )
            catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
            ref = TableRef(
                environment="dev", namespace=NamespacePath.from_parts(["raw"]), name="events"
            )
            catalog.create_external_table(
                principal, ref, location_pattern="raw/events/*.parquet", format="parquet"
            )

            query_service = QueryService(session, AllowAllAuthorization())
            run = query_service.submit_query(principal, "dev", "SELECT id FROM raw.events")

            run_one(session, run.id, AllowAllAuthorization())

            session.refresh(run)
            assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
            # SUCCEEDED alone proves nothing here: a table resolving to zero
            # real files also succeeds, via the empty-relation substitution
            # `rewrite_query` makes for a table with no data files. Reading
            # the result back is what proves the registered Parquet file was
            # the thing actually read.
            assert run.result_location is not None
            raw = StorageResolver(session).for_environment("dev").storage.read(run.result_location)
            with pyarrow.ipc.open_file(io.BytesIO(raw)) as reader:
                assert reader.read_all().to_pydict() == {"id": [1, 2]}

        engine.dispose()
