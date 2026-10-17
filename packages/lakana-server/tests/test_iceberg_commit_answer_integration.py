"""A real pyiceberg client commits through the Iceberg REST catalog without READ.

Postgres and SeaweedFS from testcontainers and a real HTTP server: the answer to a commit
carries the table's history only for a caller who may read the table, and an unmodified
pyiceberg client still parses the answer it gets. A Python task's own commit, which may write
its table and not read it, gets that answer too and moves the task's own tip.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pyarrow as pa
import pytest
import requests
from pyiceberg.catalog.rest import RestCatalog
from pyiceberg.manifest import (
    DataFile,
    DataFileContent,
    FileFormat,
    ManifestEntry,
    ManifestEntryStatus,
    write_manifest,
    write_manifest_list,
)
from pyiceberg.table import Table as IcebergTable
from pyiceberg.table.metadata import TableMetadataUtil
from pyiceberg.table.refs import SnapshotRefType
from pyiceberg.table.snapshots import Operation, Snapshot, Summary
from pyiceberg.table.update import AddSnapshotUpdate, SetSnapshotRefUpdate
from pyiceberg.typedef import Record
from pyiceberg.types import LongType
from sqlalchemy import Engine, update
from sqlalchemy.orm import Session

import lakana
from lakana_core.db import create_engine
from lakana_core.models import TaskRun
from lakana_core.runs.tasks import OwnTip, read_own_tip
from lakana_server.app import create_app
from tests.integration_helpers import unique_name
from tests.python_asset_helpers import TOKEN, Services, bound_attempt, running_services, serving

pytestmark = pytest.mark.integration

_IDS = pa.schema([pa.field("id", pa.int64())])


@dataclass(frozen=True)
class _Stack:
    """The services, a running server and an admin client.

    Parameters
    ----------
    services
        Postgres and SeaweedFS.
    url
        The server's base URL.
    admin
        An SDK client with the bootstrap token.
    db
        An engine on the metadata database.
    """

    services: Services
    url: str
    admin: lakana.Client
    db: Engine

    def catalog(self, environment: str, token: str) -> RestCatalog:
        """Open an unmodified pyiceberg REST catalog on an environment.

        The server hands out no storage credentials, so the client gets the store's own.

        Parameters
        ----------
        environment
            The environment.
        token
            The bearer token.

        Returns
        -------
        RestCatalog
            The catalog.
        """
        options: dict[str, Any] = self.services.storage["options"]  # ty: ignore[invalid-assignment]
        return RestCatalog(name="lakana", uri=self.url, prefix=environment, token=token, **options)


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Stack]:
    """Start Postgres, SeaweedFS and a real HTTP server.

    Parameters
    ----------
    tmp_path_factory
        For the repo root.

    Yields
    ------
    _Stack
        The stack.
    """
    with (
        running_services(tmp_path_factory) as services,
        serving(create_app(services.settings)) as url,
        lakana.Client(url, TOKEN, timeout=60.0) as admin,
    ):
        db = create_engine(services.settings.database)
        try:
            yield _Stack(services=services, url=url, admin=admin, db=db)
        finally:
            db.dispose()


def _writer(admin: lakana.Client, environment: str) -> str:
    """Create a user who may write the environment's tables and not read them.

    Parameters
    ----------
    admin
        An SDK client with the bootstrap token.
    environment
        The environment.

    Returns
    -------
    str
        The user's token.
    """
    name = f"writer-{uuid.uuid4().hex[:8]}"
    admin.principals.create(name, "USER")
    for action in ("READ_ENVIRONMENT", "WRITE_TABLE"):
        admin.grants.grant(
            principal=name, action=action, kind="ENVIRONMENT", environment=environment
        )
    return admin.tokens.issue(name, f"{name}-pat").token


def test_a_writer_without_read_commits_with_pyiceberg_and_parses_the_answer(
    stack: _Stack,
) -> None:
    """The append lands; the answer has no history and parses, with delete-after-commit on."""
    environment = unique_name()
    stack.services.environment(stack.admin, environment)
    stack.admin.tables.create(environment, "gold.sales", schema=_IDS)
    stack.admin.tables.append(environment, "gold.sales", pa.table({"id": [1]}, schema=_IDS))
    admin_catalog = stack.catalog(environment, TOKEN)
    with admin_catalog.load_table(("gold", "sales")).transaction() as txn:
        txn.set_properties({"write.metadata.delete-after-commit.enabled": "true"})
    loaded = admin_catalog.load_table(("gold", "sales"))
    token = _writer(stack.admin, environment)
    writer_catalog = stack.catalog(environment, token)
    table = IcebergTable(
        identifier=("gold", "sales"),
        metadata=loaded.metadata,
        metadata_location=loaded.metadata_location,
        io=loaded.io,
        catalog=writer_catalog,
    )

    table.append(pa.table({"id": [2]}, schema=_IDS))

    assert table.metadata.current_snapshot_id is None
    assert table.metadata.snapshots == []
    assert table.metadata.properties == {}
    assert table.metadata.table_uuid == loaded.metadata.table_uuid
    after = admin_catalog.load_table(("gold", "sales"))
    assert after.metadata_location == table.metadata_location
    assert sorted(after.scan().to_arrow().column("id").to_pylist()) == [1, 2]
    assert loaded.io.new_input(loaded.metadata_location).exists()
    # The answer carries no time of the table's last change, and parses without it.
    probe = requests.post(
        f"{stack.url}/v1/{environment}/namespaces/gold/tables/sales",
        headers={"Authorization": f"Bearer {token}"},
        json={},
        timeout=60,
    )
    assert probe.status_code == 200, probe.text
    assert "last-updated-ms" not in probe.json()["metadata"]
    reparsed = TableMetadataUtil.parse_obj(probe.json()["metadata"])
    assert reparsed.table_uuid == table.metadata.table_uuid


def test_a_python_tasks_own_pyiceberg_append_parses_its_answer_and_moves_its_tip(
    stack: _Stack,
) -> None:
    """The task writes its new table, then appends to it through pyiceberg.

    A task may not create a table through the Iceberg REST catalog (the write guard allows a
    task to write, append and commit, not to create), and may not load its own table, so the
    client is handed the table an administrator loaded, as in the test above. The task's
    answer is the one without history, and the client parses it.
    """
    environment = unique_name()
    stack.services.environment(stack.admin, environment)
    stack.admin.tables.create(environment, "bronze.events", schema=_IDS)
    attempt = bound_attempt(
        stack.db, stack.admin, environment, asset="gold.scores", inputs={"obs": "bronze.events"}
    )
    with Session(stack.db) as session:
        session.execute(
            update(TaskRun)
            .where(TaskRun.id == attempt.task_run_id)
            .values(has_checks=True, baseline_snapshot_id=None, baseline_publishes=True)
        )
        session.commit()
    with lakana.Client(stack.url, attempt.token, timeout=60.0) as task:
        task.tables.replace(
            environment,
            "gold.scores",
            pa.table({"id": [1]}, schema=_IDS),
            expected_snapshot_id="none",
        )
    admin_catalog = stack.catalog(environment, TOKEN)
    loaded = admin_catalog.load_table(("gold", "scores"))
    table = IcebergTable(
        identifier=("gold", "scores"),
        metadata=loaded.metadata,
        metadata_location=loaded.metadata_location,
        io=loaded.io,
        catalog=stack.catalog(environment, attempt.token),
    )

    table.append(pa.table({"id": [2]}, schema=_IDS))

    assert table.metadata.current_snapshot_id is None
    assert table.metadata.snapshots == []
    after = admin_catalog.load_table(("gold", "scores"))
    appended = after.metadata.current_snapshot_id
    assert appended not in (None, loaded.metadata.current_snapshot_id)
    with Session(stack.db) as session:
        assert read_own_tip(session, attempt.task_run_id) == OwnTip(appended)
    assert sorted(after.scan().to_arrow().column("id").to_pylist()) == [1, 2]


def test_a_writers_pyiceberg_schema_change_on_a_reused_schema_id_is_refused(
    stack: _Stack,
) -> None:
    """A dropped column does not come back through the id pyiceberg picks.

    The table evolved back to its first schema, so its current schema is not its highest. The
    writer sees only the current schema in its answer, so pyiceberg picks the current id plus
    one, which is the id of the schema that still has `salary`.
    """
    environment = unique_name()
    stack.services.environment(stack.admin, environment)
    stack.admin.tables.create(environment, "gold.sales", schema=_IDS)
    admin_catalog = stack.catalog(environment, TOKEN)
    with admin_catalog.load_table(("gold", "sales")).update_schema() as change:
        change.add_column("salary", LongType())
    with admin_catalog.load_table(("gold", "sales")).update_schema() as change:
        change.delete_column("salary")
    assert len(admin_catalog.load_table(("gold", "sales")).metadata.schemas) == 2
    token = _writer(stack.admin, environment)
    probe = requests.post(
        f"{stack.url}/v1/{environment}/namespaces/gold/tables/sales",
        headers={"Authorization": f"Bearer {token}"},
        json={},
        timeout=60,
    )
    assert probe.status_code == 200, probe.text
    loaded = admin_catalog.load_table(("gold", "sales"))
    table = IcebergTable(
        identifier=("gold", "sales"),
        metadata=TableMetadataUtil.parse_obj(probe.json()["metadata"]),
        metadata_location=probe.json()["metadata-location"],
        io=loaded.io,
        catalog=stack.catalog(environment, token),
    )

    with pytest.raises(Exception, match="already uses"), table.update_schema() as change:
        change.add_column("region", LongType())

    current = admin_catalog.load_table(("gold", "sales")).schema()
    assert [field.name for field in current.fields] == ["id"]


def test_a_commit_that_names_a_data_file_missing_from_s3_is_refused_with_422(
    stack: _Stack,
) -> None:
    """A missing object is the client's fault: 422 naming the file, not a retryable 503."""
    environment = unique_name()
    stack.services.environment(stack.admin, environment)
    stack.admin.tables.create(environment, "gold.sales", schema=_IDS)
    catalog = stack.catalog(environment, TOKEN)
    table = catalog.load_table(("gold", "sales"))
    metadata = table.metadata
    missing = f"{metadata.location}/data/never-written.parquet"
    snapshot_id = 900_000_201
    with write_manifest(
        format_version=metadata.format_version,
        spec=metadata.spec(),
        schema=metadata.schema(),
        output_file=table.io.new_output(f"{metadata.location}/metadata/{snapshot_id}-m0.avro"),
        snapshot_id=snapshot_id,
        avro_compression="null",
    ) as writer:
        writer.add(
            ManifestEntry.from_args(
                status=ManifestEntryStatus.ADDED,
                snapshot_id=snapshot_id,
                sequence_number=1,
                file_sequence_number=1,
                data_file=DataFile.from_args(
                    content=DataFileContent.DATA,
                    file_path=missing,
                    file_format=FileFormat.PARQUET,
                    partition=Record(),
                    record_count=1,
                    file_size_in_bytes=1,
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
                ),
            )
        )
    list_path = f"{metadata.location}/metadata/snap-{snapshot_id}.avro"
    with write_manifest_list(
        format_version=metadata.format_version,
        output_file=table.io.new_output(list_path),
        snapshot_id=snapshot_id,
        parent_snapshot_id=None,
        sequence_number=1,
        avro_compression="null",
    ) as list_writer:
        list_writer.add_manifests([writer.to_manifest_file()])
    snapshot = Snapshot(
        snapshot_id=snapshot_id,
        parent_snapshot_id=None,
        sequence_number=1,
        timestamp_ms=int(time.time() * 1000),
        manifest_list=list_path,
        summary=Summary(operation=Operation.APPEND),
        schema_id=metadata.current_schema_id,
    )
    transaction = table.transaction()
    transaction._apply(
        (
            AddSnapshotUpdate(snapshot=snapshot),
            SetSnapshotRefUpdate(
                ref_name="main", type=SnapshotRefType.BRANCH, snapshot_id=snapshot_id
            ),
        )
    )

    with pytest.raises(Exception, match="could not be read as a Parquet file") as caught:
        transaction.commit_transaction()

    assert missing in str(caught.value)
    assert "right now" not in str(caught.value)
