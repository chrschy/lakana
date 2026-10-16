"""Fixtures for driving the real app through the CLI in-process."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from typer.testing import CliRunner, Result

import lakana
from lakana.cli._state import CliState
from lakana.cli.app import build_app
from lakana_core.grants.authorization import GrantAuthorization
from lakana_worker.deploy import DeploySettings
from lakana_worker.poller import poll_once

ADMIN_TOKEN = "lakana_pat_testkey_testsecret"


def make_cli_state(client: TestClient, tmp_path: Path, token: str = ADMIN_TOKEN) -> CliState:
    """Build a `CliState` whose SDK clients talk to the in-process app.

    Parameters
    ----------
    client
        The running app's `TestClient` (an `httpx.Client`).
    tmp_path
        Where the profile file lives.
    token
        The credential ordinary commands use.

    Returns
    -------
    CliState
        The state.
    """
    return CliState(
        client_factory=lambda state: lakana.Client(
            url="http://testserver", token=token, http_client=client
        ),
        login_client_factory=lambda url, tok: lakana.Client(url=url, token=tok, http_client=client),
        is_interactive=lambda: False,
        config_path=tmp_path / "lakana" / "config.toml",
    )


@pytest.fixture
def cli_state(client: TestClient, tmp_path: Path) -> CliState:
    """Provide an admin `CliState` against the in-process app.

    Parameters
    ----------
    client
        The running app.
    tmp_path
        Pytest's per-test directory.

    Returns
    -------
    CliState
        The state.
    """
    return make_cli_state(client, tmp_path)


@pytest.fixture
def invoke_cli() -> Callable[..., Result]:
    """Provide a function that runs one CLI command in-process.

    Returns
    -------
    callable
        `invoke_cli(state, *args, input=None) -> Result`.
    """

    def run(state: CliState, *args: str, input: str | None = None) -> Result:
        env = {
            name: None for name in ("LAKANA_OUTPUT", "LAKANA_PROFILE", "LAKANA_TOKEN", "LAKANA_URL")
        }
        env["COLUMNS"] = "200"
        env.update({"FORCE_COLOR": None, "PY_COLORS": None, "NO_COLOR": "1"})
        return CliRunner().invoke(build_app(), list(args), obj=state, input=input, env=env)

    return run


@pytest.fixture
def worker(client: TestClient) -> Iterator[None]:
    """Run a worker in a background thread for the test's duration.

    `lakana sql` and `lakana deploy` submit and then poll; this thread claims and
    executes queued query and deploy runs, as a real `lakana-worker` process would.

    Parameters
    ----------
    client
        The running app, for its database engine.

    Yields
    ------
    None
        While the worker runs.
    """
    engine = client.app.state.engine
    stop = threading.Event()
    errors: list[BaseException] = []

    def loop() -> None:
        """Repeatedly claim and run one queued query, backing off when idle or locked.

        Any exception that escapes an iteration ends the loop and is
        recorded in `errors`, rather than dying silently on this daemon
        thread — a thread that dies unnoticed would otherwise surface only
        as a confusing CLI query timeout, with no indication a worker
        exception was the actual cause.

        Raises
        ------
        OperationalError
            Re-raised internally when `poll_once` fails for a reason other
            than SQLite lock contention with the test-request thread; caught
            by this function's own outer handler, never propagated to the
            caller.
        """
        try:
            while not stop.is_set():
                try:
                    with Session(engine) as session:
                        claimed = poll_once(
                            session,
                            GrantAuthorization(session),
                            deploy=DeploySettings(
                                repo_roots=tuple(client.app.state.settings.repo_roots)
                            ),
                        )
                except OperationalError as exc:
                    # SQLite serializes writers; the test-request thread and
                    # this worker thread share one file-backed engine, so a
                    # "database is locked" collision is expected under
                    # concurrency, not a real failure. Any other
                    # OperationalError is a genuine bug and should still
                    # fail the test (via the outer `except` below).
                    if "database is locked" not in str(exc):
                        raise
                    claimed = False
                if not claimed:
                    stop.wait(0.05)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    yield
    stop.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    if errors:
        raise errors[0]
