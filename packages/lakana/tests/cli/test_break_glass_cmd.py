"""`--break-glass --reason` on the write commands."""

from __future__ import annotations

import json
import re
from pathlib import Path

from conftest import FakeApi, invoke, make_state

_REASON = "incident 42: fix one row"


def _plain(text: str) -> str:
    """Return CLI output without ANSI codes or Rich box borders, whitespace collapsed.

    Parameters
    ----------
    text
        Raw stdout or stderr.

    Returns
    -------
    str
        The plain, single-spaced text.
    """
    plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
    for border in "│╭╮╰╯─":
        plain = plain.replace(border, " ")
    return " ".join(plain.split())


def test_table_delete_sends_the_reason_and_warns(tmp_path: Path) -> None:
    """The reason goes in the query string; the CLI warns that a run may overwrite the fix."""
    api = FakeApi()
    api.on("DELETE", "/api/v1/environments/prod/namespaces/bronze/tables/t", status=204)
    result = invoke(
        make_state(tmp_path, api),
        "table",
        "delete",
        "bronze.t",
        "--env",
        "prod",
        "--yes",
        "--break-glass",
        "--reason",
        _REASON,
        env={"GITHUB_ACTIONS": "true"},
    )
    assert result.exit_code == 0, result.output
    assert api.requests[-1].url.params["break_glass_reason"] == _REASON
    assert "next run of the deployed code may overwrite" in _plain(result.stderr)


def test_ns_create_sends_the_reason_in_the_body(tmp_path: Path) -> None:
    """JSON-body endpoints get the field."""
    api = FakeApi()
    api.on(
        "POST",
        "/api/v1/environments/prod/namespaces",
        status=201,
        json={"path": "bronze.eu", "properties": {}},
    )
    result = invoke(
        make_state(tmp_path, api),
        "ns",
        "create",
        "bronze.eu",
        "--env",
        "prod",
        "--break-glass",
        "--reason",
        _REASON,
    )
    assert result.exit_code == 0, result.output
    assert json.loads(api.requests[-1].content)["break_glass_reason"] == _REASON


def test_flag_errors_are_usage_errors(tmp_path: Path) -> None:
    """`--break-glass` needs `--reason`; `--reason` needs `--break-glass`; sql needs `--into`."""
    state = make_state(tmp_path, FakeApi())
    assert invoke(state, "ns", "create", "x.y", "--env", "prod", "--break-glass").exit_code == 2
    assert invoke(state, "ns", "create", "x.y", "--env", "prod", "--reason", _REASON).exit_code == 2
    assert (
        invoke(
            state, "sql", "SELECT 1", "--env", "prod", "--break-glass", "--reason", _REASON
        ).exit_code
        == 2
    )


def test_without_break_glass_no_field_is_sent(tmp_path: Path) -> None:
    """Existing requests are unchanged when the flag is absent."""
    api = FakeApi()
    api.on(
        "POST",
        "/api/v1/environments/dev/namespaces",
        status=201,
        json={"path": "bronze.eu", "properties": {}},
    )
    invoke(make_state(tmp_path, api), "ns", "create", "bronze.eu", "--env", "dev")
    assert "break_glass_reason" not in json.loads(api.requests[-1].content)


_BASE = "/api/v1/environments/prod/namespaces/bronze/tables"
_TABLE = {
    "name": "sales",
    "namespace": "bronze",
    "uuid": "u1",
    "metadata_location": "file:///m.json",
    "schema": [{"name": "id", "type": "long", "nullable": False}],
}
_GLASS = ("--break-glass", "--reason", _REASON)


def test_table_create_and_create_external_send_the_reason_in_the_body(tmp_path: Path) -> None:
    """Both create commands put the field in their JSON body."""
    api = FakeApi()
    api.on("POST", _BASE, status=201, json=_TABLE)
    api.on(
        "POST",
        f"{_BASE}/external",
        status=201,
        json={
            "name": "raw",
            "namespace": "bronze",
            "location_pattern": "raw/x/*.csv",
            "format": "csv",
            "schema": {"a": "string"},
        },
    )
    state = make_state(tmp_path, api)
    created = invoke(
        state, "table", "create", "bronze.sales", "--env", "prod", "--column", "id:long", *_GLASS
    )
    assert created.exit_code == 0, created.output
    assert json.loads(api.requests[-1].content)["break_glass_reason"] == _REASON
    external = invoke(
        state,
        "table",
        "create-external",
        "bronze.raw",
        "--env",
        "prod",
        "--location",
        "raw/x/*.csv",
        "--format",
        "csv",
        *_GLASS,
    )
    assert external.exit_code == 0, external.output
    assert json.loads(api.requests[-1].content)["break_glass_reason"] == _REASON


def test_table_append_and_replace_send_the_reason_as_a_query_parameter(tmp_path: Path) -> None:
    """Arrow-body endpoints carry the reason in the query string."""
    api = FakeApi()
    api.on("GET", f"{_BASE}/sales", json=_TABLE)
    api.on("POST", f"{_BASE}/sales/data", json={"rows": 1, "files": 1, "snapshot_id": 9})
    api.on("PUT", f"{_BASE}/sales/data", json={"rows": 1, "files": 1, "snapshot_id": 4})
    csv = tmp_path / "data.csv"
    csv.write_text("id\n1\n")
    state = make_state(tmp_path, api)
    for command in ("append", "replace"):
        result = invoke(state, "table", command, "bronze.sales", str(csv), "--env", "prod", *_GLASS)
        assert result.exit_code == 0, result.output
        assert api.requests[-1].url.params["break_glass_reason"] == _REASON


def test_ns_delete_sends_the_reason_as_a_query_parameter(tmp_path: Path) -> None:
    """`ns delete --yes --break-glass` puts the reason in the query string."""
    api = FakeApi()
    api.on("DELETE", "/api/v1/environments/prod/namespaces/bronze.eu", status=204)
    result = invoke(
        make_state(tmp_path, api), "ns", "delete", "bronze.eu", "--env", "prod", "--yes", *_GLASS
    )
    assert result.exit_code == 0, result.output
    assert api.requests[-1].url.params["break_glass_reason"] == _REASON


def test_sql_into_sends_the_reason_in_the_query_body(tmp_path: Path) -> None:
    """`sql --into … --break-glass` sends the field with the write target."""
    api = FakeApi()
    queries = "/api/v1/environments/prod/queries"
    api.on("POST", queries, status=202, json={"query_id": "q1"})
    api.on(
        "GET",
        f"{queries}/q1",
        json={
            "query_id": "q1",
            "status": "SUCCEEDED",
            "error_message": None,
            "lineage": None,
            "write_mode": "append",
            "target": "bronze.sales",
            "rows_written": 1,
            "snapshot_id": 7,
            "watermark": None,
            "schema_diff": None,
        },
    )
    result = invoke(
        make_state(tmp_path, api),
        "sql",
        "SELECT 1::BIGINT AS id",
        "--into",
        "bronze.sales",
        "--mode",
        "append",
        "--env",
        "prod",
        *_GLASS,
    )
    assert result.exit_code == 0, result.output
    posted = next(r for r in api.requests if r.method == "POST")
    assert json.loads(posted.content)["break_glass_reason"] == _REASON
