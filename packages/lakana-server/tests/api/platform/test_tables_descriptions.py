"""The description routes: set and reset a table's or a column's description."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.models import AuditEvent, AuditEventType, Table
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment

_TABLES = "/api/v1/environments/dev/namespaces/gold/tables"


def _setup(client: TestClient) -> None:
    """Create `dev` with the table `gold.t` (columns `id` and `note`).

    Parameters
    ----------
    client
        The test client.
    """
    create_environment(client)
    created = client.post(
        _TABLES,
        headers=AUTH_HEADER,
        json={
            "name": "t",
            "schema": [
                {"name": "id", "type": "long", "nullable": False},
                {"name": "note", "type": "string", "nullable": True},
            ],
        },
    )
    assert created.status_code == 201, created.text


def _person(client: TestClient, name: str, *actions: str) -> dict[str, str]:
    """Create a user holding only `actions` on `dev` and return its headers.

    Parameters
    ----------
    client
        The test client.
    name
        The principal's name.
    *actions
        Actions granted at environment scope.

    Returns
    -------
    dict
        The `Authorization` header.
    """
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "USER"})
    for action in actions:
        granted = client.post(
            "/api/v1/grants",
            headers=AUTH_HEADER,
            json={"principal": name, "action": action, "kind": "ENVIRONMENT", "environment": "dev"},
        )
        assert granted.status_code < 300, granted.text
    issued = client.post(
        f"/api/v1/principals/{name}/tokens", headers=AUTH_HEADER, json={"name": name}
    )
    return {"Authorization": f"Bearer {issued.json()['token']}"}


def test_a_table_shows_no_description_until_one_is_set(client: TestClient) -> None:
    _setup(client)
    table = client.get(f"{_TABLES}/t", headers=AUTH_HEADER).json()
    assert table["description"] is None
    assert table["description_source"] is None
    assert {(c["name"], c["description"], c["description_source"]) for c in table["schema"]} == {
        ("id", None, None),
        ("note", None, None),
    }


def test_patch_sets_a_table_description_and_null_resets_it(client: TestClient) -> None:
    _setup(client)
    set_ = client.patch(f"{_TABLES}/t", headers=AUTH_HEADER, json={"description": "Orders"})
    assert set_.status_code == 200, set_.text
    assert (set_.json()["description"], set_.json()["description_source"]) == ("Orders", "edited")
    listed = client.get(_TABLES, headers=AUTH_HEADER).json()
    assert listed[0]["description"] == "Orders"

    reset = client.patch(f"{_TABLES}/t", headers=AUTH_HEADER, json={"description": None})
    assert reset.status_code == 200, reset.text
    assert reset.json()["description"] is None
    assert reset.json()["description_source"] is None


def test_put_sets_a_column_description_and_null_resets_it(client: TestClient) -> None:
    _setup(client)
    url = f"{_TABLES}/t/columns/note/description"
    set_ = client.put(url, headers=AUTH_HEADER, json={"description": "Free text"})
    assert set_.status_code == 200, set_.text
    columns = {c["name"]: c for c in set_.json()["schema"]}
    assert (columns["note"]["description"], columns["note"]["description_source"]) == (
        "Free text",
        "edited",
    )
    assert columns["id"]["description"] is None

    reset = client.put(url, headers=AUTH_HEADER, json={"description": None})
    assert {c["name"]: c["description"] for c in reset.json()["schema"]}["note"] is None


def test_an_unknown_column_is_404_and_names_the_columns(client: TestClient) -> None:
    _setup(client)
    response = client.put(
        f"{_TABLES}/t/columns/nope/description", headers=AUTH_HEADER, json={"description": "x"}
    )
    assert response.status_code == 404, response.text
    assert "id, note" in response.text


def test_a_description_over_4000_characters_is_422_with_the_next_step(client: TestClient) -> None:
    _setup(client)
    response = client.patch(f"{_TABLES}/t", headers=AUTH_HEADER, json={"description": "x" * 4001})
    assert response.status_code == 422, response.text
    assert "4,000" in response.text and "shorter" in response.text
    ok = client.patch(f"{_TABLES}/t", headers=AUTH_HEADER, json={"description": "x" * 4000})
    assert ok.status_code == 200, ok.text


def test_a_missing_description_field_is_422(client: TestClient) -> None:
    _setup(client)
    assert client.patch(f"{_TABLES}/t", headers=AUTH_HEADER, json={}).status_code == 422


def test_a_reader_may_not_describe_and_the_refusal_is_audited(client: TestClient) -> None:
    _setup(client)
    reader = _person(client, "reader", "READ_TABLE", "READ_NAMESPACE", "READ_ENVIRONMENT")
    refused = client.patch(f"{_TABLES}/t", headers=reader, json={"description": "No"})
    assert refused.status_code == 403, refused.text
    refused_column = client.put(
        f"{_TABLES}/t/columns/id/description", headers=reader, json={"description": "No"}
    )
    assert refused_column.status_code == 403, refused_column.text
    assert client.get(f"{_TABLES}/t", headers=AUTH_HEADER).json()["description"] is None
    with Session(client.app.state.engine) as session:
        denied = session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.AUTHORIZATION_DENIED)
        ).all()
        assert any("DROP_TABLE" in str(event.detail) for event in denied)
        assert (
            session.scalars(
                select(AuditEvent).where(AuditEvent.event_type == AuditEventType.TABLE_DESCRIBED)
            ).all()
            == []
        )


def test_a_manager_may_describe_and_the_change_is_audited(client: TestClient) -> None:
    _setup(client)
    manager = _person(
        client,
        "manager",
        "READ_TABLE",
        "READ_NAMESPACE",
        "READ_ENVIRONMENT",
        "DROP_TABLE",
    )
    done = client.patch(f"{_TABLES}/t", headers=manager, json={"description": "By the manager"})
    assert done.status_code == 200, done.text
    column = client.put(
        f"{_TABLES}/t/columns/id/description", headers=manager, json={"description": "Key"}
    )
    assert column.status_code == 200, column.text
    with Session(client.app.state.engine) as session:
        events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type.in_(
                    [AuditEventType.TABLE_DESCRIBED, AuditEventType.COLUMN_DESCRIBED]
                )
            )
        ).all()
        assert sorted(e.event_type.value for e in events) == ["COLUMN_DESCRIBED", "TABLE_DESCRIBED"]
        assert all("By the manager" not in str(e.detail) for e in events)


def test_the_creator_owns_the_table_and_may_describe_it(client: TestClient) -> None:
    _setup(client)
    owner = _person(client, "maker", "CREATE_TABLE", "READ_TABLE", "READ_NAMESPACE", "WRITE_TABLE")
    created = client.post(
        _TABLES,
        headers=owner,
        json={"name": "mine", "schema": [{"name": "id", "type": "long", "nullable": False}]},
    )
    assert created.status_code == 201, created.text
    done = client.patch(f"{_TABLES}/mine", headers=owner, json={"description": "Mine"})
    assert done.status_code == 200, done.text
    with Session(client.app.state.engine) as session:
        row = session.scalars(select(Table).where(Table.name == "mine")).one()
        assert row.description_edited == "Mine"
    other = client.patch(f"{_TABLES}/t", headers=owner, json={"description": "Not mine"})
    assert other.status_code == 403, other.text
