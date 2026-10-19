"""Grants without a filter, a principal's permissions, and searching people (#147)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.models import AuditEvent, AuditEventType
from lakana_server.api.platform.listing import NEXT_CURSOR_HEADER
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment, create_table


def _user(client: TestClient, name: str) -> dict[str, str]:
    """Create a user with a token and return its auth header.

    Parameters
    ----------
    client
        The test client.
    name
        The user's name.

    Returns
    -------
    dict of str to str
        An Authorization header.
    """
    created = client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "USER"}
    )
    assert created.status_code == 201, created.text
    issued = client.post(
        f"/api/v1/principals/{name}/tokens", headers=AUTH_HEADER, json={"name": name}
    )
    return {"Authorization": f"Bearer {issued.json()['token']}"}


def _grant(client: TestClient, **body: str) -> str:
    """Create a grant as the administrator.

    Parameters
    ----------
    client
        The test client.
    **body
        The request body.

    Returns
    -------
    str
        The grant's id.
    """
    created = client.post("/api/v1/grants", headers=AUTH_HEADER, json=body)
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _denials(client: TestClient) -> int:
    """Count the recorded denials.

    Parameters
    ----------
    client
        The test client.

    Returns
    -------
    int
        The number of `AUTHORIZATION_DENIED` rows.
    """
    with Session(client.app.state.engine) as session:
        rows = session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.AUTHORIZATION_DENIED)
        ).scalars()
        return len(list(rows))


def _setup(client: TestClient) -> dict[str, str]:
    """Create `dev` with `gold.orders`, the user alice with two grants, and group `team`.

    Parameters
    ----------
    client
        The test client.

    Returns
    -------
    dict of str to str
        The grant ids by label, and alice's auth header under ``alice``.
    """
    create_environment(client)
    create_table(client, "dev", "gold", "orders")
    alice = _user(client, "alice")
    client.post("/api/v1/groups", headers=AUTH_HEADER, json={"name": "team"})
    client.put("/api/v1/groups/team/members/alice", headers=AUTH_HEADER)
    return {
        "alice": alice["Authorization"],
        "dev": _grant(
            client, principal="alice", role="VIEWER", kind="ENVIRONMENT", environment="dev"
        ),
        "orders": _grant(
            client,
            group="team",
            action="WRITE_TABLE",
            kind="TABLE",
            environment="dev",
            namespace="gold",
            table="orders",
        ),
    }


def test_grants_list_without_a_filter_and_page(client: TestClient) -> None:
    ids = _setup(client)
    every = client.get("/api/v1/grants", headers=AUTH_HEADER)
    assert every.status_code == 200, every.text
    assert [g["id"] for g in every.json()][:2] == [ids["orders"], ids["dev"]]
    assert NEXT_CURSOR_HEADER not in every.headers
    first = client.get("/api/v1/grants", headers=AUTH_HEADER, params={"limit": 1})
    assert [g["id"] for g in first.json()] == [ids["orders"]]
    second = client.get(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        params={"limit": 1, "cursor": first.headers[NEXT_CURSOR_HEADER]},
    )
    assert [g["id"] for g in second.json()] == [ids["dev"]]
    too_many = client.get("/api/v1/grants", headers=AUTH_HEADER, params={"limit": 201})
    assert too_many.status_code == 422


def test_grants_filters_combine(client: TestClient) -> None:
    ids = _setup(client)
    by_action = client.get("/api/v1/grants", headers=AUTH_HEADER, params={"action": "write_table"})
    assert ids["orders"] in {g["id"] for g in by_action.json()}
    assert ids["dev"] not in {g["id"] for g in by_action.json()}
    table = client.get(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        params={"group_name": "team", "environment": "dev", "namespace": "gold", "table": "orders"},
    )
    assert [g["id"] for g in table.json()] == [ids["orders"]]
    alice_in_dev = client.get(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        params={"principal_name": "alice", "environment": "dev"},
    )
    assert [g["id"] for g in alice_in_dev.json()] == [ids["dev"]]


def test_grants_refuse_contradictory_filters(client: TestClient) -> None:
    _setup(client)
    both = client.get(
        "/api/v1/grants", headers=AUTH_HEADER, params={"principal_name": "alice", "group_name": "t"}
    )
    assert both.status_code == 422
    assert "at most one" in both.json()["detail"]
    gap = client.get("/api/v1/grants", headers=AUTH_HEADER, params={"namespace": "gold"})
    assert gap.status_code == 422
    assert "environment" in gap.json()["detail"]
    unknown = client.get("/api/v1/grants", headers=AUTH_HEADER, params={"action": "FLY"})
    assert unknown.status_code == 422
    assert "READ_TABLE" in unknown.json()["detail"]


def test_a_non_manager_may_not_list_grants(client: TestClient) -> None:
    ids = _setup(client)
    refused = client.get("/api/v1/grants", headers={"Authorization": ids["alice"]})
    assert refused.status_code == 403
    assert _denials(client) == 1


def _permission(body: dict[str, object], action: str) -> dict[str, object]:
    """Pick one action's entry of a permissions answer.

    Parameters
    ----------
    body
        The response body.
    action
        The action.

    Returns
    -------
    dict
        Its entry.
    """
    entries = body["permissions"]
    assert isinstance(entries, list)
    return next(entry for entry in entries if entry["action"] == action)


def test_an_administrator_explains_a_principal_on_a_table(client: TestClient) -> None:
    ids = _setup(client)
    answer = client.get(
        "/api/v1/principals/alice/permissions",
        headers=AUTH_HEADER,
        params={"environment": "dev", "namespace": "gold", "table": "orders"},
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert (body["principal"], body["active"], body["note"]) == ("alice", True, None)
    assert body["scope"] == {
        "kind": "TABLE",
        "environment": "dev",
        "namespace": "gold",
        "table": "orders",
    }
    assert _permission(body, "READ_TABLE") == {
        "action": "READ_TABLE",
        "allowed": True,
        "via": [
            {
                "kind": "role",
                "grant_id": ids["dev"],
                "group": None,
                "role": "VIEWER",
                "scope": {
                    "kind": "ENVIRONMENT",
                    "environment": "dev",
                    "namespace": None,
                    "table": None,
                },
            }
        ],
        "reason": None,
    }
    write = _permission(body, "WRITE_TABLE")
    assert write["via"] == [
        {
            "kind": "group",
            "grant_id": ids["orders"],
            "group": "team",
            "role": None,
            "scope": body["scope"],
        }
    ]
    drop = _permission(body, "DROP_TABLE")
    assert (drop["allowed"], drop["via"], drop["reason"]) == (
        False,
        [],
        "'alice' may not DROP_TABLE this.",
    )


def test_the_explanation_matches_what_the_routes_decide(client: TestClient) -> None:
    ids = _setup(client)
    alice = {"Authorization": ids["alice"]}
    answer = client.get(
        "/api/v1/principals/alice/permissions",
        headers=alice,
        params={"environment": "dev", "namespace": "gold", "table": "orders"},
    ).json()
    described = client.get("/api/v1/environments/dev/namespaces/gold/tables/orders", headers=alice)
    assert _permission(answer, "READ_TABLE")["allowed"] is (described.status_code == 200)
    dropped = client.delete("/api/v1/environments/dev/namespaces/gold/tables/orders", headers=alice)
    assert _permission(answer, "DROP_TABLE")["allowed"] is (dropped.status_code != 403)
    instance = client.get("/api/v1/principals/alice/permissions", headers=alice).json()
    assert instance["scope"]["kind"] == "INSTANCE"
    listed = client.get("/api/v1/grants", headers=alice)
    assert _permission(instance, "MANAGE_GRANT")["allowed"] is (listed.status_code == 200)


def test_a_principal_sees_its_own_permissions_but_not_anothers(client: TestClient) -> None:
    ids = _setup(client)
    _user(client, "bob")
    alice = {"Authorization": ids["alice"]}
    own = client.get(
        "/api/v1/principals/alice/permissions", headers=alice, params={"environment": "dev"}
    )
    assert own.status_code == 200, own.text
    assert _permission(own.json(), "READ_ENVIRONMENT")["allowed"] is True
    before = _denials(client)
    for name in ("bob", "nobody"):
        refused = client.get(f"/api/v1/principals/{name}/permissions", headers=alice)
        assert refused.status_code == 403, refused.text
    assert _denials(client) == before + 2
    missing_scope = client.get(
        "/api/v1/principals/alice/permissions", headers=alice, params={"environment": "prod"}
    )
    assert missing_scope.status_code == 200
    assert not any(p["allowed"] for p in missing_scope.json()["permissions"])


def test_an_administrator_gets_404_for_what_does_not_exist(client: TestClient) -> None:
    _setup(client)
    nobody = client.get("/api/v1/principals/nobody/permissions", headers=AUTH_HEADER)
    assert nobody.status_code == 404
    prod = client.get(
        "/api/v1/principals/alice/permissions", headers=AUTH_HEADER, params={"environment": "prod"}
    )
    assert prod.status_code == 404
    gap = client.get(
        "/api/v1/principals/alice/permissions", headers=AUTH_HEADER, params={"table": "orders"}
    )
    assert gap.status_code == 422


def test_a_deactivated_principal_is_flagged(client: TestClient) -> None:
    _setup(client)
    client.delete("/api/v1/principals/alice", headers=AUTH_HEADER)
    body = client.get("/api/v1/principals/alice/permissions", headers=AUTH_HEADER).json()
    assert body["active"] is False
    assert "deactivated" in body["note"]


def test_principals_are_searched_and_paged(client: TestClient) -> None:
    for name in ("Carol_X", "carolyx", "100%-sure"):
        client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "USER"})
    found = client.get("/api/v1/principals", headers=AUTH_HEADER, params={"q": "l_X"})
    assert [p["name"] for p in found.json()] == ["Carol_X"]
    found = client.get("/api/v1/principals", headers=AUTH_HEADER, params={"q": "0%"})
    assert [p["name"] for p in found.json()] == ["100%-sure"]
    first = client.get("/api/v1/principals", headers=AUTH_HEADER, params={"limit": 2})
    assert [p["name"] for p in first.json()] == ["100%-sure", "carolyx"]
    rest = client.get(
        "/api/v1/principals",
        headers=AUTH_HEADER,
        params={"limit": 2, "cursor": first.headers[NEXT_CURSOR_HEADER]},
    )
    assert [p["name"] for p in rest.json()] == ["Carol_X", "admin"]
    assert NEXT_CURSOR_HEADER not in rest.headers


def test_groups_are_searched_and_paged(client: TestClient) -> None:
    for name in ("data_eng", "dataxeng"):
        client.post("/api/v1/groups", headers=AUTH_HEADER, json={"name": name})
    found = client.get("/api/v1/groups", headers=AUTH_HEADER, params={"q": "A_E"})
    assert [g["name"] for g in found.json()] == ["data_eng"]
    first = client.get("/api/v1/groups", headers=AUTH_HEADER, params={"limit": 1})
    assert [g["name"] for g in first.json()] == ["dataxeng"]
    rest = client.get(
        "/api/v1/groups",
        headers=AUTH_HEADER,
        params={"limit": 1, "cursor": first.headers[NEXT_CURSOR_HEADER]},
    )
    assert [g["name"] for g in rest.json()] == ["data_eng"]
    other = client.get(
        "/api/v1/groups", headers=AUTH_HEADER, params={"cursor": first.headers[NEXT_CURSOR_HEADER]}
    )
    assert other.status_code == 200
    wrong = client.get(
        "/api/v1/principals",
        headers=AUTH_HEADER,
        params={"cursor": first.headers[NEXT_CURSOR_HEADER]},
    )
    assert wrong.status_code == 422
