"""The exit test: a row filter and a column mask both take effect end to end.

Postgres and SeaweedFS via testcontainers, mirroring
`test_worker_lifecycle_integration.py`'s own pattern exactly. Uses
`GrantAuthorization`, not `AllowAllAuthorization`, so the real grant and
policy resolution path is exercised — not just the pipeline's mechanics.

The two policies are created **through the API**, as the exit criterion
requires, not by calling `RowFilterService`/`ColumnMaskService` directly:
`TestClient(create_app(settings))` runs the real FastAPI app against this
same Postgres database, and the two `POST`s carry the bootstrap admin's PAT,
so `get_principal`'s real credential verification and the services' real
`Action.MANAGE_POLICY` check both run before anything is stored. That is the
seam this test exists to close — "created over the wire" and "actually takes
effect" proven in one run rather than in two tests that never meet. An
in-process `TestClient` rather than the live uvicorn thread the earlier
exit-criterion tests use: those need a real socket because their client is
an external library (`pyiceberg`'s `RestCatalog`, `requests`) that cannot
reach an ASGI app in-process. Nothing here has that constraint, and
`TestClient` issues a full request through the same app, routers,
dependencies, and exception handlers `create_app` gives production.
"""

import io

import pyarrow as pa
import pyarrow.ipc
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer
from worker_test_helpers import SeaweedFsContainer

from lakana_core.auth import AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.bootstrap import ensure_bootstrap_admin_grant, ensure_bootstrap_principal
from lakana_core.catalog.query_service import QueryService
from lakana_core.catalog.service import CatalogService
from lakana_core.config import DatabaseConfig, StorageConfig
from lakana_core.db import create_engine
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.identity.pats import PatService
from lakana_core.identity.principals import PrincipalService
from lakana_core.models import Base, PrincipalType, QueryRunStatus
from lakana_core.storage.resolver import StorageResolver
from lakana_server.app import create_app
from lakana_server.settings import Settings
from lakana_worker.pipeline import run_one

pytestmark = pytest.mark.integration

_PAT_KEY_ID = "exitcriterion"
_PAT_SECRET = "faketoken"
_TOKEN = f"lakana_pat_{_PAT_KEY_ID}_{_PAT_SECRET}"
"""The bootstrap admin's PAT, minted deterministically by the app's own
lifespan from `Settings.bootstrap_pat_key_id`/`bootstrap_pat_secret` —
matching `lakana-server`'s own integration tests."""


def _sample_customers_parquet_bytes() -> bytes:
    """Serialize a small customers table (including a sensitive ssn column) to Parquet bytes.

    Returns
    -------
    bytes
        A valid Parquet file.
    """
    buffer = io.BytesIO()
    pq.write_table(
        pa.table(
            {
                "id": [1, 2, 3],
                "name": ["Alice", "Bob", "Carol"],
                "ssn": ["111-11-1111", "222-22-2222", "333-33-3333"],
                "region": ["EU", "US", "EU"],
            }
        ),
        buffer,
    )
    return buffer.getvalue()


def test_a_row_filter_and_a_column_mask_both_take_effect_end_to_end() -> None:
    """A join across a nested CTE comes out filtered and masked, for API-created policies."""
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
            admin = ensure_bootstrap_principal(session, "admin")
            ensure_bootstrap_admin_grant(session, admin, AllowAllAuthorization())
            # The admin already has an active grant by the time the app
            # below starts, so `bootstrap_if_needed` finds an admin
            # and mints nothing -- this test mints the deterministic PAT
            # itself instead of relying on the app's lifespan to do it.
            PatService(session, AllowAllAuthorization()).issue(
                admin, admin.name, name="bootstrap", key_id=_PAT_KEY_ID, secret=_PAT_SECRET
            )
            catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
            catalog.create_environment(
                admin,
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
            sample_bytes = _sample_customers_parquet_bytes()
            client.put_object(
                "lakana",
                "dev/raw/customers/a.parquet",
                io.BytesIO(sample_bytes),
                length=len(sample_bytes),
            )
            # create_environment already creates the bronze/silver/gold
            # namespaces (see its docstring); an explicit create_namespace
            # call here would raise AlreadyExistsError.
            ref = TableRef(
                environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="customers"
            )
            catalog.create_external_table(
                admin, ref, location_pattern="raw/customers/*.parquet", format="parquet"
            )

            principals = PrincipalService(session, AllowAllAuthorization())
            alice = principals.create_principal(
                admin, name="alice", type=PrincipalType.SERVICE_ACCOUNT
            )
            alice_ref = PrincipalRef(id=alice.id, name=alice.name, type=alice.type)

            grants = GrantService(session, AllowAllAuthorization())
            grants.grant(
                admin,
                principal="alice",
                role=Role.ANALYST,
                kind=SecurableKind.ENVIRONMENT,
                environment="dev",
            )
            # Both policies are created over the wire, by the bootstrap
            # admin, against the real app pointed at this same database —
            # every service call above has already committed, so the app's
            # own connection sees the environment, the table, and alice.
            settings = Settings(
                database=DatabaseConfig(url=pg.get_connection_url()),
                bootstrap_pat_key_id=_PAT_KEY_ID,
                bootstrap_pat_secret=_PAT_SECRET,
            )
            with TestClient(create_app(settings)) as api:
                headers = {"Authorization": f"Bearer {_TOKEN}"}
                created_filter = api.post(
                    "/api/v1/row-filters",
                    headers=headers,
                    json={
                        "principal": "alice",
                        "environment": "dev",
                        "namespace": "gold",
                        "table": "customers",
                        "expression": "region = 'EU'",
                    },
                )
                assert created_filter.status_code == 201, created_filter.text
                created_mask = api.post(
                    "/api/v1/column-masks",
                    headers=headers,
                    json={
                        "principal": "alice",
                        "environment": "dev",
                        "namespace": "gold",
                        "table": "customers",
                        "column": "ssn",
                        "expression": "NULL",
                    },
                )
                assert created_mask.status_code == 201, created_mask.text

            query_service = QueryService(session, GrantAuthorization(session))
            run = query_service.submit_query(
                alice_ref,
                "dev",
                "WITH c AS (SELECT * FROM gold.customers) "
                "SELECT a.id, a.name, a.ssn, a.region "
                "FROM c a JOIN c b ON a.id = b.id",
            )

            run_one(session, run.id, GrantAuthorization(session))

            session.refresh(run)
            assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
            assert run.result_location is not None
            raw = StorageResolver(session).for_environment("dev").storage.read(run.result_location)
            with pyarrow.ipc.open_file(io.BytesIO(raw)) as reader:
                result = reader.read_all()

            assert result.column("ssn").to_pylist() == [None, None]
            assert set(result.column("region").to_pylist()) == {"EU"}
            assert set(result.column("name").to_pylist()) == {"Alice", "Carol"}

        engine.dispose()
