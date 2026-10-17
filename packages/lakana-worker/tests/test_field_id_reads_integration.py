"""A schema change followed by a policy-bound read, on Postgres and SeaweedFS."""

import io

import pyarrow as pa
import pyarrow.ipc
import pytest
from pyiceberg.schema import Schema
from pyiceberg.table.update import AddSchemaUpdate, SetCurrentSchemaUpdate
from pyiceberg.types import LongType, NestedField, StringType
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
from lakana_core.identity.principals import PrincipalService
from lakana_core.models import Base, PrincipalType, QueryRunStatus
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_core.policy.row_filters import RowFilterService
from lakana_core.storage.resolver import StorageResolver
from lakana_worker.pipeline import run_one

pytestmark = pytest.mark.integration


def _customers(
    ids: list[int], regions: list[str], names: list[str], tier: list[str] | None
) -> pa.Table:
    """Build rows of `gold.customers`, with or without the `tier` column.

    Parameters
    ----------
    ids
        The ids.
    regions
        The regions.
    names
        The names, under `label` before the change and `name` after it.
    tier
        The tiers, or `None` for rows written before the column existed.

    Returns
    -------
    pa.Table
        The rows.
    """
    columns = {
        "id": pa.array(ids, pa.int64()),
        "region": regions,
        ("label" if tier is None else "name"): names,
    }
    if tier is not None:
        columns["tier"] = tier
    return pa.table(columns)


def test_a_rename_and_an_added_column_then_a_filtered_and_masked_read() -> None:
    """Old and new files through presigned URLs, a filter, a mask and real grants."""
    with (
        PostgresContainer("postgres:16-alpine", driver="pg8000") as pg,
        SeaweedFsContainer() as seaweedfs,
    ):
        engine = create_engine(DatabaseConfig(url=pg.get_connection_url()))
        Base.metadata.create_all(engine)
        seaweedfs.get_client().make_bucket("lakana")
        endpoint = f"http://{seaweedfs.get_config()['endpoint']}"
        with Session(engine) as session:
            admin = ensure_bootstrap_principal(session, "admin")
            ensure_bootstrap_admin_grant(session, admin, AllowAllAuthorization())
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
            ref = TableRef(
                environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="customers"
            )
            created = catalog.create_table(
                admin,
                ref,
                schema=Schema(
                    NestedField(1, "id", LongType(), required=False),
                    NestedField(2, "region", StringType(), required=False),
                    NestedField(3, "label", StringType(), required=False),
                ),
            )
            catalog.append(admin, ref, _customers([1, 2], ["EU", "US"], ["Alice", "Bob"], None))
            changed = Schema(
                NestedField(1, "id", LongType(), required=False),
                NestedField(2, "region", StringType(), required=False),
                NestedField(3, "name", StringType(), required=False),
                NestedField(4, "tier", StringType(), required=False),
                schema_id=created.metadata.current_schema_id + 1,
            )
            catalog.commit_table(
                admin,
                ref,
                requirements=[],
                updates=[AddSchemaUpdate(schema_=changed), SetCurrentSchemaUpdate(schema_id=-1)],
            )
            catalog.append(admin, ref, _customers([3], ["EU"], ["Carol"], ["gold"]))

            alice = PrincipalService(session, AllowAllAuthorization()).create_principal(
                admin, name="alice", type=PrincipalType.SERVICE_ACCOUNT
            )
            alice_ref = PrincipalRef(id=alice.id, name=alice.name, type=alice.type)
            GrantService(session, AllowAllAuthorization()).grant(
                admin,
                principal="alice",
                role=Role.ANALYST,
                kind=SecurableKind.ENVIRONMENT,
                environment="dev",
            )
            RowFilterService(session, AllowAllAuthorization(), catalog).create(
                admin,
                principal="alice",
                environment="dev",
                namespace="gold",
                table="customers",
                expression="region = 'EU'",
            )
            ColumnMaskService(session, AllowAllAuthorization(), catalog).create(
                admin,
                principal="alice",
                environment="dev",
                namespace="gold",
                table="customers",
                column="name",
                expression="NULL",
            )

            results = {}
            for reader in (admin, alice_ref):
                run = QueryService(session, GrantAuthorization(session)).submit_query(
                    reader, "dev", "SELECT * FROM gold.customers"
                )
                run_one(session, run.id, GrantAuthorization(session))
                session.refresh(run)
                assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
                assert run.result_location is not None
                raw = (
                    StorageResolver(session)
                    .for_environment("dev")
                    .storage.read(run.result_location)
                )
                with pyarrow.ipc.open_file(io.BytesIO(raw)) as stream:
                    results[reader.name] = sorted(
                        stream.read_all().to_pylist(), key=lambda r: r["id"]
                    )

        assert results["admin"] == [
            {"id": 1, "region": "EU", "name": "Alice", "tier": None},
            {"id": 2, "region": "US", "name": "Bob", "tier": None},
            {"id": 3, "region": "EU", "name": "Carol", "tier": "gold"},
        ]
        assert results["alice"] == [
            {"id": 1, "region": "EU", "name": None, "tier": None},
            {"id": 3, "region": "EU", "name": None, "tier": "gold"},
        ]
        engine.dispose()
