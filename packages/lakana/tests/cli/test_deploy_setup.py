"""Tests for the Compose bundle's first-run setup."""

from __future__ import annotations

import io
import json

import httpx
import pytest
from conftest import FakeApi

import lakana
from lakana.cli.deploy import setup
from lakana.errors import LakanaConnectionError, ServerError

_ENV_PATH = "/api/v1/environments/dev"
_SECRETS_PATH = "/api/v1/environments/dev/secrets"
_STORAGE_PATH = "/api/v1/environments/dev/storage"
_SECRET = {
    "id": "00000000-0000-0000-0000-000000000001",
    "name": "seaweedfs-s3",
    "environment": "dev",
    "created_by": "admin",
    "created_at": "2026-09-28T00:00:00+00:00",
    "format_version": 1,
    "key_id": "env",
}


def _dev(secret_ref: str | None, *, root: str = "s3://lakana/dev") -> dict[str, object]:
    """Return an environment document for `dev`.

    Parameters
    ----------
    secret_ref
        Its storage secret reference.
    root
        Its storage root.

    Returns
    -------
    dict
        The JSON the server returns for `dev`.
    """
    return {
        "name": "dev",
        "storage_root": root,
        "storage_secret_ref": secret_ref,
        "is_protected": False,
        "properties": {},
    }


def test_first_run_creates_dev_then_the_secret_then_binds_it() -> None:
    """Secrets are environment-scoped, so dev comes first."""
    api = FakeApi()
    api.on("GET", _ENV_PATH, status=404, json={"detail": "not found"})
    api.on("POST", "/api/v1/environments", status=201, json=_dev(None))
    api.on("GET", _SECRETS_PATH, status=200, json=[])
    api.on("POST", _SECRETS_PATH, status=201, json=_SECRET)
    api.on("PATCH", _STORAGE_PATH, status=200, json=_dev("seaweedfs-s3"))

    setup.run_setup(api.client(), access_key="ak", secret_key="sk", out=io.StringIO())

    assert [(r.method, r.url.path) for r in api.requests] == [
        ("GET", _ENV_PATH),
        ("POST", "/api/v1/environments"),
        ("GET", _SECRETS_PATH),
        ("POST", _SECRETS_PATH),
        ("PATCH", _STORAGE_PATH),
    ]
    assert json.loads(api.requests[1].content) == {
        "name": "dev",
        "storage": {
            "scheme": "s3",
            "root": "s3://lakana/dev",
            "options": {"s3.endpoint": "http://seaweedfs:8333"},
        },
    }
    secret_body = json.loads(api.requests[3].content)
    assert secret_body["name"] == "seaweedfs-s3"
    assert secret_body["values"] == {"s3.access-key-id": "ak", "s3.secret-access-key": "sk"}
    assert json.loads(api.requests[4].content) == {
        "options": {"s3.endpoint": "http://seaweedfs:8333"},
        "secret_ref": "seaweedfs-s3",
    }


_ROTATE_PATH = "/api/v1/environments/dev/secrets/seaweedfs-s3"
_SECRET_2 = {**_SECRET, "format_version": 2}


def test_format_1_storage_secret_is_set_again_once() -> None:
    """The bundle's own secret at format 1 is rotated with its own values, and says so."""
    api = FakeApi()
    api.on("GET", _ENV_PATH, status=200, json=_dev("seaweedfs-s3"))
    api.on("GET", _SECRETS_PATH, status=200, json=[_SECRET])
    api.on("PATCH", _ROTATE_PATH, status=200, json=_SECRET_2)
    out = io.StringIO()

    setup.run_setup(api.client(), access_key="ak", secret_key="sk", out=out)

    rotates = [r for r in api.requests if r.url.path == _ROTATE_PATH]
    assert len(rotates) == 1
    assert json.loads(rotates[0].content) == {
        "values": {"s3.access-key-id": "ak", "s3.secret-access-key": "sk"}
    }
    assert out.getvalue().count("again") == 1
    assert "seaweedfs-s3" in out.getvalue()


def test_second_run_after_rotation_rotates_nothing() -> None:
    """Once the listing shows format 2, a restart rotates nothing."""
    api = FakeApi()
    api.on("GET", _ENV_PATH, status=200, json=_dev("seaweedfs-s3"))
    api.on("GET", _SECRETS_PATH, status=200, json=[_SECRET_2])
    out = io.StringIO()

    setup.run_setup(api.client(), access_key="ak", secret_key="sk", out=out)
    setup.run_setup(api.client(), access_key="ak", secret_key="sk", out=out)

    assert [r.method for r in api.requests] == ["GET"] * 4
    assert out.getvalue() == ""


def test_other_secrets_at_format_1_are_left_alone() -> None:
    """Only the bundle's own secret is set again."""
    other = {**_SECRET, "name": "git-token", "id": "00000000-0000-0000-0000-000000000002"}
    api = FakeApi()
    api.on("GET", _ENV_PATH, status=200, json=_dev("seaweedfs-s3"))
    api.on("GET", _SECRETS_PATH, status=200, json=[_SECRET_2, other])

    setup.run_setup(api.client(), access_key="ak", secret_key="sk", out=io.StringIO())

    assert [r.method for r in api.requests] == ["GET", "GET"]


def test_operator_bound_dev_keeps_a_format_1_secret_untouched() -> None:
    """A `dev` bound to another secret is the operator's: no rotate, not even a listing."""
    api = FakeApi()
    api.on("GET", _ENV_PATH, status=200, json=_dev("my-s3"))

    setup.run_setup(api.client(), access_key="ak", secret_key="sk", out=io.StringIO())

    assert [r.method for r in api.requests] == ["GET"]


def test_listing_without_a_format_field_reads_as_format_1_and_is_rotated() -> None:
    """A server that predates the field gets a harmless rotate (the SDK reports 1).

    The bundle's server and this step are one image, so it does not happen in a bundle.
    """
    old = {k: v for k, v in _SECRET.items() if k not in ("format_version", "key_id")}
    api = FakeApi()
    api.on("GET", _ENV_PATH, status=200, json=_dev("seaweedfs-s3"))
    api.on("GET", _SECRETS_PATH, status=200, json=[old])
    api.on("PATCH", _ROTATE_PATH, status=200, json=old)

    setup.run_setup(api.client(), access_key="ak", secret_key="sk", out=io.StringIO())

    assert [r.method for r in api.requests] == ["GET", "GET", "PATCH"]


def test_rerun_creates_nothing() -> None:
    """Every step skips what exists."""
    api = FakeApi()
    api.on("GET", _ENV_PATH, status=200, json=_dev("seaweedfs-s3"))
    api.on("GET", _SECRETS_PATH, status=200, json=[_SECRET_2])

    setup.run_setup(api.client(), access_key="ak", secret_key="sk", out=io.StringIO())

    assert [r.method for r in api.requests] == ["GET", "GET"]


def test_existing_dev_without_secret_gets_secret_and_binding() -> None:
    """Resume after an interrupted first run."""
    api = FakeApi()
    api.on("GET", _ENV_PATH, status=200, json=_dev(None))
    api.on("GET", _SECRETS_PATH, status=200, json=[])
    api.on("POST", _SECRETS_PATH, status=201, json=_SECRET)
    api.on("PATCH", _STORAGE_PATH, status=200, json=_dev("seaweedfs-s3"))

    setup.run_setup(api.client(), access_key="ak", secret_key="sk", out=io.StringIO())

    assert [(r.method, r.url.path) for r in api.requests][1:] == [
        ("GET", _SECRETS_PATH),
        ("POST", _SECRETS_PATH),
        ("PATCH", _STORAGE_PATH),
    ]


def test_dev_recreated_with_another_endpoint_gets_the_bundles_endpoint_with_the_secret() -> None:
    """Delete, recreate with other options, restart: one PATCH sets both.

    The secret is never attached on its own, so it cannot end up on the other endpoint.
    """
    api = FakeApi()
    api.on("GET", _ENV_PATH, status=200, json=_dev(None))
    api.on("GET", _SECRETS_PATH, status=200, json=[_SECRET_2])
    api.on("PATCH", _STORAGE_PATH, status=200, json=_dev("seaweedfs-s3"))
    out = io.StringIO()

    setup.run_setup(api.client(), access_key="ak", secret_key="sk", out=out)

    patches = [r for r in api.requests if r.method == "PATCH"]
    assert [json.loads(r.content) for r in patches] == [
        {"options": {"s3.endpoint": "http://seaweedfs:8333"}, "secret_ref": "seaweedfs-s3"}
    ]
    assert "the storage options of 'dev' to the bundle's" in out.getvalue()


def test_secret_exists_but_unbound_only_binds() -> None:
    """Fourth partial state: the secret exists but the bind step never ran.

    The gap a crash between `client.secrets.create` and
    `client.environments.update_storage` would leave: unlike
    `test_existing_dev_without_secret_gets_secret_and_binding` (secret
    missing), here `secrets.list` already returns it, so only the `PATCH`
    should follow -- no second `POST` to `secrets`.
    """
    api = FakeApi()
    api.on("GET", _ENV_PATH, status=200, json=_dev(None))
    api.on("GET", _SECRETS_PATH, status=200, json=[_SECRET_2])
    api.on("PATCH", _STORAGE_PATH, status=200, json=_dev("seaweedfs-s3"))

    setup.run_setup(api.client(), access_key="ak", secret_key="sk", out=io.StringIO())

    assert [(r.method, r.url.path) for r in api.requests] == [
        ("GET", _ENV_PATH),
        ("GET", _SECRETS_PATH),
        ("PATCH", _STORAGE_PATH),
    ]


def test_dev_with_a_different_storage_root_is_left_untouched() -> None:
    """A `dev` with a foreign storage root is the operator's, untouched.

    No secret is created and no bind is attempted -- only the initial `GET`
    is sent.
    """
    api = FakeApi()
    api.on("GET", _ENV_PATH, status=200, json=_dev(None, root="file:///data/lake/dev"))
    out = io.StringIO()

    setup.run_setup(api.client(), access_key="ak", secret_key="sk", out=out)

    assert [r.method for r in api.requests] == ["GET"]
    assert "operator-managed" in out.getvalue()
    assert "file:///data/lake/dev" in out.getvalue()


def test_dev_with_a_foreign_secret_ref_is_left_untouched() -> None:
    """A `dev` already bound to someone else's secret is left alone, not silently rebound.

    Only the initial `GET` is sent -- in particular, `secrets.list` is
    never called, so the pre-existing binding cannot be second-guessed.
    """
    api = FakeApi()
    api.on("GET", _ENV_PATH, status=200, json=_dev("someone-elses-secret"))
    out = io.StringIO()

    setup.run_setup(api.client(), access_key="ak", secret_key="sk", out=out)

    assert [r.method for r in api.requests] == ["GET"]
    assert "operator-managed" in out.getvalue()


def test_main_exits_non_zero_without_leaking_secrets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed call exits 1 with a one-line reason; no secret in the output.

    `attempts=1` and a no-op `sleep` keep this instant: the real point
    (nothing in the output leaks a secret) does not depend on exhausting
    the full 30-attempt settling retry against an address nothing listens
    on.
    """
    monkeypatch.setenv("LAKANA_SETUP_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("LAKANA_SETUP_TOKEN", "lakana_pat_k_SECRETTOKEN")
    monkeypatch.setenv("LAKANA_SETUP_S3_ACCESS_KEY", "ak")
    monkeypatch.setenv("LAKANA_SETUP_S3_SECRET_KEY", "SECRETS3")

    assert setup.main(attempts=1, sleep=lambda seconds: None) == 1

    captured = capsys.readouterr()
    assert "SECRETTOKEN" not in captured.out + captured.err
    assert "SECRETS3" not in captured.out + captured.err


def test_main_missing_variable_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    """Misconfiguration is a usage error naming the variable, not a traceback."""
    for key in (
        "LAKANA_SETUP_URL",
        "LAKANA_SETUP_TOKEN",
        "LAKANA_SETUP_S3_ACCESS_KEY",
        "LAKANA_SETUP_S3_SECRET_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    assert setup.main() == 2


def test_main_exits_0_when_the_bootstrap_token_is_revoked_401(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A revoked bootstrap credential is expected, not a failure.

    Once an operator has replaced the bootstrap admin and revoked its PAT
    (the documented `deploy-with-compose.md` flow), every later `docker compose up`
    still re-runs `setup` with that now-dead token. Treating 401 as fatal
    would break every restart after following the documented procedure.
    """
    monkeypatch.setenv("LAKANA_SETUP_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("LAKANA_SETUP_TOKEN", "lakana_pat_k1_supersecret")
    monkeypatch.setenv("LAKANA_SETUP_S3_ACCESS_KEY", "ak")
    monkeypatch.setenv("LAKANA_SETUP_S3_SECRET_KEY", "sk")
    api = FakeApi()
    api.on("GET", _ENV_PATH, status=401, json={"detail": "invalid token"})
    _patch_client(monkeypatch, api)

    assert setup.main() == 0

    out = capsys.readouterr().out
    assert (
        "bootstrap credential rejected (retired after revocation, or a re-run init over an "
        "old volume); skipping setup" in out
    )


def test_main_exits_0_when_the_bootstrap_token_lacks_permission_403(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """403 gets the same treatment as 401 -- the credential was rejected."""
    monkeypatch.setenv("LAKANA_SETUP_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("LAKANA_SETUP_TOKEN", "lakana_pat_k1_supersecret")
    monkeypatch.setenv("LAKANA_SETUP_S3_ACCESS_KEY", "ak")
    monkeypatch.setenv("LAKANA_SETUP_S3_SECRET_KEY", "sk")
    api = FakeApi()
    api.on("GET", _ENV_PATH, status=403, json={"detail": "forbidden"})
    _patch_client(monkeypatch, api)

    assert setup.main() == 0

    out = capsys.readouterr().out
    assert (
        "bootstrap credential rejected (retired after revocation, or a re-run init over an "
        "old volume); skipping setup" in out
    )


def test_main_other_errors_still_exit_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only 401/403 get the "rejected" treatment -- every other error still fails the container."""
    monkeypatch.setenv("LAKANA_SETUP_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("LAKANA_SETUP_TOKEN", "lakana_pat_k1_supersecret")
    monkeypatch.setenv("LAKANA_SETUP_S3_ACCESS_KEY", "ak")
    monkeypatch.setenv("LAKANA_SETUP_S3_SECRET_KEY", "sk")
    api = FakeApi()
    api.on("GET", _ENV_PATH, status=404, json={"detail": "not found"})
    api.on("POST", "/api/v1/environments", status=500, json={"detail": "boom"})
    _patch_client(monkeypatch, api)

    assert setup.main(attempts=1) == 1

    out = capsys.readouterr()
    assert "bootstrap credential rejected" not in out.out


def _patch_client(monkeypatch: pytest.MonkeyPatch, api: FakeApi) -> None:
    """Make `lakana.Client(...)` build over `api`'s transport instead of a real socket.

    Captures the real `lakana.Client` class before patching the module
    attribute `main` reads, and builds directly over
    `httpx.MockTransport` rather than through `FakeApi.client` — calling
    that helper here would recurse, since it constructs a `lakana.Client`
    itself.

    Parameters
    ----------
    monkeypatch
        Pytest's monkeypatch fixture.
    api
        The fake platform API to route requests to.
    """
    real_client = lakana.Client

    def build(url: str, token: str) -> lakana.Client:
        """Build a real `Client` wired to `api`'s `httpx.MockTransport`.

        Parameters
        ----------
        url
            The base URL `main` was configured with.
        token
            The bearer token `main` was configured with.

        Returns
        -------
        lakana.Client
            A client over `api`.
        """
        http = httpx.Client(transport=httpx.MockTransport(api.handle), base_url=url)
        return real_client(url=url, token=token, http_client=http)

    monkeypatch.setattr(lakana, "Client", build)


def _set_setup_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the four `LAKANA_SETUP_*` variables `main` reads.

    Parameters
    ----------
    monkeypatch
        Pytest's monkeypatch fixture.
    """
    monkeypatch.setenv("LAKANA_SETUP_URL", "http://lakana.test")
    monkeypatch.setenv("LAKANA_SETUP_TOKEN", "lakana_pat_k1_supersecret")
    monkeypatch.setenv("LAKANA_SETUP_S3_ACCESS_KEY", "ak")
    monkeypatch.setenv("LAKANA_SETUP_S3_SECRET_KEY", "sk")


def test_main_succeeds_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """`main` builds a real client and drives `run_setup` to a clean exit.

    Covers `main`'s success path (coverage ratchet): `lakana.Client` is
    patched to build over `FakeApi`'s transport instead of a real socket.
    """
    _set_setup_env(monkeypatch)
    api = FakeApi()
    api.on("GET", _ENV_PATH, status=200, json=_dev("seaweedfs-s3"))
    api.on("GET", _SECRETS_PATH, status=200, json=[_SECRET_2])
    _patch_client(monkeypatch, api)

    assert setup.main() == 0


def test_main_retries_transient_errors_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server still settling (`LakanaConnectionError`/`ServerError`) is retried, then succeeds."""
    _set_setup_env(monkeypatch)
    api = FakeApi()
    _patch_client(monkeypatch, api)
    calls = {"n": 0}

    def flaky_run_setup(
        client: lakana.Client, *, access_key: str, secret_key: str, out: object = None
    ) -> None:
        """Fail with alternating transient errors twice, then succeed.

        Parameters
        ----------
        client
            Unused; the fake never calls the API.
        access_key
            Unused.
        secret_key
            Unused.
        out
            Unused.

        Raises
        ------
        lakana.errors.LakanaConnectionError
            On the first call.
        lakana.errors.ServerError
            On the second call.
        """
        calls["n"] += 1
        if calls["n"] == 1:
            raise LakanaConnectionError("Could not reach http://lakana.test: ConnectError")
        if calls["n"] == 2:
            raise ServerError("boom", status=503)

    monkeypatch.setattr(setup, "run_setup", flaky_run_setup)
    sleeps: list[float] = []

    assert setup.main(sleep=sleeps.append, attempts=5) == 0
    assert calls["n"] == 3
    assert sleeps == [2.0, 2.0]


def test_main_exhausts_retries_and_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server that never settles within `attempts` exits 1, not hangs forever."""
    _set_setup_env(monkeypatch)
    api = FakeApi()
    _patch_client(monkeypatch, api)

    def always_fails(
        client: lakana.Client, *, access_key: str, secret_key: str, out: object = None
    ) -> None:
        """Raise a `ServerError`, unconditionally.

        Parameters
        ----------
        client
            Unused.
        access_key
            Unused.
        secret_key
            Unused.
        out
            Unused.

        Raises
        ------
        lakana.errors.ServerError
            Always.
        """
        raise ServerError("boom", status=503)

    monkeypatch.setattr(setup, "run_setup", always_fails)
    sleeps: list[float] = []

    assert setup.main(sleep=sleeps.append, attempts=3) == 1
    assert sleeps == [2.0, 2.0]


def test_run_with_retry_forwards_out() -> None:
    """`_run_with_retry` passes `out` through to `run_setup` on every attempt.

    Before this fix, `_run_with_retry` called `run_setup` without
    forwarding `out` at all, so a caller-supplied stream was silently
    ignored under retry.
    """
    api = FakeApi()
    api.on("GET", _ENV_PATH, status=404, json={"detail": "not found"})
    api.on("POST", "/api/v1/environments", status=201, json=_dev(None))
    api.on("GET", _SECRETS_PATH, status=200, json=[])
    api.on("POST", _SECRETS_PATH, status=201, json=_SECRET)
    api.on("PATCH", _STORAGE_PATH, status=200, json=_dev("seaweedfs-s3"))
    out = io.StringIO()

    setup._run_with_retry(
        api.client(),
        access_key="ak",
        secret_key="sk",
        out=out,
        sleep=lambda seconds: None,
        attempts=1,
    )

    assert "Created environment 'dev'" in out.getvalue()
    assert "Created storage secret 'seaweedfs-s3'" in out.getvalue()
    assert "Bound 'seaweedfs-s3' to 'dev'" in out.getvalue()


def test_main_writes_progress_to_the_live_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`out`'s default resolves at call time, so `main`'s progress reaches a redirected stdout.

    A default bound once at import time (`out: TextIO =
    sys.stdout`) would capture the pre-test `sys.stdout` object, so writes
    would never show up in a later test's `capsys` capture. Binding lazily
    inside the function body reads whatever `sys.stdout` currently is.
    """
    _set_setup_env(monkeypatch)
    api = FakeApi()
    api.on("GET", _ENV_PATH, status=404, json={"detail": "not found"})
    api.on("POST", "/api/v1/environments", status=201, json=_dev(None))
    api.on("GET", _SECRETS_PATH, status=200, json=[])
    api.on("POST", _SECRETS_PATH, status=201, json=_SECRET)
    api.on("PATCH", _STORAGE_PATH, status=200, json=_dev("seaweedfs-s3"))
    _patch_client(monkeypatch, api)

    assert setup.main() == 0

    assert "Created environment 'dev'" in capsys.readouterr().out
