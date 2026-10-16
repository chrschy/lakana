"""SDK round trip against real Postgres and SeaweedFS.

Every other SDK end-to-end test in this directory runs against SQLite and
`file://` storage (see `conftest.py`'s `lakana_client` fixture and
`test_sdk_data_plane.py`). This test instead stands up a real
`postgres:16-alpine` and a real SeaweedFS S3 gateway via testcontainers —
matching `test_lifecycle_integration.py`'s pattern — to prove the SDK's
create/append/query round trip also works against the production-shaped
catalog and object-storage backends, not only the fast unit-tier doubles.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

import lakana
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.models import Base
from lakana_server.app import create_app
from lakana_server.settings import Settings
from lakana_worker.pipeline import run_one
from tests.server_test_helpers import SeaweedFsContainer

pytestmark = pytest.mark.integration


def test_sdk_round_trip_against_postgres_and_seaweedfs() -> None:
    """Create an environment/table, append, query, and fetch a matching table.

    Drives the platform API only through `lakana.Client`, over the app's own
    `TestClient` (no real network hop needed — the SDK's HTTP transport
    accepts any `httpx.Client`-compatible object, and `TestClient` is one).
    A query is run to completion in-process via `lakana_worker.pipeline.
    run_one`, matching every other SDK end-to-end test's pattern.
    """
    with (
        PostgresContainer("postgres:16-alpine", driver="pg8000") as pg,
        SeaweedFsContainer() as seaweedfs,
    ):
        bucket_client = seaweedfs.get_client()
        bucket_client.make_bucket("lakana")
        seaweedfs_endpoint = f"http://{seaweedfs.get_config()['endpoint']}"

        settings = Settings(
            database=DatabaseConfig(url=pg.get_connection_url()),
            bootstrap_pat_key_id="sdkintegration",
            bootstrap_pat_secret="sdkintegrationsecret",
        )

        # The app's lifespan bootstraps the admin principal but does not run
        # migrations (a separate deploy step in production); create the
        # schema directly, matching `test_lifecycle_integration.py`.
        schema_engine = create_engine(settings.database)
        Base.metadata.create_all(schema_engine)
        schema_engine.dispose()

        with (
            TestClient(create_app(settings)) as client,
            lakana.Client(
                url="http://testserver",
                token="lakana_pat_sdkintegration_sdkintegrationsecret",
                http_client=client,
            ) as sdk_client,
        ):
            sdk_client.environments.create(
                "dev",
                storage={
                    "scheme": "s3",
                    "root": "s3://lakana/dev",
                    "options": {
                        "s3.endpoint": seaweedfs_endpoint,
                        "s3.access-key-id": seaweedfs.access_key,
                        "s3.secret-access-key": seaweedfs.secret_key,
                    },
                },
            )
            schema = pa.schema(
                [
                    pa.field("id", pa.int64(), nullable=False),
                    pa.field("region", pa.string()),
                ]
            )
            sdk_client.tables.create("dev", "bronze.sales", schema=schema)
            data = pa.table({"id": [1, 2, 3], "region": ["eu", "us", "eu"]}, schema=schema)

            appended = sdk_client.tables.append("dev", "bronze.sales", data)
            assert appended.rows == 3

            run = sdk_client.queries.submit(
                "SELECT id, region FROM bronze.sales ORDER BY id", environment="dev"
            )

            engine = client.app.state.engine
            with Session(engine) as session:
                run_one(session, uuid.UUID(run.id), GrantAuthorization(session))

            result = run.result()
            # Checked before any cast, so a type-mapping regression on the
            # real Postgres/SeaweedFS path (e.g. a column silently coming
            # back as the wrong Arrow type) can't hide behind the cast
            # below — matching test_sdk_data_plane.py's own pattern.
            assert result.schema.field("id").type == pa.int64()
            assert result.schema.field("region").type == pa.string()
            assert result.cast(data.schema).equals(data)

            # Keyset paging on Postgres's own timestamptz/uuid ordering:
            # one event per page must still visit every event exactly once.
            # Every request records its own authentication event, so `until`
            # freezes the set being compared.
            cutoff = datetime.now(UTC)
            everything = sdk_client.audit_events.list(until=cutoff, limit=500)
            assert everything.next_cursor is None
            paged = list(sdk_client.audit_events.iter(until=cutoff, page_size=1))
            assert [e.id for e in paged] == [e.id for e in everything.items]
            assert len(paged) > 3
