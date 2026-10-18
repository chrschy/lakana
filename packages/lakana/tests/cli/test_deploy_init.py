"""Tests for `lakana bundle init` (renamed in M5b)."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml
from conftest import FakeApi, invoke, make_state

import lakana
from lakana.cli import _profiles
from lakana.cli.deploy import _init
from lakana.errors import ConfigError

_SAFE = re.compile(r"[A-Za-z0-9_\-+/=.:]+")


def _env(path: Path) -> dict[str, str]:
    """Parse the generated `.env` (KEY='value' per line).

    Parameters
    ----------
    path
        The `.env` file.

    Returns
    -------
    dict
        The key/value pairs, quotes stripped.
    """
    out = {}
    for line in path.read_text().splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            out[key] = value.strip("'")
    return out


@pytest.fixture
def released(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the CLI is a release build so the default image tag is valid."""
    monkeypatch.setattr(_init, "_cli_version", lambda: "0.4.0")


def test_init_writes_compose_and_env_and_a_team_profile(tmp_path: Path, released: None) -> None:
    """A fresh directory gets both files; the profile points at the bundle."""
    state = make_state(tmp_path, FakeApi())
    target = tmp_path / "bundle"
    result = invoke(state, "bundle", "init", str(target))
    assert result.exit_code == 0, result.stderr
    compose = yaml.safe_load((target / "compose.yaml").read_text())
    assert set(compose["services"]) == {
        "postgres",
        "seaweedfs",
        "seaweedfs-core",
        "bucket",
        "migrate",
        "server",
        "worker",
        "worker-interactive",
        "worker-pipeline",
        "setup",
    }
    assert "ports" not in compose["services"]["postgres"]
    assert "ports" not in compose["services"]["seaweedfs"]
    assert "ports" not in compose["services"]["seaweedfs-core"]
    env = _env(target / ".env")
    assert stat.S_IMODE((target / ".env").stat().st_mode) == 0o600
    assert env["LAKANA_IMAGE"] == "ghcr.io/lakana-labs/lakana:0.4.0"
    assert (
        env["LAKANA_BIND"] == "127.0.0.1"
        and env["LAKANA_PORT"] == "8000"
        and env["LAKANA_WORKERS"] == "2"
    )
    profile = _profiles.load(state.profile_path())["profiles"]["team"]
    assert profile["url"] == "http://127.0.0.1:8000"
    assert (
        profile["token"]
        == f"lakana_pat_{env['LAKANA_BOOTSTRAP_PAT_KEY_ID']}_{env['LAKANA_BOOTSTRAP_PAT_SECRET']}"
    )
    assert profile["default_environment"] == "dev"


def test_every_generated_value_is_interpolation_safe(tmp_path: Path, released: None) -> None:
    """No `$`, `#`, quotes or whitespace can reach Compose."""
    state = make_state(tmp_path, FakeApi())
    invoke(state, "bundle", "init", str(tmp_path / "b"))
    for key, value in _env(tmp_path / "b" / ".env").items():
        assert _SAFE.fullmatch(value), key


def test_secrets_differ_between_runs_and_are_never_printed(tmp_path: Path, released: None) -> None:
    """Two bundles share no secret, and no secret reaches the terminal."""
    a = make_state(tmp_path / "a", FakeApi())
    b = make_state(tmp_path / "b", FakeApi())
    ra = invoke(a, "bundle", "init", str(tmp_path / "a" / "x"))
    rb = invoke(b, "bundle", "init", str(tmp_path / "b" / "x"))
    ea, eb = _env(tmp_path / "a" / "x" / ".env"), _env(tmp_path / "b" / "x" / ".env")
    for key in (
        "POSTGRES_PASSWORD",
        "LAKANA_S3_ACCESS_KEY",
        "LAKANA_S3_SECRET_KEY",
        "LAKANA_SECRETS__MASTER_KEY",
        "LAKANA_BOOTSTRAP_PAT_SECRET",
    ):
        assert ea[key] != eb[key]
        assert ea[key] not in ra.stdout + ra.stderr
        assert eb[key] not in rb.stdout + rb.stderr


def test_rerun_into_an_existing_bundle_refuses_and_changes_nothing(
    tmp_path: Path, released: None
) -> None:
    """Rotating the master key would lock out every stored secret."""
    state = make_state(tmp_path, FakeApi())
    target = tmp_path / "b"
    invoke(state, "bundle", "init", str(target))
    before = (
        (target / ".env").read_bytes(),
        (target / "compose.yaml").read_bytes(),
        state.profile_path().read_bytes(),
    )
    result = invoke(state, "--profile", "other", "bundle", "init", str(target))
    assert result.exit_code == 2
    assert (
        (target / ".env").read_bytes(),
        (target / "compose.yaml").read_bytes(),
        state.profile_path().read_bytes(),
    ) == before


def test_an_existing_profile_of_that_name_is_refused(tmp_path: Path, released: None) -> None:
    """Never overwrite a saved profile; suggest another --profile."""
    state = make_state(tmp_path, FakeApi())
    _profiles.set_profile(
        state.profile_path(), "team", url="https://x", token="t", default_environment=None
    )
    result = invoke(state, "bundle", "init", str(tmp_path / "b"))
    assert result.exit_code == 2
    assert "--profile" in result.stderr
    assert not (tmp_path / "b" / ".env").exists()


def test_team_profile_becomes_default_only_if_none_exists(tmp_path: Path, released: None) -> None:
    """An existing default profile is kept."""
    state = make_state(tmp_path, FakeApi())
    _profiles.set_profile(
        state.profile_path(), "work", url="https://x", token="t", default_environment=None
    )
    invoke(state, "bundle", "init", str(tmp_path / "b"))
    assert _profiles.load(state.profile_path())["default_profile"] == "work"


def test_dev_build_without_image_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`+` is not a valid Docker tag character."""
    monkeypatch.setattr(_init, "_cli_version", lambda: "0.4.0.dev3+gabc")
    state = make_state(tmp_path, FakeApi())
    result = invoke(state, "bundle", "init", str(tmp_path / "b"))
    assert result.exit_code == 2
    assert "--image" in result.stderr
    assert not (tmp_path / "b").exists()


def test_dev_build_with_image_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CI points init at a locally built image."""
    monkeypatch.setattr(_init, "_cli_version", lambda: "0.4.0.dev3+gabc")
    state = make_state(tmp_path, FakeApi())
    result = invoke(state, "bundle", "init", str(tmp_path / "b"), "--image", "lakana:ci")
    assert result.exit_code == 0, result.stderr
    assert _env(tmp_path / "b" / ".env")["LAKANA_IMAGE"] == "lakana:ci"


@pytest.mark.parametrize("bind", ["0.0.0.0", "192.168.1.5"])
def test_non_loopback_bind_warns_about_plain_http(
    tmp_path: Path, released: None, bind: str
) -> None:
    """Opening the port is explicit and warned about."""
    state = make_state(tmp_path, FakeApi())
    result = invoke(state, "bundle", "init", str(tmp_path / "b"), "--bind", bind)
    assert result.exit_code == 0
    assert "plain HTTP" in result.stderr


def test_public_url_sets_the_profile_url(tmp_path: Path, released: None) -> None:
    """Clients use the operator's TLS URL; https is not warned about."""
    state = make_state(tmp_path, FakeApi())
    result = invoke(
        state, "bundle", "init", str(tmp_path / "b"), "--public-url", "https://lakana.example.com"
    )
    assert "plain HTTP" not in result.stderr
    assert (
        _profiles.load(state.profile_path())["profiles"]["team"]["url"]
        == "https://lakana.example.com"
    )


def test_non_loopback_http_public_url_warns_about_plain_http(
    tmp_path: Path, released: None
) -> None:
    """A plain-`http://` `--public-url` naming a real host is also warned about."""
    state = make_state(tmp_path, FakeApi())
    result = invoke(
        state, "bundle", "init", str(tmp_path / "b"), "--public-url", "http://lakana.example.com"
    )
    assert result.exit_code == 0
    assert "plain HTTP" in result.stderr


@pytest.mark.parametrize("url", ["ftp://x", "lakana.example.com", "http://"])
def test_invalid_public_url_is_refused(tmp_path: Path, released: None, url: str) -> None:
    """Only absolute http(s) URLs."""
    state = make_state(tmp_path, FakeApi())
    assert invoke(state, "bundle", "init", str(tmp_path / "b"), "--public-url", url).exit_code == 2


@pytest.mark.parametrize(
    ("flag", "value"), [("--workers", "0"), ("--port", "0"), ("--port", "70000")]
)
def test_out_of_range_numbers_are_refused(
    tmp_path: Path, released: None, flag: str, value: str
) -> None:
    """Workers >= 1; port 1..65535."""
    state = make_state(tmp_path, FakeApi())
    assert invoke(state, "bundle", "init", str(tmp_path / "b"), flag, value).exit_code == 2


def test_target_path_that_is_a_file_exits_3(tmp_path: Path, released: None) -> None:
    """Filesystem errors exit 3 and name the path."""
    (tmp_path / "f").write_text("x")
    state = make_state(tmp_path, FakeApi())
    result = invoke(state, "bundle", "init", str(tmp_path / "f"))
    assert result.exit_code == 3
    assert str(tmp_path / "f") in result.stderr


def test_compose_services_wire_dependencies_and_healthchecks(
    tmp_path: Path, released: None
) -> None:
    """Start order: postgres -> migrate -> server/worker; seaweedfs -> bucket; both -> setup.

    `worker` also waits on `setup` completing successfully:
    this is what makes `docker compose up --wait` actually block until
    `setup` exits, and fail if it exits non-zero, with no second command
    needed -- `docker compose wait setup` raced `setup`'s own fast exit on a
    restart (Race A: `up --wait` polls after `setup` already exited and
    reports it a failure; Race B: `wait setup` runs after `compose wait`
    can no longer find the now-gone container), so it's gone.
    """
    state = make_state(tmp_path, FakeApi())
    invoke(state, "bundle", "init", str(tmp_path / "b"))
    s = yaml.safe_load((tmp_path / "b" / "compose.yaml").read_text())["services"]
    assert s["migrate"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    for svc in ("server", "worker"):
        assert s[svc]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert s["setup"]["depends_on"]["server"]["condition"] == "service_healthy"
    assert s["setup"]["depends_on"]["bucket"]["condition"] == "service_completed_successfully"
    assert s["worker"]["depends_on"]["setup"]["condition"] == "service_completed_successfully"
    assert s["server"]["ports"] == ["${LAKANA_BIND}:${LAKANA_PORT}:8000"]
    assert s["worker"]["deploy"]["replicas"] == "${LAKANA_WORKERS}"
    assert "LAKANA_BOOTSTRAP_PAT_SECRET" in str(s["server"]["environment"])
    assert "LAKANA_BOOTSTRAP_PAT_SECRET" not in str(s["worker"].get("environment", {}))


def _networks(service: dict) -> set[str]:
    """Return the names of the networks a Compose service joins.

    Parameters
    ----------
    service
        One entry of the parsed `services:` mapping.

    Returns
    -------
    set[str]
        The service's networks; `default` for a service that names none.
    """
    return set(service.get("networks") or ["default"])


def test_seaweedfs_internal_apis_are_unreachable_from_the_worker(
    tmp_path: Path, released: None
) -> None:
    """The worker and server reach SeaweedFS only through the S3 gateway (#14).

    `weed server` (master 9333, volume 8080, filer) sits alone on an `internal` network that only
    SeaweedFS's own services share; `seaweedfs`, a separate `weed s3` container, is the only
    SeaweedFS service on the network the worker and server use, and listens on 8333 and its
    gRPC port 18333 only (the Iceberg and Lance ports are off).
    """
    state = make_state(tmp_path, FakeApi())
    invoke(state, "bundle", "init", str(tmp_path / "b"))
    compose = yaml.safe_load((tmp_path / "b" / "compose.yaml").read_text())
    services = compose["services"]
    isolated = "seaweed-internal"
    assert compose["networks"][isolated]["internal"] is True
    on_internal = {name for name, svc in services.items() if isolated in _networks(svc)}
    assert on_internal == {"seaweedfs-core", "seaweedfs", "bucket"}
    assert _networks(services["seaweedfs-core"]) == {isolated}
    assert _networks(services["bucket"]) == {isolated}
    for name in ("server", "worker", "worker-interactive", "worker-pipeline", "setup"):
        assert isolated not in _networks(services[name]), name
    # Only the gateway bridges the two, and it runs the S3 server alone, with its extra HTTP ports
    # off; 18333 (gRPC) is the one residual port.
    assert _networks(services["seaweedfs"]) == {isolated, "default"}
    gateway = services["seaweedfs"]["command"].split()
    assert gateway[0] == "s3" and "-port=8333" in gateway
    assert "-filer=seaweedfs-core:8888" in gateway
    assert "-port.iceberg=0" in gateway and "-port.lance=0" in gateway
    assert "-ip=seaweedfs" in gateway
    # `network_mode` would put a service on another's network without `networks:` showing it.
    assert all("network_mode" not in svc for svc in services.values())
    assert "expose" not in services["seaweedfs-core"]
    assert "server" not in gateway
    assert "-s3" not in services["seaweedfs-core"]["command"].split()
    # No worker or server setting names the internal host.
    for name in ("server", "worker", "setup"):
        assert "seaweedfs-core" not in str(services[name])
    assert "ports" not in services["seaweedfs"]


def test_healthcheck_disabled_on_non_http_services(tmp_path: Path, released: None) -> None:
    """The one-shots (migrate, setup) disable the image's inherited HEALTHCHECK.

    Neither serves `/healthz`, so the image's HEALTHCHECK would otherwise
    leave them "unhealthy" under `docker compose up --wait`. `server` keeps
    the image's own healthcheck (no override, or an explicit one).
    """
    state = make_state(tmp_path, FakeApi())
    invoke(state, "bundle", "init", str(tmp_path / "b"))
    s = yaml.safe_load((tmp_path / "b" / "compose.yaml").read_text())["services"]
    for svc in ("migrate", "setup"):
        assert s[svc]["healthcheck"]["disable"] is True
    server_healthcheck = s["server"].get("healthcheck")
    if server_healthcheck is not None:
        assert "/healthz" in str(server_healthcheck.get("test", ""))


def test_worker_has_a_process_healthcheck_not_a_disabled_one(
    tmp_path: Path, released: None
) -> None:
    """`up --wait` needs a healthcheck on every long-running service.

    Older Compose v2 releases (GitHub's runners) fail `up --wait` with "has no
    healthcheck configured" for a running service whose healthcheck is
    disabled; the worker serves no HTTP, so it checks its own process instead.
    """
    state = make_state(tmp_path, FakeApi())
    invoke(state, "bundle", "init", str(tmp_path / "b"))
    worker = yaml.safe_load((tmp_path / "b" / "compose.yaml").read_text())["services"]["worker"]
    assert "disable" not in worker["healthcheck"]
    assert "lakana-worker" in " ".join(worker["healthcheck"]["test"])


def test_cli_version_reads_the_installed_package_version() -> None:
    """The unpatched seam returns `lakana.__version__` (the `released` fixture replaces it)."""
    assert _init._cli_version() == lakana.__version__


@pytest.mark.parametrize(
    ("bind", "expected_url", "expect_warning"),
    [
        ("127.0.0.1", "http://127.0.0.1:8000", False),
        ("::1", "http://127.0.0.1:8000", False),
        ("localhost", "http://127.0.0.1:8000", False),
        ("0.0.0.0", "http://127.0.0.1:8000", True),
        ("::", "http://127.0.0.1:8000", True),
        ("192.168.1.5", "http://192.168.1.5:8000", True),
        ("fd00::5", "http://[fd00::5]:8000", True),
    ],
)
def test_default_profile_url_and_warning_follow_bind(
    tmp_path: Path, released: None, bind: str, expected_url: str, expect_warning: bool
) -> None:
    """Loopback/wildcard binds save 127.0.0.1; a specific address saves itself.

    Wildcards (`0.0.0.0`, `::`) still warn about plain HTTP, since they expose
    the port to the network even though the saved URL stays loopback.
    """
    state = make_state(tmp_path, FakeApi())
    result = invoke(state, "bundle", "init", str(tmp_path / "b"), "--bind", bind)
    assert result.exit_code == 0, result.stderr
    url = _profiles.load(state.profile_path())["profiles"]["team"]["url"]
    assert url == expected_url
    assert urlsplit(url).port == 8000
    assert ("plain HTTP" in result.stderr) == expect_warning


@pytest.mark.parametrize("bind", ["not-an-address", "256.1.1.1", "example.com"])
def test_invalid_bind_is_refused(tmp_path: Path, released: None, bind: str) -> None:
    """`--bind` must be an IP address or the literal `localhost`."""
    state = make_state(tmp_path, FakeApi())
    target = tmp_path / "b"
    result = invoke(state, "bundle", "init", str(target), "--bind", bind)
    assert result.exit_code == 2
    assert not target.exists()


@pytest.mark.parametrize(
    ("bind", "expected_lakana_bind"),
    [
        ("127.0.0.1", "127.0.0.1"),
        ("localhost", "127.0.0.1"),
        ("::", "[::]"),
        ("::1", "[::1]"),
        ("fd00::5", "[fd00::5]"),
    ],
)
def test_env_bind_is_bracketed_for_ipv6_so_compose_ports_syntax_parses(
    tmp_path: Path, released: None, bind: str, expected_lakana_bind: str
) -> None:
    """Fix round 2/3: Compose port syntax needs `[::1]`, not `::1`.

    `host_ip` must also be an IP literal, so `localhost` is written as
    `127.0.0.1` .
    """
    state = make_state(tmp_path, FakeApi())
    target = tmp_path / "b"
    result = invoke(state, "bundle", "init", str(target), "--bind", bind)
    assert result.exit_code == 0, result.stderr
    assert _env(target / ".env")["LAKANA_BIND"] == expected_lakana_bind


@pytest.mark.parametrize("image", ["ghcr.io/x image", "img;rm -rf", "a\nb", "im'g"])
def test_invalid_image_is_refused(tmp_path: Path, released: None, image: str) -> None:
    """`--image` is validated before anything is written."""
    state = make_state(tmp_path, FakeApi())
    target = tmp_path / "b"
    result = invoke(state, "bundle", "init", str(target), "--image", image)
    assert result.exit_code == 2
    assert not target.exists()


def test_concurrent_init_never_deletes_the_winners_env(
    tmp_path: Path, released: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A losing racer must not delete the winner's already-written `.env`.

    `check_bundle_absent` is patched to a no-op to simulate the race window
    between it and the actual `O_EXCL` open: another process already won
    and wrote `.env`, so this run's own attempt fails with `EEXIST`. Only
    the files *this* invocation created may be rolled back.
    """
    target = tmp_path / "b"
    target.mkdir()
    winner_content = "# winner\nPOSTGRES_PASSWORD='winner-secret'\n"
    (target / ".env").write_text(winner_content)
    state = make_state(tmp_path, FakeApi())

    with monkeypatch.context() as m:
        m.setattr(_init, "check_bundle_absent", lambda directory: None)
        result = invoke(state, "bundle", "init", str(target))

    assert result.exit_code == 2
    assert (target / ".env").read_text() == winner_content
    assert not (target / "compose.yaml").exists()


def test_concurrent_init_rolls_back_only_its_own_env_when_compose_loses_the_race(
    tmp_path: Path, released: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Symmetric race: this run's own `.env` write succeeds, but `compose.yaml` was won."""
    target = tmp_path / "b"
    target.mkdir()
    winner_compose = "# winner compose\n"
    (target / "compose.yaml").write_text(winner_compose)
    state = make_state(tmp_path, FakeApi())

    with monkeypatch.context() as m:
        m.setattr(_init, "check_bundle_absent", lambda directory: None)
        result = invoke(state, "bundle", "init", str(target))

    assert result.exit_code == 2
    assert (target / "compose.yaml").read_text() == winner_compose
    assert not (target / ".env").exists()


def test_bundle_exists_check_runs_before_the_profile_exists_check(
    tmp_path: Path, released: None
) -> None:
    """When both are already taken, the relevant refusal is about the bundle."""
    state = make_state(tmp_path, FakeApi())
    target = tmp_path / "b"
    invoke(state, "bundle", "init", str(target))
    result = invoke(state, "bundle", "init", str(target))
    assert result.exit_code == 2
    assert "already exists; refusing to overwrite a deployment" in result.stderr
    assert "--profile" not in result.stderr


def test_dangling_env_symlink_refuses_and_writes_nothing(tmp_path: Path, released: None) -> None:
    """A symlinked `.env` counts as 'already exists' (exit 2), even if dangling."""
    target = tmp_path / "b"
    target.mkdir()
    (target / ".env").symlink_to(target / "nonexistent-target")
    state = make_state(tmp_path, FakeApi())
    result = invoke(state, "bundle", "init", str(target))
    assert result.exit_code == 2
    assert not (target / "compose.yaml").exists()
    assert (target / ".env").is_symlink()


def test_dangling_compose_symlink_refuses_and_writes_nothing(
    tmp_path: Path, released: None
) -> None:
    """Same ruling for a symlinked `compose.yaml`."""
    target = tmp_path / "b"
    target.mkdir()
    (target / "compose.yaml").symlink_to(target / "nonexistent-target")
    state = make_state(tmp_path, FakeApi())
    result = invoke(state, "bundle", "init", str(target))
    assert result.exit_code == 2
    assert not (target / ".env").exists()
    assert (target / "compose.yaml").is_symlink()


def test_compose_write_failure_leaves_no_env_and_a_rerun_then_succeeds(
    tmp_path: Path, released: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure after `.env` exists must not leave it behind."""
    target = tmp_path / "b"
    state = make_state(tmp_path, FakeApi())
    real_open = os.open

    def fake_open(path: str | bytes | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        if str(path).endswith("compose.yaml"):
            raise OSError("disk full")
        return real_open(path, flags, mode)

    # Scoped to this block (not `monkeypatch` itself): undoing it must not also
    # undo the `released` fixture's patch, since both share one MonkeyPatch object.
    with monkeypatch.context() as m:
        m.setattr(_init.os, "open", fake_open)
        result = invoke(state, "bundle", "init", str(target))
    assert result.exit_code == 3
    assert not (target / ".env").exists()
    assert not (target / "compose.yaml").exists()

    result2 = invoke(state, "bundle", "init", str(target))
    assert result2.exit_code == 0, result2.stderr


def test_profile_save_failure_removes_the_whole_bundle_and_a_rerun_then_succeeds(
    tmp_path: Path, released: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure saving the profile must not leave a half bundle."""
    target = tmp_path / "b"
    state = make_state(tmp_path, FakeApi())

    def fail_set_profile(*args: object, **kwargs: object) -> None:
        raise ConfigError("disk full")

    with monkeypatch.context() as m:
        m.setattr(_profiles, "set_profile", fail_set_profile)
        result = invoke(state, "bundle", "init", str(target))
    assert result.exit_code == 3
    assert not (target / ".env").exists()
    assert not (target / "compose.yaml").exists()

    result2 = invoke(state, "bundle", "init", str(target))
    assert result2.exit_code == 0, result2.stderr


def test_next_steps_use_plain_up_wait_with_no_second_command(
    tmp_path: Path, released: None
) -> None:
    """`up -d --wait` alone is the whole story now.

    `docker compose wait setup` used to be appended here to close the gap
    where `--wait` reports a `healthcheck: {disable: true}` service healthy
    the moment it starts, not when it exits -- but on a restart, where
    `setup` sends two GETs and exits in under a second, that second command
    raced `setup`'s own exit two different ways (`compose_smoke_test.sh`'s
    history has the detail). `worker.depends_on.setup` in the compose
    template (see `test_compose_services_wire_dependencies_and_healthchecks`)
    now makes `--wait` itself block on `setup` and fail if it exits non-zero,
    so no second command is needed, and none should reappear here.
    """
    state = make_state(tmp_path, FakeApi())
    result = invoke(state, "bundle", "init", str(tmp_path / "b"))
    assert result.exit_code == 0, result.stderr
    assert "docker compose up -d --wait\n" in result.stdout
    assert "wait setup" not in result.stdout


def test_next_steps_do_not_ask_for_a_registry_login(tmp_path: Path, released: None) -> None:
    """The image is public, so the next steps name no `docker login`."""
    state = make_state(tmp_path, FakeApi())
    result = invoke(state, "bundle", "init", str(tmp_path / "b"))
    assert result.exit_code == 0, result.stderr
    assert "docker login" not in result.stdout
    # The bundle's own path may say "private" (macOS's /var is /private/var); the steps may not.
    steps = result.stdout.replace(str(tmp_path.resolve()), "").replace(str(tmp_path), "")
    assert "private" not in steps


_VAR_RE = re.compile(r"\$\{(\w+)(?::-[^}]*)?\}")
_COMPOSE_BUILTINS = frozenset({"COMPOSE_PROJECT_NAME"})


def test_every_compose_variable_has_a_matching_env_key(released: None) -> None:
    """A template typo in `${VAR}` would otherwise only be caught by the path-gated smoke test."""
    template = (Path(_init.__file__).parent / _init.TEMPLATE_NAME).read_text()
    referenced = set(_VAR_RE.findall(template)) - _COMPOSE_BUILTINS
    generated = set(_init.generate_env(image="x", bind="127.0.0.1", port=8000, workers=2).keys())
    assert referenced == generated


def test_bundle_mounts_repos_read_only_and_sets_repo_roots(tmp_path: Path, released: None) -> None:
    """Server and worker see ./repos at /repos, read-only, and only there."""
    state = make_state(tmp_path, FakeApi())
    invoke(state, "bundle", "init", str(tmp_path / "b"))
    compose = yaml.safe_load((tmp_path / "b" / "compose.yaml").read_text())
    assert compose["services"]["server"]["volumes"] == ["./repos:/repos:ro"]
    assert compose["services"]["worker"]["volumes"] == [
        "./repos:/repos:ro",
        "worker-cache:/home/lakana/.cache/lakana-worker",
    ]
    for svc in ("server", "worker"):
        assert compose["services"][svc]["environment"]["LAKANA_REPO_ROOTS"] == '["/repos"]'
    assert (tmp_path / "b" / "repos").is_dir()


def test_deploy_init_is_gone(tmp_path: Path, released: None) -> None:
    """The old bundle command path is not registered; nothing is generated."""
    state = make_state(tmp_path, FakeApi())
    result = invoke(state, "deploy", "init", str(tmp_path / "b"))
    assert result.exit_code != 0
    assert not (tmp_path / "b" / "compose.yaml").exists()


def test_repos_directory_failure_leaves_no_bundle_and_a_rerun_then_succeeds(
    tmp_path: Path, released: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `repos/` that can't be created fails with exit 3 and removes the files written so far."""
    target = tmp_path / "b"
    state = make_state(tmp_path, FakeApi())
    real_mkdir = Path.mkdir

    def fake_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == "repos":
            raise OSError("read-only filesystem")
        real_mkdir(self, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    with monkeypatch.context() as m:
        m.setattr(Path, "mkdir", fake_mkdir)
        result = invoke(state, "bundle", "init", str(target))
    assert result.exit_code == 3
    assert not (target / ".env").exists()
    assert not (target / "compose.yaml").exists()

    result2 = invoke(state, "bundle", "init", str(target))
    assert result2.exit_code == 0, result2.stderr


def test_the_worker_gets_a_drain_grace_period(tmp_path: Path, released: None) -> None:
    """Compose waits 45 s: 20 s drain + 10 s kill grace + 2 s output drain, with margin."""
    invoke(make_state(tmp_path, FakeApi()), "bundle", "init", str(tmp_path / "b"))
    compose = yaml.safe_load((tmp_path / "b" / "compose.yaml").read_text())
    assert compose["services"]["worker"]["stop_grace_period"] == "45s"


def test_the_worker_can_run_python_assets(tmp_path: Path, released: None) -> None:
    """The worker knows the server's address and keeps its venvs across restarts."""
    invoke(make_state(tmp_path, FakeApi()), "bundle", "init", str(tmp_path / "b"))
    compose = yaml.safe_load((tmp_path / "b" / "compose.yaml").read_text())
    worker = compose["services"]["worker"]
    assert worker["environment"]["LAKANA_RUN__SERVER_URL"] == "http://server:8000"
    assert "LAKANA_RUN__SERVER_URL" not in compose["services"]["server"]["environment"]
    assert "worker-cache" in compose["volumes"]


def test_the_worker_container_has_a_memory_limit_from_one_value_with_a_default(
    tmp_path: Path, released: None
) -> None:
    """One `.env` value; the Compose file falls back to it for an older bundle.

    The worker derives its own limit (90 percent) from the container's, so nothing else is set.
    """
    state = make_state(tmp_path, FakeApi())
    target = tmp_path / "b"
    invoke(state, "bundle", "init", str(target))
    env = _env(target / ".env")
    assert env["LAKANA_WORKER_MEMORY"] == "4g"
    assert "LAKANA_WORKER_MEMORY_LIMIT" not in env
    raw = (target / ".env").read_text()
    assert raw.split("LAKANA_WORKER_MEMORY=")[0].splitlines()[-1].startswith("# Memory limit")
    worker = yaml.safe_load((target / "compose.yaml").read_text())["services"]["worker"]
    assert worker["mem_limit"] == "${LAKANA_WORKER_MEMORY:-4g}"
    assert worker["memswap_limit"] == worker["mem_limit"]
    assert "LAKANA_WORKER__MEMORY_LIMIT" not in worker["environment"]
    assert "LAKANA_WORKER_MEMORY_LIMIT" not in (target / "compose.yaml").read_text()
    assert worker["restart"] == "unless-stopped"


def _bundle_services(tmp_path: Path) -> tuple[dict, Path]:
    """Generate a bundle and return its parsed services and its directory.

    Parameters
    ----------
    tmp_path
        The test's directory.

    Returns
    -------
    tuple
        The `services` mapping of the parsed `compose.yaml` (merge keys resolved) and the
        bundle directory.
    """
    target = tmp_path / "b"
    invoke(make_state(tmp_path, FakeApi()), "bundle", "init", str(target))
    return yaml.safe_load((target / "compose.yaml").read_text())["services"], target


def test_the_bundle_has_a_worker_service_per_kind_with_zero_replicas_by_default(
    tmp_path: Path, released: None
) -> None:
    """The same definition as `worker`, a kind of work, and a count from `.env`."""
    s, _ = _bundle_services(tmp_path)
    worker = s["worker"]
    assert "LAKANA_WORKER__TAKES" not in worker["environment"]
    for service, takes, count in (
        ("worker-interactive", "interactive", "LAKANA_WORKERS_INTERACTIVE"),
        ("worker-pipeline", "pipeline", "LAKANA_WORKERS_PIPELINE"),
    ):
        extra = s[service]
        assert {k: v for k, v in extra.items() if k not in ("environment", "deploy")} == {
            k: v for k, v in worker.items() if k not in ("environment", "deploy")
        }
        assert extra["environment"] == {**worker["environment"], "LAKANA_WORKER__TAKES": takes}
        assert extra["deploy"]["replicas"] == f"${{{count}:-0}}"
        # The memory limit is the worker's, and the same single `.env` value.
        assert extra["mem_limit"] == "${LAKANA_WORKER_MEMORY:-4g}"
        assert extra["memswap_limit"] == extra["mem_limit"]
        assert extra["restart"] == "unless-stopped"
        assert extra["depends_on"] == worker["depends_on"]


def test_the_default_env_starts_the_workers_it_started_before_this_release(
    tmp_path: Path, released: None
) -> None:
    """A default installation changes nothing; the extra services have no replica."""
    s, target = _bundle_services(tmp_path)
    env = _env(target / ".env")

    def replicas(service: str) -> int:
        """Resolve a service's replica count the way Compose does, from `.env`.

        Parameters
        ----------
        service
            The service name.

        Returns
        -------
        int
            The count Compose would start.
        """
        match = _VAR_RE_FULL.fullmatch(s[service]["deploy"]["replicas"])
        assert match
        name, _, default = match.groups()
        return int(env.get(name, default))

    assert replicas("worker") == 2
    assert replicas("worker-interactive") == 0
    assert replicas("worker-pipeline") == 0
    assert env["LAKANA_WORKERS_INTERACTIVE"] == "0" and env["LAKANA_WORKERS_PIPELINE"] == "0"
    # An .env that predates both keys starts none either.
    del env["LAKANA_WORKERS_INTERACTIVE"], env["LAKANA_WORKERS_PIPELINE"]
    assert replicas("worker-interactive") == 0 and replicas("worker-pipeline") == 0


_VAR_RE_FULL = re.compile(r"\$\{(\w+)(:-(\d+))?\}")


def test_the_env_file_has_both_counts_at_zero_with_a_comment(
    tmp_path: Path, released: None
) -> None:
    """Each count follows `LAKANA_WORKERS` with a comment; memory names all three."""
    _, target = _bundle_services(tmp_path)
    raw = (target / ".env").read_text()
    keys = [line.partition("=")[0] for line in raw.splitlines() if line and line[0] != "#"]
    at = keys.index("LAKANA_WORKERS")
    assert keys[at : at + 4] == [
        "LAKANA_WORKERS",
        "LAKANA_WORKERS_INTERACTIVE",
        "LAKANA_WORKERS_PIPELINE",
        "LAKANA_WORKER_MEMORY",
    ]
    lines = raw.splitlines()

    def comment_before(key: str) -> str:
        """Return the comment line above a key.

        Parameters
        ----------
        key
            The `.env` key.

        Returns
        -------
        str
            The line above `key=`.
        """
        return lines[next(i for i, line in enumerate(lines) if line.startswith(f"{key}=")) - 1]

    assert comment_before("LAKANA_WORKERS_INTERACTIVE") == (
        "# Workers that take ad-hoc queries only (0: none). "
        "Keep a worker that takes pipeline work when you set this."
    )
    assert comment_before("LAKANA_WORKERS_PIPELINE") == (
        "# Workers that take pipeline work only: deploys, tasks, maintenance (0: none)."
    )
    memory = comment_before("LAKANA_WORKER_MEMORY")
    assert "LAKANA_WORKERS + LAKANA_WORKERS_INTERACTIVE + LAKANA_WORKERS_PIPELINE" in memory


def test_neither_new_service_gets_the_bootstrap_secret(tmp_path: Path, released: None) -> None:
    """The extra workers read the shared block only, as `worker` does."""
    s, _ = _bundle_services(tmp_path)
    for service in ("worker-interactive", "worker-pipeline"):
        assert "LAKANA_BOOTSTRAP_PAT_SECRET" not in str(s[service]["environment"])
