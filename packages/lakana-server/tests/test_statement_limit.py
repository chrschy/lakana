"""Tests for the longest SQL statement the server accepts."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from lakana_core.config import QueryLimitsConfig
from lakana_server.app import create_app
from lakana_server.settings import Settings
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment, create_table

LIMIT = 40
"""The small limit these tests set; every long statement is a few dozen characters."""


@pytest.fixture
def small_client(settings: Settings) -> Iterator[TestClient]:
    """Provide a client whose statement limit is `LIMIT` characters.

    Parameters
    ----------
    settings
        The suite's settings.

    Yields
    ------
    TestClient
        A client for an app with the small limit.
    """
    small = settings.model_copy(
        update={"query_limits": QueryLimitsConfig(max_statement_chars=LIMIT)}
    )
    with TestClient(create_app(small)) as client:
        yield client


def _statement(length: int) -> str:
    """Build a valid statement of exactly `length` characters.

    Parameters
    ----------
    length
        The length, at least 12.

    Returns
    -------
    str
        ``SELECT '...'`` padded with a string literal.
    """
    return "SELECT '" + "a" * (length - 9) + "'"


def _assert_refused(response: Response) -> None:
    """Assert a 422 that names the limit and the setting and echoes none of the statement.

    Parameters
    ----------
    response
        The response.
    """
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert str(LIMIT) in detail
    assert "LAKANA_QUERY_LIMITS__MAX_STATEMENT_CHARS" in detail
    assert "aaaa" not in detail


def test_the_default_limit_is_a_hundred_thousand_characters() -> None:
    """A team that sets nothing can still submit any statement a person writes by hand."""
    assert QueryLimitsConfig().max_statement_chars == 100_000


def test_a_limit_of_zero_is_refused_at_startup() -> None:
    """A limit that would refuse every statement is a configuration error."""
    with pytest.raises(ValueError, match="max_statement_chars"):
        QueryLimitsConfig(max_statement_chars=0)


def test_a_statement_over_the_limit_is_refused_at_submit(small_client: TestClient) -> None:
    """A query longer than the limit is a 422 before it is queued."""
    create_environment(small_client)
    _assert_refused(
        small_client.post(
            "/api/v1/environments/dev/queries",
            headers=AUTH_HEADER,
            json={"sql": _statement(LIMIT + 1)},
        )
    )


def test_a_statement_of_exactly_the_limit_is_accepted(small_client: TestClient) -> None:
    """The limit is inclusive."""
    create_environment(small_client)
    response = small_client.post(
        "/api/v1/environments/dev/queries", headers=AUTH_HEADER, json={"sql": _statement(LIMIT)}
    )
    assert response.status_code == 202, response.text


def test_a_write_statement_over_the_limit_is_refused_at_submit(small_client: TestClient) -> None:
    """A query with a target (a write run) has the same limit."""
    create_environment(small_client)
    _assert_refused(
        small_client.post(
            "/api/v1/environments/dev/queries",
            headers=AUTH_HEADER,
            json={
                "sql": _statement(LIMIT + 1),
                "target": {"table": "gold.t", "mode": "append"},
            },
        )
    )


def _alice(client: TestClient) -> None:
    """Create the environment, a table and the principal the policy tests use.

    Parameters
    ----------
    client
        The test client.
    """
    create_environment(client)
    create_table(client, "dev", "gold", "customers")
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "alice", "type": "USER"})


def _policy(expression: str) -> dict[str, object]:
    """Build a row filter or column mask body.

    Parameters
    ----------
    expression
        The expression.

    Returns
    -------
    dict
        The request body.
    """
    return {
        "principal": "alice",
        "environment": "dev",
        "namespace": "gold",
        "table": "customers",
        "expression": expression,
    }


def test_a_row_filter_expression_over_the_limit_is_refused(small_client: TestClient) -> None:
    """The row-filter route applies the same limit to its expression."""
    _alice(small_client)
    long = "id = " + "1" * (LIMIT - 4)
    _assert_refused(
        small_client.post("/api/v1/row-filters", headers=AUTH_HEADER, json=_policy(long))
    )
    short = small_client.post("/api/v1/row-filters", headers=AUTH_HEADER, json=_policy("id = 1"))
    assert short.status_code == 201, short.text


def test_a_column_mask_expression_over_the_limit_is_refused(small_client: TestClient) -> None:
    """The column-mask route applies the same limit to its expression."""
    _alice(small_client)
    long = "'" + "a" * (LIMIT - 1) + "'"
    _assert_refused(
        small_client.post(
            "/api/v1/column-masks", headers=AUTH_HEADER, json={**_policy(long), "column": "id"}
        )
    )
    short = small_client.post(
        "/api/v1/column-masks", headers=AUTH_HEADER, json={**_policy("NULL"), "column": "id"}
    )
    assert short.status_code == 201, short.text


def test_settings_read_the_limit_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """`LAKANA_QUERY_LIMITS__MAX_STATEMENT_CHARS` sets the limit."""
    monkeypatch.setenv("LAKANA_QUERY_LIMITS__MAX_STATEMENT_CHARS", "500")
    loaded = Settings(database=settings.database, secrets=settings.secrets)
    assert loaded.query_limits.max_statement_chars == 500
