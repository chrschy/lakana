"""The exit test: projects and deploys on Postgres and SeaweedFS.

Drives the platform through `lakana.Client` over the app's own `TestClient`;
queued deploy runs are executed in-process by `poll_once` with deploy
settings, as a `lakana-worker` process would. Repositories are local (git on
disk); HTTPS and SSH fetching are covered by `lakana-worker`'s own tests.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import os
import subprocess
import tarfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, func, select
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

import lakana
from lakana._models import Deploy
from lakana.errors import Conflict, DeployFailed, PermissionDenied, ValidationError
from lakana_core import errors as lakana_core_errors
from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.config import DatabaseConfig
from lakana_core.connections.kinds import ConnectionKind
from lakana_core.connections.service import ConnectionService
from lakana_core.db import create_engine
from lakana_core.errors import ConflictError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.models import (
    Base,
    Connection,
    DeployRun,
    DeployStatus,
    Environment,
    JobSchedule,
    JobVersion,
    Principal,
    Repository,
)
from lakana_core.projects.deploys import CONFLICT_MESSAGE, DeployService
from lakana_core.projects.diff import DeployDiff
from lakana_core.projects.issues import ProjectValidationError
from lakana_core.projects.limits import DeployLimits
from lakana_core.secrets.keys import EnvKeyProvider
from lakana_server.app import create_app
from lakana_server.settings import Settings
from lakana_worker.deploy import DeploySettings, code_path
from lakana_worker.poller import poll_once
from tests.server_test_helpers import SeaweedFsContainer

pytestmark = pytest.mark.integration

_TOKEN = "lakana_pat_m5bintegration_m5bintegrationsecret"
_IDENTITY = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


@dataclass(frozen=True)
class _Stack:
    """Running services and the settings pointing at them.

    Parameters
    ----------
    settings
        Server settings (with `repo_roots`).
    storage
        The S3 storage block for `environments.create`.
    roots
        The repo root directory.
    seaweedfs
        The SeaweedFS container, for reading uploaded tarballs.
    """

    settings: Settings
    storage: dict[str, object]
    roots: Path
    seaweedfs: SeaweedFsContainer


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Stack]:
    """Start Postgres and SeaweedFS once for this module.

    Parameters
    ----------
    tmp_path_factory
        For the repo root.

    Yields
    ------
    _Stack
        The services.
    """
    roots = tmp_path_factory.mktemp("repos")
    with (
        PostgresContainer("postgres:16-alpine", driver="pg8000") as pg,
        SeaweedFsContainer() as seaweedfs,
    ):
        seaweedfs.get_client().make_bucket("lakana")
        settings = Settings(
            database=DatabaseConfig(url=pg.get_connection_url()),
            bootstrap_pat_key_id="m5bintegration",
            bootstrap_pat_secret="m5bintegrationsecret",
            repo_roots=[str(roots)],
        )
        engine = create_engine(settings.database)
        Base.metadata.create_all(engine)
        engine.dispose()
        config = seaweedfs.get_config()
        yield _Stack(
            settings=settings,
            storage={
                "scheme": "s3",
                "root": "s3://lakana/{env}",
                "options": {
                    "s3.endpoint": f"http://{config['endpoint']}",
                    "s3.access-key-id": seaweedfs.access_key,
                    "s3.secret-access-key": seaweedfs.secret_key,
                },
            },
            roots=roots,
            seaweedfs=seaweedfs,
        )


def _git(cwd: Path, *args: str) -> str:
    """Run git with a fixed identity.

    Parameters
    ----------
    cwd
        Working directory.
    *args
        Arguments after ``git``.

    Returns
    -------
    str
        Stripped stdout.
    """
    return subprocess.run(
        ["git", "-c", "init.defaultBranch=main", *args],
        cwd=cwd,
        env={**os.environ, **_IDENTITY},
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _project(path: Path, files: dict[str, str]) -> str:
    """Write and commit files, creating the repository if needed.

    Parameters
    ----------
    path
        Repository directory.
    files
        Relative path → content; `None`-free.

    Returns
    -------
    str
        The new commit SHA.
    """
    if not (path / ".git").exists():
        path.mkdir(parents=True, exist_ok=True)
        _git(path, "init", "-q")
    for relative, text in files.items():
        (path / relative).parent.mkdir(parents=True, exist_ok=True)
        (path / relative).write_text(text)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "--allow-empty", "-m", "change")
    return _git(path, "rev-parse", "HEAD")


def _work(client: TestClient, settings: DeploySettings | None = None) -> None:
    """Drain the queue in-process, as the worker would.

    Parameters
    ----------
    client
        The test client, for its engine.
    settings
        Deploy settings; defaults to the app's repo roots.
    """
    settings = settings or DeploySettings(repo_roots=tuple(client.app.state.settings.repo_roots))
    while True:
        with Session(client.app.state.engine) as session:
            if not poll_once(session, GrantAuthorization(session), deploy=settings):
                return


def _env(sdk: lakana.Client, stack: _Stack, name: str) -> None:
    """Create an S3-backed environment with `bronze.events`.

    Parameters
    ----------
    sdk
        The admin client.
    stack
        The services.
    name
        Environment name.
    """
    storage = {**stack.storage, "root": f"s3://lakana/{name}"}
    sdk.environments.create(name, storage=storage)
    sdk.tables.create(name, "bronze.events", schema=pa.schema([pa.field("id", pa.int64())]))


@pytest.fixture
def sdk(stack: _Stack) -> Iterator[tuple[TestClient, lakana.Client]]:
    """Start an app on the shared stack and an admin SDK client.

    Parameters
    ----------
    stack
        The services.

    Yields
    ------
    tuple
        The test client and the SDK client.
    """
    with (
        TestClient(create_app(stack.settings)) as client,
        lakana.Client(url="http://testserver", token=_TOKEN, http_client=client) as admin,
    ):
        yield client, admin


def _name() -> str:
    """Return a unique, dot-free name for this test's environment or repository.

    Returns
    -------
    str
        e.g. ``t3f2a…``.
    """
    return f"t{uuid.uuid4().hex[:10]}"


def test_local_pipeline_end_to_end(stack: _Stack, sdk: tuple[TestClient, lakana.Client]) -> None:
    """Deploy, dry-run diff, redeploy reuse, rollback both ways, orphans, the tarball in S3."""
    client, admin = sdk
    env, repo = _name(), _name()
    _env(admin, stack, env)
    admin.tables.create(env, "silver.old", schema=pa.schema([pa.field("id", pa.int64())]))
    path = stack.roots / repo
    first = _project(
        path,
        {
            "lakana.yaml": f"repo: {repo}\n",
            "assets/silver/old.sql": "SELECT id FROM bronze.events\n",
            "assets/silver/kept.sql": "SELECT id FROM bronze.events\n",
        },
    )
    admin.repositories.create(repo, "local", str(path))
    dry = admin.deploys.create(env, repo, kind="dry_run")
    _work(client)
    diff = admin.deploys.wait(env, dry.id, timeout=10).diff
    assert (
        diff is not None and diff["adopted"] == ["silver.old"] and diff["added"] == ["silver.kept"]
    )
    assert admin.deployments.list(env) == []

    done = admin.deploys.create(env, repo)
    _work(client)
    one = admin.deploys.wait(env, done.id, timeout=10)
    version = admin.project_versions.get(env, one.project_version_id or "")
    key = f"{env}/{code_path(repo, first, version.code_sha256)}"
    blob = stack.seaweedfs.get_client().get_object("lakana", key).read()
    assert hashlib.sha256(blob).hexdigest() == version.code_sha256
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(blob))) as archive:
        names = archive.getnames()
    assert "assets/silver/kept.sql" in names and not any(n.startswith(".git") for n in names)

    (path / "assets" / "silver" / "old.sql").unlink()
    second = _project(path, {})
    admin.deploys.create(env, repo)
    _work(client)
    assert [o.table for o in admin.orphans.list(env).orphans] == ["silver.old"]
    again = admin.deploys.create(env, repo)
    _work(client)
    rerun = admin.deploys.wait(env, again.id, timeout=10)
    assert rerun.resolved_sha == second
    assert all(not tables for tables in (rerun.diff or {}).values())

    back = admin.deploys.create(env, repo, kind="rollback", to_previous=True)
    _work(client)
    assert admin.deploys.wait(env, back.id, timeout=10).resolved_sha == first
    forward = admin.deploys.create(env, repo, kind="rollback", to_sha=second)
    _work(client)
    assert admin.deploys.wait(env, forward.id, timeout=10).resolved_sha == second
    assert admin.deployments.list(env)[0].commit_sha == second


def test_authorization_exit_criteria(stack: _Stack, sdk: tuple[TestClient, lakana.Client]) -> None:
    """Cover the authorization exit criteria.

    No DEPLOY gives 403; a grant revoked between queue and claim fails the run; an unreadable
    input reads as not found; a manifest hides SQL from a reader without access.
    """
    client, admin = sdk
    env, repo = _name(), _name()
    _env(admin, stack, env)
    path = stack.roots / repo
    _project(
        path,
        {"lakana.yaml": f"repo: {repo}\n", "assets/silver/a.sql": "SELECT id FROM bronze.events\n"},
    )
    admin.repositories.create(repo, "local", str(path))
    ci_name = _name()
    admin.principals.create(ci_name, "SERVICE_ACCOUNT")
    token = admin.tokens.issue(ci_name, "ci").token
    with lakana.Client(url="http://testserver", token=token, http_client=client) as ci:
        with pytest.raises(PermissionDenied):
            ci.deploys.create(env, repo)
        grant = admin.grants.grant(
            principal=ci_name, action="DEPLOY", kind="ENVIRONMENT", environment=env
        )
        admin.grants.grant(principal=ci_name, role="VIEWER", kind="ENVIRONMENT", environment=env)
        queued = ci.deploys.create(env, repo)
        admin.grants.revoke(grant.id)
        _work(client)
        failed = admin.deploys.get(env, queued.id)
        assert failed.status == "failed" and (failed.error_message or "").startswith(
            "permission denied"
        )

    blind_name = _name()
    admin.principals.create(blind_name, "SERVICE_ACCOUNT")
    blind_token = admin.tokens.issue(blind_name, "ci").token
    admin.grants.grant(principal=blind_name, action="DEPLOY", kind="ENVIRONMENT", environment=env)
    admin.grants.grant(
        principal=blind_name, action="READ_ENVIRONMENT", kind="ENVIRONMENT", environment=env
    )
    with lakana.Client(url="http://testserver", token=blind_token, http_client=client) as blind:
        queued = blind.deploys.create(env, repo)
        _work(client)
        with pytest.raises(DeployFailed, match=r"table 'bronze\.events' not found"):
            blind.deploys.wait(env, queued.id, timeout=10)
        done = admin.deploys.create(env, repo)
        _work(client)
        version_id = admin.deploys.wait(env, done.id, timeout=10).project_version_id or ""
        asset = blind.project_versions.get(env, version_id).manifest["assets"][0]
        assert "sql" not in asset and asset["hidden_references"] == 1
        assert "sql" in admin.project_versions.get(env, version_id).manifest["assets"][0]


def _race_records(
    engine: Engine, principal: PrincipalRef, run_ids: list[str], tag: str
) -> list[str]:
    """Record each run from the same observed pointer, at the same moment.

    Parameters
    ----------
    engine
        The shared engine.
    principal
        The deploying principal.
    run_ids
        Queued deploy runs of one environment and repository.
    tag
        Makes each run's fake commit SHA unique to this round.

    Returns
    -------
    list of str
        ``ok`` or ``conflict`` per run, sorted.
    """
    outcomes: list[str] = []
    barrier = threading.Barrier(len(run_ids))

    def record(run_id: str, sha: str) -> None:
        """Observe the pointer, wait for the other threads, then record.

        Parameters
        ----------
        run_id
            The deploy run to record.
        sha
            A fake commit SHA unique to this run.
        """
        with Session(engine) as session:
            service = DeployService(session, AllowAllAuthorization())
            run = session.get(DeployRun, uuid.UUID(run_id))
            assert run is not None
            run.status, run.worker_id = DeployStatus.RUNNING, "w1"  # claimed, as run_deploy does
            session.commit()
            run.resolved_sha = sha
            current = service.current_job_version(run.environment_id, run.repository_id)
            barrier.wait()
            try:
                service.record_deploy(
                    principal,
                    run,
                    manifest={"assets": []},
                    code_uri="u",
                    code_sha256="0" * 64,
                    diff=DeployDiff(),
                    expected_job_version_id=current.id if current else None,
                    worker_id="w1",
                )
                outcomes.append("ok")
            except ConflictError:
                outcomes.append("conflict")

    threads = [
        threading.Thread(target=record, args=(run_id, f"{tag}{index}".ljust(40, "a")))
        for index, run_id in enumerate(run_ids)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return sorted(outcomes)


def _discard_unfinished(engine: Engine, run_ids: list[str]) -> None:
    """Delete the raced runs that never finished, so a later test's worker does not claim them.

    Parameters
    ----------
    engine
        The shared engine.
    run_ids
        The runs a race queued.
    """
    with Session(engine) as session:
        session.execute(
            delete(DeployRun).where(
                DeployRun.id.in_([uuid.UUID(r) for r in run_ids]),
                DeployRun.status.in_([DeployStatus.QUEUED, DeployStatus.RUNNING]),
            )
        )
        session.commit()


def _job_version_count(engine: Engine, env: str, repo: str) -> int:
    """Count the project versions recorded for an environment and repository.

    Parameters
    ----------
    engine
        The shared engine.
    env
        Environment name.
    repo
        Repository name.

    Returns
    -------
    int
        The number of rows.
    """
    with Session(engine) as session:
        return (
            session.scalar(
                select(func.count())
                .select_from(JobVersion)
                .join(Environment, Environment.id == JobVersion.environment_id)
                .join(Repository, Repository.id == JobVersion.repository_id)
                .where(Environment.name == env, Repository.name == repo)
            )
            or 0
        )


def test_racing_deploys_one_conflict_on_postgres(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """Two records from the same pointer: one ConflictError, loser writes no project version."""
    client, admin = sdk
    env, repo = _name(), _name()
    _env(admin, stack, env)
    path = stack.roots / repo
    _project(path, {"lakana.yaml": "", "assets/silver/a.sql": "SELECT id FROM bronze.events\n"})
    admin.repositories.create(repo, "local", str(path))
    engine = client.app.state.engine
    with Session(engine) as session:
        row = session.query(Principal).filter(Principal.name == "admin").one()
        principal = PrincipalRef(id=row.id, name=row.name, type=row.type)
    for round_ in range(2):
        runs = [admin.deploys.create(env, repo).id for _ in range(2)]
        outcomes = _race_records(engine, principal, runs, str(round_))
        _discard_unfinished(engine, runs)
        assert outcomes == ["conflict", "ok"], round_
        assert _job_version_count(engine, env, repo) == round_ + 1, round_


def test_two_repositories_racing_for_one_new_table_have_one_winner(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """Both pass validation, then record at once: the advisory lock lets exactly one claim it."""
    client, admin = sdk
    env, repo_a, repo_b = _name(), _name(), _name()
    _env(admin, stack, env)
    for repo in (repo_a, repo_b):
        _project(stack.roots / repo, {"lakana.yaml": "", "assets/silver/x.sql": "SELECT 1 AS id\n"})
        admin.repositories.create(repo, "local", str(stack.roots / repo))
    runs = [admin.deploys.create(env, repo).id for repo in (repo_a, repo_b)]
    engine = client.app.state.engine
    with Session(engine) as session:
        row = session.query(Principal).filter(Principal.name == "admin").one()
        principal = PrincipalRef(id=row.id, name=row.name, type=row.type)
    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def record(run_id: str, sha: str) -> None:
        """Record one run's manifest, claiming `silver.shared`, once both threads are ready.

        Parameters
        ----------
        run_id
            The deploy run.
        sha
            A fake commit SHA.
        """
        with Session(engine) as session:
            service = DeployService(session, AllowAllAuthorization())
            run = session.get(DeployRun, uuid.UUID(run_id))
            assert run is not None
            run.status, run.worker_id = DeployStatus.RUNNING, "w1"  # claimed, as run_deploy does
            session.commit()
            run.resolved_sha = sha
            barrier.wait()
            try:
                service.record_deploy(
                    principal,
                    run,
                    manifest={"assets": [{"table": "silver.shared"}]},
                    code_uri="u",
                    code_sha256="0" * 64,
                    diff=DeployDiff(),
                    expected_job_version_id=None,
                    worker_id="w1",
                )
                outcomes.append("ok")
            except lakana_core_errors.ValidationError:
                outcomes.append("refused")

    threads = [
        threading.Thread(target=record, args=(run_id, f"{index}".ljust(40, "c")))
        for index, run_id in enumerate(runs)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    _discard_unfinished(engine, runs)
    assert sorted(outcomes) == ["ok", "refused"]
    assert _job_version_count(engine, env, repo_a) + _job_version_count(engine, env, repo_b) == 1


def test_concurrent_repository_create_has_one_winner(stack: _Stack) -> None:
    """Two creates of one name at the same moment: one 201, the other a Conflict."""
    name = _name()
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def create() -> None:
        """Register the repository from its own app and client, after the barrier."""
        with (
            TestClient(create_app(stack.settings)) as client,
            lakana.Client(url="http://testserver", token=_TOKEN, http_client=client) as admin,
        ):
            barrier.wait()
            try:
                admin.repositories.create(name, "https", "https://example.invalid/r.git")
                outcomes.append("created")
            except Conflict:
                outcomes.append("conflict")

    threads = [threading.Thread(target=create) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["conflict", "created"]


def test_rollback_to_previous_after_an_intervening_deploy_conflicts(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """A to_previous target is fixed when queued; a deploy in between makes it a conflict."""
    client, admin = sdk
    env, repo = _name(), _name()
    _env(admin, stack, env)
    path = stack.roots / repo
    for index in range(3):
        _project(path, {"assets/silver/a.sql": f"SELECT id + {index} AS id FROM bronze.events\n"})
        if index == 0:
            admin.repositories.create(repo, "local", str(path))
        run = admin.deploys.create(env, repo)
        _work(client)
        admin.deploys.wait(env, run.id, timeout=10)
    _project(path, {"assets/silver/a.sql": "SELECT id + 9 AS id FROM bronze.events\n"})
    intervening = admin.deploys.create(env, repo)
    back = admin.deploys.create(env, repo, kind="rollback", to_previous=True)
    _work(client)
    newest = admin.deploys.wait(env, intervening.id, timeout=10)
    with pytest.raises(DeployFailed, match=CONFLICT_MESSAGE):
        admin.deploys.wait(env, back.id, timeout=10)
    assert admin.deployments.list(env)[0].commit_sha == newest.resolved_sha


def test_ssh_repository_registers_through_the_server(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """Spec exit criterion: repo add registers local, https and ssh."""
    _, admin = sdk
    known_hosts = (
        "git.example.invalid ssh-ed25519 "
        "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
    )
    for kind, url, extra in [
        ("https", "https://git.example.invalid/team/repo.git", {}),
        ("ssh", "git@git.example.invalid:team/repo.git", {"ssh_known_hosts": known_hosts}),
    ]:
        name = _name()
        created = admin.repositories.create(name, kind, url, **extra)
        assert (created.kind, created.url) == (kind, url)
        assert admin.repositories.get(name).kind == kind
    _project(stack.roots / "reg", {})
    local = admin.repositories.create(_name(), "local", str(stack.roots / "reg"))
    assert local.kind == "local"


def test_cross_repository_inputs_and_ownership(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """B reads A's current output; B claiming A's table fails."""
    client, admin = sdk
    env, repo_a, repo_b = _name(), _name(), _name()
    _env(admin, stack, env)
    _project(stack.roots / repo_a, {"assets/silver/shared.sql": "SELECT id FROM bronze.events\n"})
    _project(stack.roots / repo_b, {"assets/gold/report.sql": "SELECT id FROM silver.shared\n"})
    admin.repositories.create(repo_a, "local", str(stack.roots / repo_a))
    admin.repositories.create(repo_b, "local", str(stack.roots / repo_b))
    a = admin.deploys.create(env, repo_a)
    _work(client)
    admin.deploys.wait(env, a.id, timeout=10)
    b = admin.deploys.create(env, repo_b)
    _work(client)
    assert admin.deploys.wait(env, b.id, timeout=10).status == "succeeded"
    _project(stack.roots / repo_b, {"assets/silver/shared.sql": "SELECT 1 AS id\n"})
    thief = admin.deploys.create(env, repo_b)
    _work(client)
    with pytest.raises(DeployFailed, match=f"is produced by repository '{repo_a}'"):
        admin.deploys.wait(env, thief.id, timeout=10)


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"lakana.yaml": "a: &a [1,2]\nb: [*a, *a]\n"}, "anchors and aliases"),
        (
            {"assets/silver/deep.sql": "SELECT " + "(" * 3000 + "1" + ")" * 3000 + " AS a\n"},
            "nested too deeply|does not parse",
        ),
    ],
)
def test_adversarial_content_fails_cleanly(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client], files: dict[str, str], expected: str
) -> None:
    """Billion laughs and deeply nested SQL end as failed runs, not crashes."""
    client, admin = sdk
    env, repo = _name(), _name()
    _env(admin, stack, env)
    _project(stack.roots / repo, {"assets/silver/a.sql": "SELECT id FROM bronze.events\n", **files})
    admin.repositories.create(repo, "local", str(stack.roots / repo))
    queued = admin.deploys.create(env, repo)
    _work(client)
    with pytest.raises(DeployFailed, match=expected):
        admin.deploys.wait(env, queued.id, timeout=10)


def test_adversarial_repository_tricks(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client], tmp_path: Path
) -> None:
    """Fail cleanly on a passwd symlink, an fsmonitor canary, a flag-like ref, a big tree."""
    client, admin = sdk
    env, repo = _name(), _name()
    _env(admin, stack, env)
    path = stack.roots / repo
    _project(path, {"assets/silver/a.sql": "SELECT id FROM bronze.events\n"})
    os.symlink("/etc/passwd", path / "assets" / "silver" / "passwd.sql")
    _project(path, {})
    canary = tmp_path / "canary"
    # Set after the test's own last git command in this repository: git status/add would run it.
    _git(path, "config", "core.fsmonitor", f"touch {canary}")
    admin.repositories.create(repo, "local", str(path))
    queued = admin.deploys.create(env, repo)
    _work(client)
    with pytest.raises(DeployFailed, match="symlinks are not allowed under assets/"):
        admin.deploys.wait(env, queued.id, timeout=10)
    assert not canary.exists()
    with pytest.raises(ValidationError):
        admin.deploys.create(env, repo, ref=f"--upload-pack=touch {canary}")
    assert not canary.exists()
    _git(path, "config", "--unset", "core.fsmonitor")
    (path / "assets" / "silver" / "passwd.sql").unlink()
    _project(path, {"big.txt": "x" * 100_000})
    queued = admin.deploys.create(env, repo)
    _work(
        client,
        DeploySettings(repo_roots=(str(stack.roots),), limits=DeployLimits(checkout_bytes=50_000)),
    )
    with pytest.raises(DeployFailed, match="larger than"):
        admin.deploys.wait(env, queued.id, timeout=10)


def test_a_deploy_keeps_the_job_schedules_in_step_on_postgres(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """Deploy, change, roll back and remove a job: rows follow, warnings reach the client."""
    client, admin = sdk
    env, repo = _name(), _name()
    _env(admin, stack, env)
    path = stack.roots / repo
    daily = "jobs:\n  daily:\n    select: ['silver.*']\n    schedule: {cron: '0 3 * * *'}\n"
    asset = {"assets/silver/a.sql": "SELECT id FROM bronze.events\n"}
    _project(path, {"lakana.yaml": f"repo: {repo}\n{daily}", **asset})
    admin.repositories.create(repo, "local", str(path))

    def deploy(*, rollback: bool = False) -> Deploy:
        run = (
            admin.deploys.create(env, repo, kind="rollback", to_previous=True)
            if rollback
            else admin.deploys.create(env, repo)
        )
        _work(client)
        return admin.deploys.wait(env, run.id, timeout=10)

    def rows() -> dict[str, object]:
        with Session(client.app.state.engine) as session:
            return {
                row.job: row.next_fire_at
                for row in session.scalars(
                    select(JobSchedule).join(Repository).where(Repository.name == repo)
                )
            }

    first = deploy()
    assert rows()["daily"] is not None
    assert any("no run principal is bound" in w for w in first.warnings)

    dry = admin.deploys.create(env, repo, kind="dry_run")
    _work(client)
    admin.deploys.wait(env, dry.id, timeout=10)
    assert set(rows()) == {"daily"}

    _project(path, {"lakana.yaml": f"repo: {repo}\n"})
    deploy()
    assert rows() == {}
    deploy(rollback=True)
    assert rows()["daily"] is not None


_CONNECTION_KEYS = EnvKeyProvider(base64.b64encode(b"6" * 32).decode())
_CUSTOMERS_PATH = "assets/bronze/customers.py"


def _customers_manifest(*connections: str) -> dict[str, object]:
    """Build a manifest whose one Python asset, `bronze.customers`, declares `connections`.

    Parameters
    ----------
    *connections
        The connection names.

    Returns
    -------
    dict
        The manifest.
    """
    return {
        "assets": [
            {
                "table": "bronze.customers",
                "kind": "python",
                "path": _CUSTOMERS_PATH,
                "connections": list(connections),
            }
        ]
    }


def _admin(engine: Engine) -> PrincipalRef:
    """Find the bootstrap administrator.

    Parameters
    ----------
    engine
        The shared engine.

    Returns
    -------
    PrincipalRef
        The administrator.
    """
    with Session(engine) as session:
        row = session.query(Principal).filter(Principal.name == "admin").one()
        return PrincipalRef(id=row.id, name=row.name, type=row.type)


def _claim(session: Session, run_id: str, sha: str | None = None) -> DeployRun:
    """Mark a queued run as running under worker `w1`, as `run_deploy` does.

    Parameters
    ----------
    session
        The session the run is recorded in.
    run_id
        The run.
    sha
        The resolved commit of a deploy; `None` for a rollback.

    Returns
    -------
    DeployRun
        The claimed run.
    """
    run = session.get(DeployRun, uuid.UUID(run_id))
    assert run is not None
    run.status, run.worker_id = DeployStatus.RUNNING, "w1"
    session.commit()
    if sha is not None:
        run.resolved_sha = sha
    return run


def _record(engine: Engine, principal: PrincipalRef, run_id: str, sha: str, manifest: dict) -> None:
    """Record a deploy run with `manifest`, without contention.

    Parameters
    ----------
    engine
        The shared engine.
    principal
        The deployer.
    run_id
        The queued run.
    sha
        A fake commit SHA.
    manifest
        The manifest.
    """
    with Session(engine) as session:
        service = DeployService(session, AllowAllAuthorization())
        run = _claim(session, run_id, sha)
        current = service.current_job_version(run.environment_id, run.repository_id)
        service.record_deploy(
            principal,
            run,
            manifest=manifest,
            code_uri="u",
            code_sha256="0" * 64,
            diff=DeployDiff(),
            expected_job_version_id=current.id if current else None,
            worker_id="w1",
        )


def _held_at_commit(session: Session, ready: threading.Event, release: threading.Event) -> None:
    """Make the session's next commit flush, signal `ready`, and wait for `release`.

    Parameters
    ----------
    session
        The session whose transaction is held open with its locks.
    ready
        Set once everything is flushed and the transaction holds its locks.
    release
        Waited for (10 s at most) before the real commit.
    """
    real = session.commit

    def held() -> None:
        """Flush, hold, then commit."""
        session.flush()
        ready.set()
        release.wait(10)
        real()

    vars(session)["commit"] = held  # an instance attribute shadows the method


def _wait_for_a_lock_wait(engine: Engine) -> None:
    """Wait (10 s at most) until a backend of this database waits on a lock.

    Parameters
    ----------
    engine
        The shared engine.
    """
    for _ in range(200):
        with Session(engine) as session:
            waiting = session.scalar(
                sql_text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' AND datname = current_database()"
                )
            )
        if waiting:
            return
        time.sleep(0.05)
    pytest.fail("no session ever waited on a lock")


@dataclass(frozen=True)
class _Held:
    """A step that holds its transaction open at the commit (`_held_at_commit`).

    Parameters
    ----------
    step
        The step.
    ready
        Set once the step holds its locks.
    release
        Lets the step commit.
    """

    step: Callable[[], None]
    ready: threading.Event
    release: threading.Event


def _race(first: _Held, second: Callable[[], None], engine: Engine) -> dict[str, object]:
    """Run `first` until it holds its commit, then `second` until it blocks, then let both go.

    Parameters
    ----------
    first
        The step that takes its locks first and holds them.
    second
        The step that must wait for them.
    engine
        The shared engine, to see the wait.

    Returns
    -------
    dict
        ``first`` and ``second``: ``ok`` or the exception each raised.
    """
    outcomes: dict[str, object] = {}

    def run(name: str, step: Callable[[], None]) -> None:
        """Run one step and keep its outcome.

        Parameters
        ----------
        name
            ``first`` or ``second``.
        step
            The step.
        """
        try:
            step()
            outcomes[name] = "ok"
        except Exception as exc:
            outcomes[name] = exc

    ready, release = first.ready, first.release
    holder = threading.Thread(target=run, args=("first", first.step))
    holder.start()
    assert ready.wait(10), "the first step never reached its commit"
    waiter = threading.Thread(target=run, args=("second", second))
    waiter.start()
    try:
        _wait_for_a_lock_wait(engine)
        assert waiter.is_alive()
    finally:
        release.set()
        holder.join(10)
        waiter.join(10)
    assert not holder.is_alive() and not waiter.is_alive()
    return outcomes


def _held_delete(engine: Engine, principal: PrincipalRef, env: str) -> _Held:
    """Build a step that deletes `crm-db` and holds its transaction at the commit.

    Parameters
    ----------
    engine
        The shared engine.
    principal
        The administrator.
    env
        The environment.

    Returns
    -------
    _Held
        The step and its events.
    """
    ready, release = threading.Event(), threading.Event()

    def delete_connection() -> None:
        """Delete the connection in a session of its own."""
        with Session(engine) as session:
            _held_at_commit(session, ready, release)
            ConnectionService(session, AllowAllAuthorization(), _CONNECTION_KEYS).delete(
                principal, env, "crm-db"
            )

    return _Held(delete_connection, ready, release)


def _connection_world(
    stack: _Stack, client: TestClient, admin: lakana.Client
) -> tuple[str, str, PrincipalRef]:
    """Create an environment with connection `crm-db` and a registered repository.

    Parameters
    ----------
    stack
        The services.
    client
        The test client, for its engine.
    admin
        The admin SDK client.

    Returns
    -------
    tuple
        The environment, the repository and the administrator.
    """
    env, repo = _name(), _name()
    _env(admin, stack, env)
    _project(stack.roots / repo, {"lakana.yaml": "", "assets/silver/a.sql": "SELECT 1 AS id\n"})
    admin.repositories.create(repo, "local", str(stack.roots / repo))
    principal = _admin(client.app.state.engine)
    with Session(client.app.state.engine) as session:
        ConnectionService(session, AllowAllAuthorization(), _CONNECTION_KEYS).create(
            principal,
            env,
            name="crm-db",
            kind=ConnectionKind.DIRECTORY,
            address={"root": "shared", "path": "crm"},
            settings={},
        )
    return env, repo, principal


@pytest.mark.parametrize("kind", ["deploy", "rollback"])
def test_a_deploy_after_a_concurrent_connection_delete_fails_naming_the_connection(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client], kind: str
) -> None:
    """The delete locks the row first; the record waits on `FOR SHARE`, then finds it gone."""
    client, admin = sdk
    engine = client.app.state.engine
    env, repo, principal = _connection_world(stack, client, admin)
    runs: list[str] = []
    if kind == "rollback":
        runs += [admin.deploys.create(env, repo).id, admin.deploys.create(env, repo).id]
        _record(engine, principal, runs[0], "1" * 40, _customers_manifest("crm-db"))
        _record(engine, principal, runs[1], "2" * 40, _customers_manifest())
        runs.append(admin.deploys.create(env, repo, kind="rollback", to_sha="1" * 40).id)
    else:
        runs.append(admin.deploys.create(env, repo).id)
    before = _job_version_count(engine, env, repo)

    def record() -> None:
        """Record the run whose version declares `crm-db`."""
        with Session(engine) as session:
            service = DeployService(session, AllowAllAuthorization())
            if kind == "rollback":
                service.execute_rollback(principal, _claim(session, runs[-1]), worker_id="w1")
                return
            service.record_deploy(
                principal,
                _claim(session, runs[-1], "3" * 40),
                manifest=_customers_manifest("crm-db"),
                code_uri="u",
                code_sha256="0" * 64,
                diff=DeployDiff(),
                expected_job_version_id=None,
                worker_id="w1",
            )

    try:
        outcomes = _race(_held_delete(engine, principal, env), record, engine)
    finally:
        _discard_unfinished(engine, runs)
    assert outcomes["first"] == "ok"
    refusal = outcomes["second"]
    assert isinstance(refusal, ProjectValidationError), refusal
    assert str(refusal) == (
        f"{_CUSTOMERS_PATH}: Asset `bronze.customers` declares connection `crm-db`, which "
        f"environment `{env}` does not have. Create it: `lakana connection create crm-db "
        f"--env {env} --kind <kind> …` (see `lakana connection create --help`)."
    )
    assert _job_version_count(engine, env, repo) == before
    current = admin.deployments.list(env)
    assert [d.commit_sha for d in current] == (["2" * 40] if kind == "rollback" else [])


def test_a_connection_delete_after_a_concurrent_deploy_is_refused_naming_the_asset(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """The record holds `FOR SHARE`; the delete waits, then sees the version and answers 409."""
    client, admin = sdk
    engine = client.app.state.engine
    env, repo, principal = _connection_world(stack, client, admin)
    run_id = admin.deploys.create(env, repo).id
    ready, release = threading.Event(), threading.Event()

    def record() -> None:
        """Record a version declaring `crm-db`, holding the transaction at its commit."""
        with Session(engine) as session:
            run = _claim(session, run_id, "4" * 40)
            _held_at_commit(session, ready, release)
            DeployService(session, AllowAllAuthorization()).record_deploy(
                principal,
                run,
                manifest=_customers_manifest("crm-db"),
                code_uri="u",
                code_sha256="0" * 64,
                diff=DeployDiff(),
                expected_job_version_id=None,
                worker_id="w1",
            )

    def delete_connection() -> None:
        """Delete the connection in a session of its own."""
        with Session(engine) as session:
            ConnectionService(session, AllowAllAuthorization(), _CONNECTION_KEYS).delete(
                principal, env, "crm-db"
            )

    try:
        outcomes = _race(_Held(record, ready, release), delete_connection, engine)
    finally:
        _discard_unfinished(engine, [run_id])
    assert outcomes["first"] == "ok"
    refusal = outcomes["second"]
    assert isinstance(refusal, ConflictError), refusal
    assert str(refusal) == (
        f"connection `crm-db` is used by `{repo}: bronze.customers` in `{env}`; "
        "deploy a version without it first."
    )
    with Session(engine) as session:
        names = session.scalars(
            select(Connection.name)
            .join(Environment, Environment.id == Connection.environment_id)
            .where(Environment.name == env)
        ).all()
    assert names == ["crm-db"]
    assert [d.commit_sha for d in admin.deployments.list(env)] == ["4" * 40]
