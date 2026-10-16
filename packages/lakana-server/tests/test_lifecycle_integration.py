"""The exit test: an external, unmodified pyiceberg client reads a table.

The table is read from Lakana's Iceberg REST catalog over real HTTP. Real
Postgres and SeaweedFS via testcontainers, a live uvicorn server in a
background thread (pyiceberg's RestCatalog uses `requests`, which cannot
reach an in-process TestClient), and an unmodified
`pyiceberg.catalog.rest.RestCatalog` client. If this passes, M1's epic
exit criterion is met and M1 is done.
"""

import socket
import threading
import time
import uuid

import pytest
import requests
import uvicorn
from minio.error import S3Error
from pyiceberg.catalog.rest import RestCatalog
from pyiceberg.exceptions import CommitFailedException, NamespaceNotEmptyError
from pyiceberg.schema import Schema
from pyiceberg.table.update import AssertTableUUID
from pyiceberg.types import LongType, NestedField, StringType
from testcontainers.postgres import PostgresContainer

from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.models import Base
from lakana_server.app import create_app
from lakana_server.settings import Settings
from tests.server_test_helpers import SeaweedFsContainer

pytestmark = pytest.mark.integration

_TOKEN = "lakana_pat_exitcriterion_faketoken"


def _free_port() -> int:
    """Find an available TCP port on localhost.

    Returns
    -------
    int
        A port not currently bound, suitable for uvicorn to listen on.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_an_external_pyiceberg_client_creates_and_reads_a_table() -> None:
    """The full lifecycle, driven by an unmodified pyiceberg RestCatalog."""
    with (
        PostgresContainer("postgres:16-alpine", driver="pg8000") as pg,
        SeaweedFsContainer() as seaweedfs,
    ):
        bucket_client = seaweedfs.get_client()
        bucket_client.make_bucket("lakana")
        seaweedfs_endpoint = f"http://{seaweedfs.get_config()['endpoint']}"

        settings = Settings(
            database=DatabaseConfig(url=pg.get_connection_url()),
            bootstrap_pat_key_id="exitcriterion",
            bootstrap_pat_secret="faketoken",
        )

        # The app's lifespan bootstraps the admin principal but does not run
        # migrations (that is a separate deploy step in production); create
        # the schema directly, matching the pattern in
        # `packages/lakana-server/tests/conftest.py`'s `settings` fixture.
        # Its own `try/finally` keeps this engine from leaking if anything
        # below raises before the server is even started.
        schema_engine = create_engine(settings.database)
        try:
            Base.metadata.create_all(schema_engine)
        finally:
            schema_engine.dispose()

        app = create_app(settings)
        port = _free_port()
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )

        server_errors: list[BaseException] = []

        def _run_server() -> None:
            """Run the uvicorn server, recording any exception it raises.

            `Thread.run` swallows an unhandled exception (it is only printed
            to stderr), so a server that dies during startup would otherwise
            leave `thread.is_alive()` false with no way to learn why. The
            exception is appended here instead, for the startup poll below
            to re-raise. Catches `BaseException`, not `Exception`: uvicorn
            signals a bind failure with `sys.exit(1)`, i.e. `SystemExit`,
            which `except Exception` does not catch — that used to leave
            `server_errors` empty and report only "did not start within 10
            seconds", hiding the actual cause.
            """
            try:
                server.run()
            except BaseException as exc:
                server_errors.append(exc)

        thread = threading.Thread(target=_run_server, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            if not thread.is_alive() and server_errors:
                raise server_errors[0]
            assert server.started, "uvicorn did not start within 10 seconds"

            base_url = f"http://127.0.0.1:{port}"
            headers = {"Authorization": f"Bearer {_TOKEN}"}

            created_env = requests.post(
                f"{base_url}/api/v1/environments",
                headers=headers,
                json={
                    "name": "dev",
                    "storage": {
                        "scheme": "s3",
                        "root": "s3://lakana/dev",
                        "options": {
                            "s3.endpoint": seaweedfs_endpoint,
                            "s3.access-key-id": seaweedfs.access_key,
                            "s3.secret-access-key": seaweedfs.secret_key,
                        },
                    },
                },
                timeout=10,
            )
            assert created_env.status_code == 201, created_env.text

            catalog = RestCatalog(name="lakana", uri=base_url, prefix="dev", token=_TOKEN)

            catalog.create_namespace(("gold", "sales"))
            assert catalog.list_namespaces() == [
                ("bronze",),
                ("gold",),
                ("gold", "sales"),
                ("silver",),
            ]

            schema = Schema(
                NestedField(1, "id", LongType(), required=True),
                NestedField(2, "region", StringType(), required=False),
            )
            table = catalog.create_table(("gold", "sales", "monthly"), schema=schema)
            assert table.metadata.table_uuid is not None
            assert catalog.list_tables(("gold", "sales")) == [("gold", "sales", "monthly")]

            with table.transaction() as txn:
                txn.set_properties(owner="analytics")
            reloaded = catalog.load_table(("gold", "sales", "monthly"))
            assert reloaded.metadata.properties["owner"] == "analytics"
            # Proves the read half of the exit criterion, not just the write
            # half: a server that dropped a field, flipped `required`, or
            # renumbered field ids on the write/read round trip through
            # SeaweedFS would still pass every assertion above.
            assert reloaded.schema().fields == schema.fields

            # Negative case 1: a failed requirement raises CommitFailedException (409).
            # `Catalog.commit_table(table, requirements, updates)` is the public
            # method the ordinary `table.transaction()` context manager itself
            # calls internally to actually reach the server; used directly here
            # because it is the one way to assert a requirement the caller
            # knows to be false, which the higher-level Transaction API has no
            # reason to expose in ordinary use.
            # The match pins this to the intended cause (a UUID mismatch) —
            # pyiceberg maps any 409 on the commit endpoint to
            # CommitFailedException, so an unpinned assertion would not
            # distinguish this from a genuine compare-and-swap conflict.
            with pytest.raises(CommitFailedException, match="Table UUID does not match"):
                catalog.commit_table(
                    reloaded, requirements=(AssertTableUUID(uuid=uuid.uuid4()),), updates=()
                )

            # Negative case 2: dropping a non-empty namespace is refused.
            with pytest.raises(NamespaceNotEmptyError):
                catalog.drop_namespace(("gold", "sales"))

            renamed = catalog.rename_table(
                ("gold", "sales", "monthly"), ("gold", "sales", "monthly_v2")
            )
            assert renamed.name() == ("gold", "sales", "monthly_v2")
            purged_metadata_location = renamed.metadata_location

            catalog.drop_table(("gold", "sales", "monthly_v2"), purge_requested=True)
            assert catalog.list_tables(("gold", "sales")) == []
            # `purge_requested=True` must actually remove the object, not
            # merely uncatalog it. Checked through the object store's own
            # client, the same way lakana-core's own exit criterion
            # (`test_the_full_table_lifecycle_on_postgres_and_seaweedfs`)
            # checks it, so a `file_io.delete` that silently no-ops cannot
            # satisfy this assertion.
            with pytest.raises(S3Error):
                bucket_client.stat_object(
                    "lakana", purged_metadata_location.removeprefix("s3://lakana/")
                )

            catalog.drop_namespace(("gold", "sales"))
            assert catalog.list_namespaces() == [("bronze",), ("gold",), ("silver",)]
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive(), "uvicorn thread did not stop within 10 seconds"
