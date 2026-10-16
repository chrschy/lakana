"""The exit test: writes, views, lineage and maintenance on Postgres and SeaweedFS.

Drives the platform through `lakana.Client` and the `lakana` CLI, over the app's
own `TestClient`, matching `test_sdk_integration.py` and
`test_cli_integration.py`. Runs are executed in-process by `run_one` (the SDK
test) or a background `poll_once` loop (the CLI test). The orphan-removal
check calls `CatalogService.maintain` directly with a later `now` and the
minimum `older_than_hours=1`: an object store's modification times can't be
aged from outside, and the 72-hour default would keep every fresh file.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer
from typer.testing import CliRunner, Result

import lakana
from lakana.cli._state import CliState
from lakana.cli.app import build_app
from lakana.resources.queries import QueryRun
from lakana_core.auth import AllowAllAuthorization
from lakana_core.bootstrap import ensure_bootstrap_principal
from lakana_core.catalog.service import CatalogService
from lakana_core.config import DatabaseConfig, StorageConfig
from lakana_core.db import create_engine
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.identifiers import TableRef
from lakana_core.models import Base, WriteMode
from lakana_core.storage.resolver import StorageResolver
from lakana_server.app import create_app
from lakana_server.settings import Settings
from lakana_worker.pipeline import run_one
from lakana_worker.poller import poll_once
from tests.cli.conftest import make_cli_state
from tests.server_test_helpers import SeaweedFsContainer

pytestmark = pytest.mark.integration

_TOKEN = "lakana_pat_m5aintegration_m5aintegrationsecret"


@dataclass(frozen=True)
class _Stack:
    """A running Postgres and SeaweedFS, and settings pointing at them.

    Parameters
    ----------
    settings
        Server settings.
    endpoint
        SeaweedFS's S3 endpoint URL.
    access_key
        Its access key.
    secret_key
        Its secret key.
    """

    settings: Settings
    endpoint: str
    access_key: str
    secret_key: str

    def options(self) -> dict[str, str]:
        """Build the S3 storage options every environment here uses.

        Returns
        -------
        dict
            `s3.endpoint` and the credentials.
        """
        return {
            "s3.endpoint": self.endpoint,
            "s3.access-key-id": self.access_key,
            "s3.secret-access-key": self.secret_key,
        }


@pytest.fixture(scope="module")
def stack() -> Iterator[_Stack]:
    """Start Postgres and SeaweedFS once for this module.

    Yields
    ------
    _Stack
        The running services.
    """
    with (
        PostgresContainer("postgres:16-alpine", driver="pg8000") as pg,
        SeaweedFsContainer() as seaweedfs,
    ):
        seaweedfs.get_client().make_bucket("lakana")
        settings = Settings(
            database=DatabaseConfig(url=pg.get_connection_url()),
            bootstrap_pat_key_id="m5aintegration",
            bootstrap_pat_secret="m5aintegrationsecret",
        )
        engine = create_engine(settings.database)
        Base.metadata.create_all(engine)
        engine.dispose()
        yield _Stack(
            settings=settings,
            endpoint=f"http://{seaweedfs.get_config()['endpoint']}",
            access_key=seaweedfs.access_key,
            secret_key=seaweedfs.secret_key,
        )


def _run(client: TestClient, run: QueryRun) -> None:
    """Execute one queued run in-process.

    Parameters
    ----------
    client
        The test client, for its engine.
    run
        The SDK's handle.
    """
    with Session(client.app.state.engine) as session:
        run_one(session, uuid.UUID(run.id), GrantAuthorization(session))


def _events(ids: list[int], day: int) -> pa.Table:
    """Build events, one per id, all at midnight on 2026-09-`day`.

    Parameters
    ----------
    ids
        The ids.
    day
        The day of September 2026.

    Returns
    -------
    pa.Table
        The rows.
    """
    ts = dt.datetime(2026, 9, day)
    return pa.table(
        {
            "id": pa.array(ids, pa.int64()),
            "ts": pa.array([ts] * len(ids), pa.timestamp("us")),
        }
    )


def test_writes_views_lineage_and_maintenance_through_the_sdk(stack: _Stack) -> None:
    """Full refresh, incremental run, view query, Arrow replace, lineage and maintenance on S3."""
    with (
        TestClient(create_app(stack.settings)) as client,
        lakana.Client(url="http://testserver", token=_TOKEN, http_client=client) as sdk,
    ):
        storage = {"scheme": "s3", "root": "s3://lakana/sdk", "options": stack.options()}
        sdk.environments.create("sdk", storage=storage)
        sdk.tables.create("sdk", "bronze.events", schema=_events([], 1).schema)
        sdk.tables.append("sdk", "bronze.events", _events([1, 2], 1))

        created = sdk.queries.submit(
            "SELECT id, ts FROM bronze.events", "sdk", into="gold.events", mode="create"
        )
        _run(client, created)
        assert created.status().rows_written == 2

        daily = sdk.queries.submit(
            "SELECT count(*) AS n FROM bronze.events", "sdk", into="gold.daily", mode="create"
        )
        _run(client, daily)
        refresh = sdk.queries.submit(
            "SELECT count(*) AS n FROM bronze.events", "sdk", into="gold.daily", mode="replace"
        )
        _run(client, refresh)
        assert refresh.status().status == "SUCCEEDED"

        sdk.tables.append("sdk", "bronze.events", _events([3], 2))
        incremental = sdk.queries.submit(
            "SELECT id, ts FROM bronze.events",
            "sdk",
            into="gold.events",
            mode="append",
            incremental_key="ts",
        )
        _run(client, incremental)
        status = incremental.status()
        assert (status.rows_written, status.watermark) == (1, "timestamp:2026-09-01T00:00:00")

        view = sdk.queries.submit(
            "SELECT id FROM gold.events WHERE id > 1", "sdk", into="gold.v", mode="view"
        )
        _run(client, view)
        assert sdk.tables.get("sdk", "gold.v").kind == "VIEW"
        read = sdk.queries.submit("SELECT id FROM gold.v ORDER BY id", "sdk")
        _run(client, read)
        assert read.result().column("id").to_pylist() == [2, 3]

        sdk.tables.create("sdk", "gold.snapshot", schema=pa.schema([pa.field("n", pa.int64())]))
        replaced = sdk.tables.replace(
            "sdk", "gold.snapshot", pa.table({"n": [7]}), sources=["gold.daily"]
        )
        assert replaced.rows == 1

        lineage = sdk.tables.lineage("sdk", "gold.snapshot", depth=3)
        assert {node.name for node in lineage.nodes} == {
            "gold.snapshot",
            "gold.daily",
            "bronze.events",
        }
        assert lineage.hidden_nodes == 0
        assert not lineage.truncated
        # gold.daily was written twice; Postgres must dedupe that to one edge per pair.
        daily_id = next(n.id for n in lineage.nodes if n.name == "gold.daily")
        assert len([e for e in lineage.edges if e.target_id == daily_id]) == 1

        maintenance = sdk.tables.maintain(
            "sdk", "gold.daily", keep_days=0, keep_last=1, older_than_hours=1
        )
        _run(client, maintenance)
        report = (maintenance.status().maintenance or {})["report"]
        assert len(report["snapshots_expired"]) == 1
        assert report["files_deleted"] == 0


def test_orphans_are_deleted_from_s3(stack: _Stack) -> None:
    """Orphan listing and deletion work against an object store (modification times, sizes)."""
    with (
        TestClient(create_app(stack.settings)) as client,
        Session(client.app.state.engine) as session,
    ):
        principal = ensure_bootstrap_principal(session, "m5a-orphans")
        catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
        catalog.create_environment(
            principal,
            name="orphans",
            storage=StorageConfig(scheme="s3", root="s3://lakana/orphans", options=stack.options()),
        )
        ref = TableRef.from_dotted("orphans", "gold.t")
        for mode in (WriteMode.CREATE, WriteMode.REPLACE):
            data = pa.table({"id": pa.array([1], pa.int64())})
            catalog.write_batches(
                principal, ref, mode=mode, schema=data.schema, batches=data.to_batches()
            )
        location = catalog.load_table(principal, ref).metadata.location
        resolved = StorageResolver(session).for_environment("orphans")
        stray = (
            location.removeprefix(resolved.config.root.rstrip("/") + "/") + "/data/stray.parquet"
        )
        resolved.storage.write(stray, b"stray")

        report = catalog.maintain(
            principal,
            ref,
            keep_days=0,
            keep_last=1,
            older_than_hours=1,
            now=dt.datetime.now(dt.UTC) + dt.timedelta(hours=2),
        )

        assert len(report.snapshots_expired) == 1
        assert report.files_deleted >= 2
        assert not resolved.storage.exists(stray)
        # Final review M3: what the current snapshot references must survive.
        metadata = catalog.load_table(principal, ref).metadata
        snapshot = metadata.current_snapshot()
        assert snapshot is not None
        manifests = snapshot.manifests(resolved.file_io)
        data_files = [
            entry.data_file.file_path
            for manifest in manifests
            for entry in manifest.fetch_manifest_entry(resolved.file_io, discard_deleted=True)
        ]
        live = [snapshot.manifest_list, *(m.manifest_path for m in manifests), *data_files]
        assert len(data_files) == 1
        root = resolved.config.root.rstrip("/") + "/"
        missing = [path for path in live if not resolved.storage.exists(path.removeprefix(root))]
        assert missing == []


def _invoke(state: CliState, *args: str, input: str | None = None) -> Result:
    """Run one CLI command in-process and require it to succeed.

    Parameters
    ----------
    state
        The CLI state.
    *args
        The arguments.
    input
        Text fed to stdin.

    Returns
    -------
    typer.testing.Result
        The successful result.
    """
    env = {name: None for name in ("LAKANA_OUTPUT", "LAKANA_PROFILE", "LAKANA_TOKEN", "LAKANA_URL")}
    env.update({"COLUMNS": "200", "FORCE_COLOR": None, "PY_COLORS": None, "NO_COLOR": "1"})
    result = CliRunner().invoke(build_app(), list(args), obj=state, input=input, env=env)
    assert result.exit_code == 0, result.stderr or result.stdout
    return result


def test_writes_lineage_and_maintenance_through_the_cli(stack: _Stack, tmp_path: Path) -> None:
    """`sql --into`, `table lineage` and `table maintain` against the running server."""
    with TestClient(create_app(stack.settings)) as client:
        engine = client.app.state.engine
        stop = threading.Event()
        errors: list[BaseException] = []

        def loop() -> None:
            """Claim and run queued runs until stopped, recording anything that escapes."""
            try:
                while not stop.is_set():
                    with Session(engine) as session:
                        claimed = poll_once(session, GrantAuthorization(session))
                    if not claimed:
                        stop.wait(0.05)
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=loop, daemon=True)
        worker.start()
        try:
            state = make_cli_state(client, tmp_path, token=_TOKEN)
            _invoke(
                state,
                "login",
                "--url",
                "http://testserver",
                "--token-stdin",
                "--env",
                "cli",
                input=_TOKEN + "\n",
            )
            options = [
                arg
                for key, value in stack.options().items()
                for arg in ("--option", f"{key}={value}")
            ]
            _invoke(state, "env", "create", "cli", "--storage-root", "s3://lakana/cli", *options)
            _invoke(state, "table", "create", "bronze.sales", "--column", "id:long")
            csv = tmp_path / "sales.csv"
            csv.write_text("id\n1\n2\n3\n")
            _invoke(state, "table", "append", "bronze.sales", str(csv))

            written = _invoke(
                state,
                "sql",
                "SELECT id FROM bronze.sales",
                "--into",
                "gold.ids",
                "--mode",
                "create",
                "--timeout",
                "60",
            )
            assert "Wrote 3 rows to cli/gold.ids" in written.stdout
            _invoke(
                state,
                "sql",
                "SELECT id FROM bronze.sales WHERE id > 1",
                "--into",
                "gold.ids",
                "--mode",
                "replace",
                "--timeout",
                "60",
            )

            lineage = json.loads(
                _invoke(state, "--output", "json", "table", "lineage", "gold.ids").stdout
            )
            assert {node["name"] for node in lineage["nodes"]} == {"gold.ids", "bronze.sales"}
            tree = _invoke(state, "table", "lineage", "gold.ids").stdout
            assert tree.splitlines()[:2] == ["gold.ids", "└── bronze.sales"]

            maintained = _invoke(
                state,
                "table",
                "maintain",
                "gold.ids",
                "--keep-days",
                "0",
                "--keep-last",
                "1",
                "--timeout",
                "60",
            )
            assert "Maintained cli/gold.ids: expired 1 snapshots" in maintained.stdout
        finally:
            stop.set()
            worker.join(timeout=10)
            assert not worker.is_alive()
            if errors:
                raise errors[0]
