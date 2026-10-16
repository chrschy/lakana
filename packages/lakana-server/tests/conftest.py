"""Shared fixtures for lakana-server tests."""

import base64
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.config import DatabaseConfig, SecretsConfig
from lakana_core.db import create_engine
from lakana_core.models import Base
from lakana_server.app import create_app
from lakana_server.auth_rate_limit import AuthAuditLimiter
from lakana_server.settings import Settings

_TEST_MASTER_KEY = base64.b64encode(b"0" * 32).decode()
"""A valid base64-encoded 32-byte master key, matching the fixed test key
used throughout lakana-core's own secrets tests (e.g.
`test_secrets_service.py`) — deterministic, never a real secret."""

AUTH_HEADER = {"Authorization": "Bearer lakana_pat_testkey_testsecret"}
"""A well-formed Authorization header accepted by get_principal. Real
verification now runs (see lakana_server.deps.get_principal) — this
authenticates because the settings fixture's Settings mints the bootstrap
principal's PAT with this exact key_id and secret."""


@pytest.fixture
def session() -> Iterator[Session]:
    """Provide a session against a fresh in-memory SQLite database.

    Used by tests that exercise `lakana_server.deps.get_principal` directly,
    independent of any route or running app.

    Yields
    ------
    Session
        A session whose schema has been created from the models.
    """
    engine = create_engine(DatabaseConfig(url="sqlite://"))
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Provide settings backed by a fresh SQLite database.

    Parameters
    ----------
    tmp_path
        Pytest's per-test temporary directory.

    Returns
    -------
    Settings
        Configuration pointed at an isolated database.
    """
    engine = create_engine(DatabaseConfig(url=f"sqlite:///{tmp_path}/lakana.db"))
    Base.metadata.create_all(engine)
    engine.dispose()
    return Settings(
        database=DatabaseConfig(url=f"sqlite:///{tmp_path}/lakana.db"),
        bootstrap_pat_key_id="testkey",
        bootstrap_pat_secret="testsecret",
        secrets=SecretsConfig(master_key=_TEST_MASTER_KEY),
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """Provide a `TestClient` around a fully constructed app.

    Wraps `TestClient` as a context manager so the lifespan runs before any
    test issues a request, guaranteeing the bootstrap principal exists.

    Parameters
    ----------
    settings
        Configuration pointed at an isolated database.

    Yields
    ------
    TestClient
        A client for the running app.
    """
    with TestClient(create_app(settings)) as c:
        yield c


@pytest.fixture
def storage_root(tmp_path: Path) -> str:
    """Provide a per-test storage root under pytest's temporary directory.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.

    Returns
    -------
    str
        A `file://` URL no other test shares.
    """
    return f"file://{tmp_path}/lake"


@pytest.fixture(autouse=True)
def _isolate_create_environment_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point `server_test_helpers.create_environment`'s default root at `tmp_path`.

    Applies to every test in this suite, not just ones that request it
    explicitly, so a test calling `create_environment` with no `root`
    argument still gets an isolated storage location rather than
    `create_environment`'s own module-level fallback (a fixed
    `tempfile.gettempdir()`-based path, used only outside pytest).

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    monkeypatch
        Used to replace the module attribute for the duration of the test.
    """
    from tests import server_test_helpers

    monkeypatch.setattr(server_test_helpers, "STORAGE_PARENT", tmp_path)


def fake_request(
    limiter: AuthAuditLimiter | None = None, client_host: str | None = "1.2.3.4"
) -> Request:
    """Build a minimal stand-in for `Request`, for calling `get_principal` directly.

    Carries only the attributes `get_principal` actually reads: `app.state.
    auth_audit_limiter`, `client.host` and the per-request `state`. Matches this test suite's
    existing pattern (see `test_deps.py`) of a `SimpleNamespace` `cast` to
    `Request` rather than constructing a real ASGI request.

    Parameters
    ----------
    limiter
        The limiter `get_principal` should consult on a failed attempt.
        Defaults to a fresh `AuthAuditLimiter` with a generous rate, so
        tests that don't care about rate limiting never see it interfere.
    client_host
        The value `request.client.host` should report, or `None` to
        simulate a request with no client info at all (`request.client` is
        `None`).

    Returns
    -------
    Request
        A `SimpleNamespace` cast to `Request`, carrying only the attributes
        above.
    """
    client = SimpleNamespace(host=client_host) if client_host is not None else None
    state = SimpleNamespace(
        auth_audit_limiter=limiter or AuthAuditLimiter(1000),
        denial_audit_limiter=DenialAuditLimiter(1000),
    )
    return cast(
        Request,
        SimpleNamespace(app=SimpleNamespace(state=state), client=client, state=SimpleNamespace()),
    )
