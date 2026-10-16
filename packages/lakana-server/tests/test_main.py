"""Tests for the `lakana-server` console script."""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path

import pytest
import sqlalchemy

from lakana_server import __main__ as server_main
from lakana_server.log_config import uvicorn_log_config


def test_the_console_scripts_are_registered() -> None:
    """`lakana-server` and `lakana-worker` resolve to their `main` functions."""
    scripts = {ep.name: ep.value for ep in entry_points(group="console_scripts")}

    assert scripts["lakana-server"] == "lakana_server.__main__:main"
    assert scripts["lakana-worker"] == "lakana_worker.__main__:main"


def test_migrate_upgrades_the_configured_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`lakana-server migrate` applies the migrations to LAKANA_DATABASE__URL."""
    url = f"sqlite:///{tmp_path}/lakana.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", url)

    server_main.main(["migrate"])

    # Dispose the engine this test creates, or it
    # leaks a connection (a ResourceWarning under `-W error`).
    engine = sqlalchemy.create_engine(url)
    try:
        assert "environments" in sqlalchemy.inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_serve_runs_uvicorn_on_the_configured_host_and_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no subcommand it serves `create_app()` on Settings.host/port."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("LAKANA_PORT", "8765")
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(server_main.uvicorn, "run", lambda app, **kw: calls.append(kw))

    server_main.main([])

    [call] = calls
    assert {k: v for k, v in call.items() if k != "log_config"} == {
        "host": "127.0.0.1",
        "port": 8765,
        "log_level": "info",
    }
    assert call["log_config"] == uvicorn_log_config()


@pytest.mark.parametrize(
    ("given", "expected"),
    [(None, "info"), ("DEBUG", "debug"), ("warning", "warning"), ("Error", "error")],
)
def test_uvicorn_gets_the_configured_log_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, given: str | None, expected: str
) -> None:
    """`LAKANA_LOG__LEVEL` is uvicorn's level too, in the lower case it takes."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.delenv("LAKANA_LOG__LEVEL", raising=False)
    if given is not None:
        monkeypatch.setenv("LAKANA_LOG__LEVEL", given)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(server_main.uvicorn, "run", lambda app, **kw: calls.append(kw))

    server_main.main([])

    assert calls[0]["log_level"] == expected


def test_a_bad_log_level_stops_the_server_naming_the_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal comes before anything is served, and says what to write."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path}/lakana.db")
    monkeypatch.setenv("LAKANA_LOG__LEVEL", "loud")
    with pytest.raises(Exception, match=r"LAKANA_LOG__LEVEL is 'loud'.*DEBUG, INFO, WARNING or"):
        server_main.main([])


def test_an_unknown_subcommand_is_a_usage_error() -> None:
    """Argparse rejects anything but `migrate` with exit code 2."""
    with pytest.raises(SystemExit) as caught:
        server_main.main(["nope"])

    assert caught.value.code == 2
