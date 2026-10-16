"""On real Postgres: a dropped table's lineage is not visible through a new table's name.

The scenario from the issue: a table is dropped, someone recreates the name, and a principal
holding a table-level READ grant on the new table asks for lineage. The old node, its edges,
its snapshot ids and its run ids must be absent from every response that principal can obtain,
while a namespace reader (the original owner's access) and an administrator still see the
history. Variants: a second drop and recreate, a view created under the old name, a task token,
and a namespace dropped and recreated.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path

import pyarrow as pa
import pyarrow.ipc
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from lakana_core.auth import AllowAllAuthorization
from lakana_core.bootstrap import ensure_bootstrap_principal
from lakana_core.catalog.json_schema import ColumnSpec
from lakana_core.catalog.service import CatalogService
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.identifiers import TableRef
from lakana_core.models import Base
from lakana_core.storage.resolver import StorageResolver
from lakana_server.app import create_app
from lakana_server.settings import Settings

pytestmark = pytest.mark.integration

_ADMIN = {"Authorization": "Bearer lakana_pat_lineagevis_lineagevissecret"}
_TABLES = "/api/v1/environments/dev/namespaces/gold/tables"
_SCHEMA = [{"name": "id", "type": "long", "nullable": True}]
_TABLE_GRANT = {"action": "READ_TABLE", "kind": "TABLE", "environment": "dev"}


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Start Postgres and an app over it.

    Yields
    ------
    TestClient
        A client whose bootstrap administrator uses `_ADMIN`.
    """
    with PostgresContainer("postgres:16-alpine", driver="pg8000") as pg:
        settings = Settings(
            database=DatabaseConfig(url=pg.get_connection_url()),
            bootstrap_pat_key_id="lineagevis",
            bootstrap_pat_secret="lineagevissecret",
        )
        engine = create_engine(settings.database)
        Base.metadata.create_all(engine)
        engine.dispose()
        with TestClient(create_app(settings)) as c:
            yield c


@pytest.fixture
def env(client: TestClient, tmp_path: Path) -> TestClient:
    """Create the `dev` environment as the administrator.

    Parameters
    ----------
    client
        The test client.
    tmp_path
        Backs the environment's storage.

    Returns
    -------
    TestClient
        The same client, with `dev` existing.
    """
    created = client.post(
        "/api/v1/environments",
        headers=_ADMIN,
        json={
            "name": "dev",
            "storage": {"scheme": "file", "root": f"file://{tmp_path}/lake", "options": {}},
        },
    )
    assert created.status_code == 201, created.text
    return client


def _principal(client: TestClient, name: str) -> dict[str, str]:
    """Create a service account and return its auth header.

    Parameters
    ----------
    client
        The test client.
    name
        The principal's name.

    Returns
    -------
    dict
        The `Authorization` header.
    """
    created = client.post(
        "/api/v1/principals", headers=_ADMIN, json={"name": name, "type": "SERVICE_ACCOUNT"}
    )
    assert created.status_code == 201, created.text
    issued = client.post(
        f"/api/v1/principals/{name}/tokens", headers=_ADMIN, json={"name": f"{name}-pat"}
    )
    assert issued.status_code == 201, issued.text
    return {"Authorization": f"Bearer {issued.json()['token']}"}


def _grant(client: TestClient, principal: str, **fields: str) -> None:
    """Grant `principal` something as the administrator.

    Parameters
    ----------
    client
        The test client.
    principal
        The grantee.
    **fields
        The grant's other fields.
    """
    granted = client.post("/api/v1/grants", headers=_ADMIN, json={"principal": principal, **fields})
    assert granted.status_code == 201, granted.text


def _create(client: TestClient, name: str, base: str = _TABLES) -> None:
    """Create a table as the administrator.

    Parameters
    ----------
    client
        The test client.
    name
        The table name.
    base
        The tables URL of its namespace.
    """
    response = client.post(base, headers=_ADMIN, json={"name": name, "schema": _SCHEMA})
    assert response.status_code == 201, response.text


def _drop(client: TestClient, name: str, base: str = _TABLES) -> None:
    """Drop a table as the administrator.

    Parameters
    ----------
    client
        The test client.
    name
        The table name.
    base
        The tables URL of its namespace.
    """
    response = client.delete(f"{base}/{name}", headers=_ADMIN)
    assert response.status_code == 204, response.text


def _append(client: TestClient, name: str, source: str, base: str = _TABLES) -> None:
    """Append a row to a table, recording `source` (dotted) as its input.

    Parameters
    ----------
    client
        The test client.
    name
        The target.
    source
        The dotted table the write read from.
    base
        The tables URL of the target's namespace.
    """
    sink = io.BytesIO()
    data = pa.table({"id": pa.array([1], pa.int64())})
    with pyarrow.ipc.new_stream(sink, data.schema) as writer:
        writer.write_table(data)
    response = client.post(
        f"{base}/{name}/data",
        headers={**_ADMIN, "Content-Type": "application/vnd.apache.arrow.stream"},
        params={"source": [source]},
        content=sink.getvalue(),
    )
    assert response.status_code == 200, response.text


def _lineage_texts(
    client: TestClient, headers: dict[str, str], tables: list[str], base: str = _TABLES
) -> list[str]:
    """Fetch every lineage response the caller can obtain, as raw text.

    Parameters
    ----------
    client
        The test client.
    headers
        The caller's auth header.
    tables
        Tables to ask about, each in both directions at several depths.
    base
        The tables URL of their namespace.

    Returns
    -------
    list of str
        The bodies of the responses.
    """
    texts = []
    for table in tables:
        for direction in ("upstream", "downstream"):
            for depth in (1, 3, 10):
                response = client.get(
                    f"{base}/{table}/lineage",
                    headers=headers,
                    params={"direction": direction, "depth": depth},
                )
                assert response.status_code == 200, response.text
                texts.append(response.text)
    return texts


def _history(client: TestClient, tables: list[str]) -> tuple[str, set[str]]:
    """Learn, as the administrator, the id and the snapshot ids a hidden node must not leak.

    Parameters
    ----------
    client
        The test client.
    tables
        Tables to read lineage of.

    Returns
    -------
    tuple
        The id of the node named `gold.x` and every snapshot id on any edge.
    """
    old_x = ""
    snapshots: set[str] = set()
    for table in tables:
        body = client.get(f"{_TABLES}/{table}/lineage", headers=_ADMIN, params={"depth": 10}).json()
        for node in body["nodes"]:
            if node["name"] == "gold.x":
                old_x = node["id"]
        for edge in body["edges"]:
            snapshots |= {
                str(edge[key])
                for key in ("source_snapshot_id", "target_snapshot_id")
                if edge[key] is not None
            }
    assert old_x
    assert snapshots, "the writes should have recorded snapshot ids"
    return old_x, snapshots


def _assert_old_node_absent(texts: list[str], old_x: str, snapshots: set[str]) -> None:
    """Assert that no response mentions the old node, its snapshots, or any edge.

    Parameters
    ----------
    texts
        Raw lineage responses.
    old_x
        The dropped node's id.
    snapshots
        The snapshot ids on its edges.
    """
    for text in texts:
        body = json.loads(text)
        assert old_x not in text
        assert not any(snapshot in text for snapshot in snapshots)
        assert all(node["dropped"] is False for node in body["nodes"])
        assert all(edge["query_run_id"] is None for edge in body["edges"])


def _chain(client: TestClient) -> None:
    """Create `gold.src → gold.x → gold.t` as the administrator.

    Parameters
    ----------
    client
        The test client.
    """
    for name in ("src", "x", "t"):
        _create(client, name)
    _append(client, "x", "gold.src")
    _append(client, "t", "gold.x")


def test_a_dropped_tables_lineage_is_hidden_from_a_table_grant_on_the_new_table(
    env: TestClient,
) -> None:
    """Drop, recreate (twice) under the name: only a namespace reader and an admin see it."""
    client = env
    _chain(client)
    old_x, snapshots = _history(client, ["t"])
    owner = _principal(client, "owner")
    _grant(client, "owner", role="ANALYST", kind="NAMESPACE", environment="dev", namespace="gold")

    mallory = _principal(client, "mallory")
    for round_ in range(2):
        _drop(client, "x")
        _create(client, "x")
        if round_ == 0:
            for table in ("src", "x", "t"):
                _grant(client, "mallory", **_TABLE_GRANT, namespace="gold", table=table)
        else:
            _grant(client, "mallory", **_TABLE_GRANT, namespace="gold", table="x")
        texts = _lineage_texts(client, mallory, ["src", "x", "t"])
        _assert_old_node_absent(texts, old_x, snapshots)
        assert all(json.loads(text)["edges"] == [] for text in texts)

    from_t = client.get(f"{_TABLES}/t/lineage", headers=mallory, params={"depth": 10}).json()
    assert from_t["hidden_nodes"] == 1
    assert [n["name"] for n in from_t["nodes"]] == ["gold.t"]

    for headers in (owner, _ADMIN):
        seen = client.get(f"{_TABLES}/t/lineage", headers=headers, params={"depth": 10}).json()
        dropped_node = next(n for n in seen["nodes"] if n["id"] == old_x)
        assert (dropped_node["name"], dropped_node["dropped"]) == ("gold.x", True)
        assert {n["name"] for n in seen["nodes"]} == {"gold.t", "gold.x", "gold.src"}
        assert seen["hidden_nodes"] == 0


def test_a_view_created_under_the_old_name_does_not_show_the_old_node(env: TestClient) -> None:
    """The new `gold.x` is a view over `gold.src`; a grant on it shows none of the old history."""
    client = env
    _chain(client)
    old_x, snapshots = _history(client, ["t"])
    _drop(client, "x")
    engine = client.app.state.engine  # type: ignore[attr-defined]
    with Session(engine) as session:
        admin = ensure_bootstrap_principal(session, "admin")
        catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
        catalog.create_or_replace_view(
            admin,
            TableRef.from_dotted("dev", "gold.x"),
            sql="SELECT id FROM gold.src",
            references=catalog.resolve_lineage_sources(admin, "dev", ["gold.src"]),
            columns=[ColumnSpec(name="id", type="long", nullable=True)],
        )
    mallory = _principal(client, "mallory")
    for table in ("src", "x", "t"):
        _grant(client, "mallory", **_TABLE_GRANT, namespace="gold", table=table)

    texts = _lineage_texts(client, mallory, ["src", "x", "t"])

    _assert_old_node_absent(texts, old_x, snapshots)
    from_t = client.get(f"{_TABLES}/t/lineage", headers=mallory, params={"depth": 10}).json()
    assert [n["name"] for n in from_t["nodes"]] == ["gold.t"]
    assert from_t["hidden_nodes"] == 1
    from_x = client.get(f"{_TABLES}/x/lineage", headers=mallory, params={"depth": 10}).json()
    assert {n["name"] for n in from_x["nodes"]} == {"gold.x", "gold.src"}
    assert len(from_x["edges"]) == 1
    assert old_x not in {n["id"] for n in from_x["nodes"]}
    assert all(old_x not in (edge["source_id"], edge["target_id"]) for edge in from_x["edges"])


def test_a_task_token_never_sees_a_dropped_sibling(env: TestClient) -> None:
    """A token scoped to `gold.t`, its principal reading namespace `gold`, sees no dropped node."""
    client = env
    _chain(client)
    old_x, snapshots = _history(client, ["t"])
    _drop(client, "x")
    _principal(client, "bot")
    _grant(client, "bot", role="ANALYST", kind="NAMESPACE", environment="dev", namespace="gold")
    minted = client.post(
        "/api/v1/task-tokens",
        headers=_ADMIN,
        json={
            "principal": "bot",
            "environment": "dev",
            "asset_scope": ["gold.t"],
            "ttl_seconds": 600,
        },
    )
    assert minted.status_code == 201, minted.text
    token = {"Authorization": f"Bearer {minted.json()['token']}"}

    texts = _lineage_texts(client, token, ["t"])

    _assert_old_node_absent(texts, old_x, snapshots)
    body = client.get(f"{_TABLES}/t/lineage", headers=token, params={"depth": 10}).json()
    assert body["hidden_nodes"] == 1
    assert body["edges"] == []


def _lineage_of_t(
    client: TestClient, headers: dict[str, str]
) -> tuple[list[tuple[str, bool]], int, int]:
    """Read `gold.t`'s upstream lineage as `(sorted (name, dropped), edge count, hidden)`.

    Parameters
    ----------
    client
        The test client.
    headers
        The caller's auth header.

    Returns
    -------
    tuple
        The visible nodes, the number of edges and `hidden_nodes`.
    """
    body = client.get(f"{_TABLES}/t/lineage", headers=headers, params={"depth": 10}).json()
    nodes = sorted((n["name"], n["dropped"]) for n in body["nodes"])
    return nodes, len(body["edges"]), body["hidden_nodes"]


def test_a_recreated_namespace_does_not_inherit_the_old_exact_path_grant(env: TestClient) -> None:
    """Dropping a namespace revokes its exact-path grant; a new grant brings the history back.

    `gold.t`, outside the namespace, is fed by `gold.sales.raw`, so the dropped node stays in
    the history of a live table the holder can read. Without the revocation the holder would
    see it after the recreate, which the last step shows by granting the namespace again.
    """
    client = env
    ns_url = "/api/v1/environments/dev/namespaces"
    base = f"{ns_url}/gold.sales/tables"

    def make_namespace() -> None:
        created = client.post(ns_url, headers=_ADMIN, json={"path": "gold.sales", "properties": {}})
        assert created.status_code == 201, created.text

    make_namespace()
    _create(client, "raw", base)
    _create(client, "t")
    _append(client, "t", "gold.sales.raw")
    holder = _principal(client, "holder")
    _grant(client, "holder", **_TABLE_GRANT, namespace="gold", table="t")
    _grant(
        client,
        "holder",
        role="ANALYST",
        kind="NAMESPACE",
        environment="dev",
        namespace="gold.sales",
    )
    live = _lineage_of_t(client, holder)
    assert live == ([("gold.sales.raw", False), ("gold.t", False)], 1, 0)

    _drop(client, "raw", base)
    assert _lineage_of_t(client, holder) == (
        [("gold.sales.raw", True), ("gold.t", False)],
        1,
        0,
    ), "the exact-path grant shows the dropped node while the namespace exists"
    deleted = client.delete(f"{ns_url}/gold.sales", headers=_ADMIN)
    assert deleted.status_code == 204, deleted.text
    make_namespace()

    assert _lineage_of_t(client, holder) == ([("gold.t", False)], 0, 1)
    texts = _lineage_texts(client, holder, ["t"])
    assert all("gold.sales.raw" not in text for text in texts)

    _grant(
        client,
        "holder",
        role="ANALYST",
        kind="NAMESPACE",
        environment="dev",
        namespace="gold.sales",
    )
    assert _lineage_of_t(client, holder) == (
        [("gold.sales.raw", True), ("gold.t", False)],
        1,
        0,
    ), "a new grant on the recreated namespace shows the history again (documented)"
