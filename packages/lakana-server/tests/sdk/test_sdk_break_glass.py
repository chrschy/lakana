"""Break-glass through the SDK."""

from __future__ import annotations

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

import lakana
from lakana.errors import PermissionDenied
from tests.api.platform.test_lockdown_api import _REASON, _protected


def test_writes_need_break_glass_in_prod(lakana_client: lakana.Client, client: TestClient) -> None:
    """Without a reason every SDK write is refused; with one it goes through."""
    _protected(client)
    rows = pa.table(
        {"id": pa.array([1], pa.int64())},
        schema=pa.schema([pa.field("id", pa.int64(), nullable=False)]),
    )
    with pytest.raises(PermissionDenied, match="is protected"):
        lakana_client.tables.append("prod", "bronze.events", rows)
    assert (
        lakana_client.tables.append("prod", "bronze.events", rows, break_glass_reason=_REASON).rows
        == 1
    )
    lakana_client.tables.replace("prod", "bronze.events", rows, break_glass_reason=_REASON)
    lakana_client.namespaces.create("prod", "bronze.eu", break_glass_reason=_REASON)
    lakana_client.namespaces.delete("prod", "bronze.eu", break_glass_reason=_REASON)
    lakana_client.tables.create(
        "prod",
        "bronze.t2",
        [{"name": "id", "type": "long", "nullable": False}],
        break_glass_reason=_REASON,
    )
    lakana_client.tables.delete("prod", "bronze.t2", break_glass_reason=_REASON)
    run = lakana_client.queries.submit(
        "SELECT 2::BIGINT AS id",
        "prod",
        into="bronze.events",
        mode="append",
        break_glass_reason=_REASON,
    )
    assert run.id
    with pytest.raises(PermissionDenied):
        lakana_client.tables.create_external("prod", "bronze.ext", "raw/x/*.parquet", "parquet")
    with pytest.raises(lakana.ValidationError):
        lakana_client.tables.create_external(
            "prod", "bronze.ext", "raw/x/*.parquet", "parquet", break_glass_reason="short"
        )
