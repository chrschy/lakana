"""A principal that is refused again and again does not flood the audit log."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from lakana_core.config import DenialAuditConfig
from lakana_core.db import create_engine
from lakana_core.models import AuditEvent, AuditEventType
from lakana_server.app import create_app
from lakana_server.settings import Settings
from tests.conftest import AUTH_HEADER

RATE = 3


@pytest.fixture
def limited_client(settings: Settings) -> Iterator[TestClient]:
    """Provide a client whose denial audit rate is `RATE` rows per principal per minute.

    Parameters
    ----------
    settings
        The suite's settings.

    Yields
    ------
    TestClient
        A client for the app with the small rate.
    """
    small = settings.model_copy(
        update={"denial_audit": DenialAuditConfig(per_principal_per_minute=RATE)}
    )
    with TestClient(create_app(small)) as client:
        yield client


def _person(client: TestClient, name: str) -> dict[str, str]:
    """Create a principal with no grant and return headers for its token.

    Parameters
    ----------
    client
        The client.
    name
        The principal's name.

    Returns
    -------
    dict
        The `Authorization` header.
    """
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "USER"})
    issued = client.post(
        f"/api/v1/principals/{name}/tokens", headers=AUTH_HEADER, json={"name": "t"}
    )
    return {"Authorization": f"Bearer {issued.json()['token']}"}


def _denials(client: TestClient) -> list[dict]:
    """List the recorded AUTHORIZATION_DENIED events, oldest first.

    Parameters
    ----------
    client
        The client.

    Returns
    -------
    list of dict
        The events.
    """
    events = client.get(
        "/api/v1/audit-events",
        headers=AUTH_HEADER,
        params={"event_type": "AUTHORIZATION_DENIED", "limit": 200},
    ).json()["items"]
    return list(reversed(events))


def test_a_flood_of_refused_requests_writes_a_bounded_number_of_rows(
    limited_client: TestClient,
) -> None:
    """Thirty refusals of one principal leave `RATE` rows, not thirty."""
    mallory = _person(limited_client, "mallory")
    for _ in range(30):
        assert limited_client.get("/api/v1/principals", headers=mallory).status_code == 403
    assert len(_denials(limited_client)) == RATE


def test_one_principals_flood_does_not_hide_anothers_denial(limited_client: TestClient) -> None:
    """Another principal refused during the flood still has the denial recorded."""
    mallory = _person(limited_client, "mallory")
    alice = _person(limited_client, "alice")
    for _ in range(30):
        limited_client.get("/api/v1/principals", headers=mallory)
    assert limited_client.get("/api/v1/principals", headers=alice).status_code == 403
    names = {event["principal_id"]: event for event in _denials(limited_client)}
    assert len(names) == 2


def test_the_default_rate_is_thirty_per_minute() -> None:
    """A team that sets nothing records thirty denials per principal per minute."""
    assert DenialAuditConfig().per_principal_per_minute == 30


def test_a_rate_of_zero_is_refused_at_startup() -> None:
    """A rate that would record nothing is a configuration error."""
    with pytest.raises(ValueError, match="per_principal_per_minute"):
        DenialAuditConfig(per_principal_per_minute=0)


def test_the_pending_count_is_written_as_a_summary_row_when_the_server_stops(
    settings: Settings,
) -> None:
    """Denials past the rate that no later row carried are not lost at a clean shutdown."""
    small = settings.model_copy(
        update={"denial_audit": DenialAuditConfig(per_principal_per_minute=RATE)}
    )
    with TestClient(create_app(small)) as client:
        mallory = _person(client, "mallory")
        for _ in range(10):
            client.get("/api/v1/principals", headers=mallory)
    engine = create_engine(small.database)
    with Session(engine) as session:
        rows = (
            session.query(AuditEvent)
            .filter(AuditEvent.event_type == AuditEventType.AUTHORIZATION_DENIED)
            .order_by(AuditEvent.occurred_at)
            .all()
        )
    engine.dispose()
    assert len(rows) == RATE + 1
    assert rows[-1].detail["suppressed"] == 10 - RATE
