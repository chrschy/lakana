# packages/lakana-server/tests/api/platform/test_tables_lineage.py
"""The lineage route."""

from __future__ import annotations

import io

import pyarrow as pa
import pyarrow.ipc
import pytest
from fastapi.testclient import TestClient

from lakana_core.catalog import lineage_graph
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment

_TABLES = "/api/v1/environments/dev/namespaces/gold/tables"


def _setup(client: TestClient) -> None:
    """Create `gold.src` and `gold.t`, and append to `gold.t` declaring `gold.src`.

    Parameters
    ----------
    client
        The test client.
    """
    create_environment(client)
    for name in ("src", "t"):
        response = client.post(
            _TABLES,
            headers=AUTH_HEADER,
            json={"name": name, "schema": [{"name": "id", "type": "long", "nullable": True}]},
        )
        assert response.status_code == 201, response.text
    sink = io.BytesIO()
    data = pa.table({"id": pa.array([1], pa.int64())})
    with pyarrow.ipc.new_stream(sink, data.schema) as writer:
        writer.write_table(data)
    appended = client.post(
        f"{_TABLES}/t/data",
        headers={**AUTH_HEADER, "Content-Type": "application/vnd.apache.arrow.stream"},
        params={"source": ["gold.src"]},
        content=sink.getvalue(),
    )
    assert appended.status_code == 200, appended.text


def test_upstream_lineage_lists_nodes_and_edges(client: TestClient) -> None:
    """The default is upstream, depth 3."""
    _setup(client)

    response = client.get(f"{_TABLES}/t/lineage", headers=AUTH_HEADER)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["direction"], body["depth"], body["hidden_nodes"]) == ("upstream", 3, 0)
    assert body["truncated"] is False
    names = {node["id"]: node["name"] for node in body["nodes"]}
    assert set(names.values()) == {"gold.t", "gold.src"}
    assert all(node["kind"] == "TABLE" and node["dropped"] is False for node in body["nodes"])
    [edge] = body["edges"]
    assert (names[edge["source_id"]], names[edge["target_id"]]) == ("gold.src", "gold.t")
    assert edge["target_snapshot_id"] is not None


def test_lineage_past_the_node_cap_reports_truncated(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`truncated` reaches the REST response when the walk stops at the cap."""
    _setup(client)
    monkeypatch.setattr(lineage_graph, "MAX_LINEAGE_NODES", 1)

    response = client.get(f"{_TABLES}/t/lineage", headers=AUTH_HEADER)

    assert response.status_code == 200, response.text
    assert response.json()["truncated"] is True
    assert [node["name"] for node in response.json()["nodes"]] == ["gold.t"]


def test_downstream_lineage_walks_the_other_way(client: TestClient) -> None:
    """`direction=downstream` from the source reaches the target."""
    _setup(client)

    response = client.get(
        f"{_TABLES}/src/lineage", headers=AUTH_HEADER, params={"direction": "downstream"}
    )

    assert {node["name"] for node in response.json()["nodes"]} == {"gold.src", "gold.t"}


def test_out_of_range_depth_and_unknown_direction_are_422(client: TestClient) -> None:
    """Depth is 1 to 10; direction is upstream or downstream."""
    _setup(client)

    too_deep = client.get(f"{_TABLES}/t/lineage", headers=AUTH_HEADER, params={"depth": 11})
    sideways = client.get(f"{_TABLES}/t/lineage", headers=AUTH_HEADER, params={"direction": "up"})

    assert too_deep.status_code == 422
    assert sideways.status_code == 422
