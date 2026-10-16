"""Model additions: repositories, deploy runs, project versions, pointer, actions, events."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.auth import Action
from lakana_core.grants.authorization import TASK_TOKEN_ACTIONS
from lakana_core.grants.roles import ROLE_ACTIONS, Role
from lakana_core.models import (
    AuditEventType,
    DeployKind,
    DeployRun,
    DeployStatus,
    Environment,
    EnvironmentDeployment,
    JobVersion,
    Principal,
    PrincipalType,
    Repository,
    RepositoryKind,
)


def _seed(session: Session) -> tuple[Environment, Repository, Principal]:
    """Insert an environment, a principal and a local repository.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    tuple of (Environment, Repository, Principal)
        The inserted rows.
    """
    principal = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="ci", is_active=True)
    environment = Environment(
        id=uuid.uuid4(), name="dev", storage_root="file:///tmp/dev", storage_options={}
    )
    repository = Repository(
        id=uuid.uuid4(),
        name="analytics",
        kind=RepositoryKind.LOCAL,
        url="/srv/repos/analytics",
        created_by=principal.id,
    )
    session.add_all([principal, environment, repository])
    session.commit()
    return environment, repository, principal


def _job_version(environment: Environment, repository: Repository, sha: str) -> JobVersion:
    """Build an unsaved project version.

    Parameters
    ----------
    environment
        The target environment.
    repository
        The source repository.
    sha
        The commit SHA.

    Returns
    -------
    Project version
        The row, not yet added to a session.
    """
    return JobVersion(
        id=uuid.uuid4(),
        repository_id=repository.id,
        environment_id=environment.id,
        commit_sha=sha,
        manifest_json={"assets": []},
        code_uri=f"file:///tmp/dev/_artifacts/code/analytics/{sha}.tar.gz",
        code_sha256="0" * 64,
        deployed_by=None,
    )


def test_enum_values_are_the_api_spelling() -> None:
    """The REST API spells kinds and statuses in lower case."""
    assert [k.value for k in RepositoryKind] == ["local", "https", "ssh"]
    assert [k.value for k in DeployKind] == ["deploy", "dry_run", "rollback"]
    assert [s.value for s in DeployStatus] == ["queued", "running", "succeeded", "failed"]


def test_repository_round_trips_with_defaults(session: Session) -> None:
    """A repository stores its nullable fields as NULL and stamps its timestamps."""
    _, repository, _ = _seed(session)
    session.expire_all()
    loaded = session.get(Repository, repository.id)
    assert loaded is not None
    assert loaded.kind is RepositoryKind.LOCAL
    assert loaded.credential_secret is None
    assert loaded.credential_username is None
    assert loaded.ssh_known_hosts is None
    assert loaded.default_ref is None
    assert loaded.created_at.tzinfo is UTC


def test_repository_name_is_unique(session: Session) -> None:
    """Two repositories cannot share a name."""
    _, repository, _ = _seed(session)
    session.add(
        Repository(
            id=uuid.uuid4(), name=repository.name, kind=RepositoryKind.HTTPS, url="https://h/r"
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_job_version_is_unique_per_repository_sha_and_environment(session: Session) -> None:
    """The same SHA cannot have two project versions for one environment and repository."""
    environment, repository, _ = _seed(session)
    session.add(_job_version(environment, repository, "a" * 40))
    session.commit()
    session.add(_job_version(environment, repository, "a" * 40))
    with pytest.raises(IntegrityError):
        session.commit()


def test_deploy_run_and_pointer_round_trip(session: Session) -> None:
    """A deploy run references its project version and the pointer references the same row."""
    environment, repository, principal = _seed(session)
    version = _job_version(environment, repository, "b" * 40)
    session.add(version)
    session.flush()
    run = DeployRun(
        id=uuid.uuid4(),
        environment_id=environment.id,
        repository_id=repository.id,
        principal_id=principal.id,
        kind=DeployKind.DEPLOY,
        requested_ref="main",
        resolved_sha="b" * 40,
        status=DeployStatus.SUCCEEDED,
        diff_json={"added": ["bronze.x"]},
        job_version_id=version.id,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    pointer = EnvironmentDeployment(
        environment_id=environment.id,
        repository_id=repository.id,
        current_job_version_id=version.id,
    )
    session.add_all([run, pointer])
    session.commit()
    session.expire_all()
    loaded = session.get(DeployRun, run.id)
    assert loaded is not None
    assert loaded.kind is DeployKind.DEPLOY
    assert loaded.status is DeployStatus.SUCCEEDED
    assert loaded.created_at.tzinfo is UTC
    loaded_pointer = session.get(EnvironmentDeployment, (environment.id, repository.id))
    assert loaded_pointer is not None
    assert loaded_pointer.current_job_version_id == version.id


def test_deleting_a_repository_cascades_its_history(session: Session) -> None:
    """Deploy runs, project versions and pointers go with their repository."""
    environment, repository, principal = _seed(session)
    version = _job_version(environment, repository, "c" * 40)
    session.add(version)
    session.flush()
    session.add(
        DeployRun(
            id=uuid.uuid4(),
            environment_id=environment.id,
            repository_id=repository.id,
            principal_id=principal.id,
            kind=DeployKind.DRY_RUN,
            status=DeployStatus.QUEUED,
        )
    )
    session.add(
        EnvironmentDeployment(
            environment_id=environment.id,
            repository_id=repository.id,
            current_job_version_id=version.id,
        )
    )
    session.commit()
    session.delete(repository)
    session.commit()
    assert session.query(DeployRun).count() == 0
    assert session.query(JobVersion).count() == 0
    assert session.query(EnvironmentDeployment).count() == 0


def test_new_actions_go_only_to_admin_and_never_to_task_tokens() -> None:
    """DEPLOY and MANAGE_REPOSITORY are admin-only and not task-token actions."""
    for action in (Action.DEPLOY, Action.MANAGE_REPOSITORY):
        assert action not in TASK_TOKEN_ACTIONS
        assert action in ROLE_ACTIONS[Role.ADMIN]
        for role in (Role.VIEWER, Role.ANALYST, Role.DATA_SCIENTIST, Role.DATA_ENGINEER):
            assert action not in ROLE_ACTIONS[role]


def test_new_audit_event_types_exist() -> None:
    """The six event types of the deploy and run engine."""
    assert {
        AuditEventType.REPOSITORY_CREATED,
        AuditEventType.REPOSITORY_UPDATED,
        AuditEventType.REPOSITORY_DELETED,
        AuditEventType.DEPLOYED,
        AuditEventType.DEPLOY_DRY_RUN,
        AuditEventType.ROLLED_BACK,
    } <= set(AuditEventType)
