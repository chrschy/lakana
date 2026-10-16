"""The M1 Phase B exit criterion: a full table lifecycle on real infrastructure.

Postgres and SeaweedFS via testcontainers. If this passes, `lakana-core`'s
catalog is complete: a table can be created, loaded, committed to, renamed
and dropped through `CatalogService`, with Iceberg metadata in object
storage and the pointer in Postgres.
"""

import uuid

import pytest
from core_test_helpers import SeaweedFsContainer
from minio import Minio
from minio.error import S3Error
from pyiceberg.schema import Schema
from pyiceberg.table.update import AssertTableUUID, SetPropertiesUpdate
from pyiceberg.types import LongType, NestedField, StringType
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from lakana_core.auth import AllowAllAuthorization
from lakana_core.bootstrap import ensure_bootstrap_principal
from lakana_core.catalog.service import CatalogService
from lakana_core.config import DatabaseConfig, StorageConfig
from lakana_core.db import create_engine
from lakana_core.errors import ConflictError, NotFoundError
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import Base
from lakana_core.storage.resolver import StorageResolver

pytestmark = pytest.mark.integration


def _object_size(client: Minio, location: str) -> int:
    """Return the size of an object, addressed by its ``s3://`` URI.

    Reads through the object store's own client rather than pyiceberg's
    `FileIO`, so that what is asserted is the state of the bucket and not
    the state `FileIO` reports.

    Parameters
    ----------
    client
        An S3 client for the running container.
    location
        An ``s3://lakana/...`` URI naming the object.

    Returns
    -------
    int
        The object's size in bytes.

    Raises
    ------
    AssertionError
        If the store reports no size for the object.
    """
    size = client.stat_object("lakana", location.removeprefix("s3://lakana/")).size
    assert size is not None
    return size


def test_the_full_table_lifecycle_on_postgres_and_seaweedfs() -> None:
    """Create, commit, rename and drop a table end to end."""
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
            ns = NamespacePath.from_parts(["gold", "sales"])
            catalog.create_namespace(principal, "dev", ns)
            ref = TableRef(environment="dev", namespace=ns, name="monthly")

            created = catalog.create_table(
                principal,
                ref,
                schema=Schema(
                    NestedField(1, "id", LongType(), required=True),
                    NestedField(2, "region", StringType(), required=False),
                ),
            )
            assert created.metadata_location.startswith("s3://lakana/dev/")
            assert created.metadata_location.split("/")[-1].startswith("00000-")
            # Read back through the object store's own client, not `file_io`:
            # this is what proves the object really landed in the bucket
            # rather than the FileIO layer merely reporting success.
            assert _object_size(client, created.metadata_location) > 0

            committed = catalog.commit_table(
                principal,
                ref,
                requirements=[AssertTableUUID(uuid=created.uuid)],
                updates=[SetPropertiesUpdate(updates={"owner": "analytics"})],
            )
            assert committed.metadata_location.split("/")[-1].startswith("00001-")
            assert catalog.load_table(principal, ref).metadata.properties["owner"] == ("analytics")

            with pytest.raises(ConflictError):
                catalog.commit_table(
                    principal,
                    ref,
                    requirements=[AssertTableUUID(uuid=uuid.uuid4())],
                    updates=[SetPropertiesUpdate(updates={"owner": "nobody"})],
                )

            with pytest.raises(ConflictError):
                catalog.drop_namespace(principal, "dev", ns)

            destination = TableRef(environment="dev", namespace=ns, name="monthly_v2")
            moved = catalog.rename_table(principal, ref, destination)
            assert moved.uuid == created.uuid
            assert moved.metadata_location == committed.metadata_location

            assert _object_size(client, committed.metadata_location) > 0

            catalog.drop_table(principal, destination, purge=True)
            assert catalog.table_exists(principal, destination) is False
            # `purge=True` must actually remove the object. Asserted through
            # the object store's own client rather than `file_io`, so a
            # `file_io.delete` that silently no-ops cannot satisfy the exit
            # criterion. The earlier metadata file is deliberately still
            # there: `purge` deletes the current pointer's target, not the
            # whole history.
            with pytest.raises(S3Error):
                _object_size(client, committed.metadata_location)
            assert _object_size(client, created.metadata_location) > 0

            catalog.drop_namespace(principal, "dev", ns)
            with pytest.raises(NotFoundError):
                catalog.load_namespace(principal, "dev", ns)

        engine.dispose()
