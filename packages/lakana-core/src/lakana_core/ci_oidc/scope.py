"""What a CI token may ask a `DEPLOY` check for, beyond what the authorizer narrows.

`GrantAuthorization` lets a CI-scoped principal do nothing but `DEPLOY` on its environment.
`DEPLOY` covers more than queuing a deploy, so each `DEPLOY` check decides the rest:

- `DeployService.submit` takes one submission (`check_ci_request`, then `use_ci_token` in the
  transaction that queues the run): the token's repository, a kind its rule allows, never a
  rollback, and for a deploy the signed commit only;
- `DeployService.get` shows the token's own run only;
- `DeployService.authorize_deploy`, the worker's re-check, resolves the token again and repeats
  `check_ci_request`; after the fetch, `check_ci_commit`;
- every other `DEPLOY` check (undeploy, pausing, resuming and testing a job, reading a
  notification delivery) refuses a CI token (`refuse_ci_token`).

Each refusal is audited as `AUTHORIZATION_DENIED` with ``reason: ci_token``, through the denial
limiter of an `AuditingAuthorization`.
"""

from __future__ import annotations

from datetime import datetime
from typing import NoReturn

from sqlalchemy import exists, update
from sqlalchemy.orm import Session

from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.auth import Action, AuthorizationService, CiScope, PrincipalRef, Securable
from lakana_core.db import rowcount
from lakana_core.errors import PermissionDenied
from lakana_core.models import CiToken, CiTrustRule, DeployKind
from lakana_core.projects.git_input import validate_ref

CI_DENIAL_REASON = "ci_token"
"""The `reason` of an `AUTHORIZATION_DENIED` row for a refusal of this module."""

ALREADY_USED_MESSAGE = (
    "This CI token has already submitted its deploy or dry run, or is no longer valid; each CI "
    "token submits one. Exchange a new ID token in the CI job for the next one."
)


def ci_refusal(
    authz: AuthorizationService, p: PrincipalRef, environment: str, message: str
) -> NoReturn:
    """Audit a refusal of a CI token's request, and refuse it.

    Parameters
    ----------
    authz
        The caller's authorization service; an `AuditingAuthorization` records the denial
        through its limiter, any other records nothing.
    p
        The CI-scoped principal.
    environment
        The environment of the request.
    message
        What the caller is told.

    Raises
    ------
    PermissionDenied
        Always, with `message`.
    """
    if isinstance(authz, AuditingAuthorization):
        authz.record_denial(
            p, Action.DEPLOY, Securable.environment_scope(environment), reason=CI_DENIAL_REASON
        )
    raise PermissionDenied(message)


def refuse_ci_token(
    authz: AuthorizationService, p: PrincipalRef, environment: str, what: str
) -> None:
    """Refuse a CI token at a `DEPLOY` check that is not for queuing or reading its run.

    Parameters
    ----------
    authz
        The caller's authorization service.
    p
        The acting principal; nothing happens unless it carries a `CiScope`.
    environment
        The environment of the request.
    what
        What was asked, as a verb phrase (``undeploy a repository``).

    Raises
    ------
    PermissionDenied
        If `p` is CI-scoped.
    """
    if p.ci_scope is not None:
        ci_refusal(
            authz,
            p,
            environment,
            f"A CI token may only queue its one deploy or dry run and read that run; it may not "
            f"{what}. Use a personal access token for that.",
        )


def check_ci_request(
    authz: AuthorizationService,
    p: PrincipalRef,
    scope: CiScope,
    *,
    environment: str,
    environment_id: object,
    repository: str,
    repository_id: object,
    kind: DeployKind,
    ref: str | None,
) -> str | None:
    """Check that a deploy run is one the CI token may queue, and pin a deploy to its commit.

    Parameters
    ----------
    authz
        The caller's authorization service.
    p
        The CI-scoped principal.
    scope
        Its scope.
    environment
        The run's environment name.
    environment_id
        Its id.
    repository
        The run's repository name.
    repository_id
        Its id.
    kind
        The run's kind.
    ref
        The ref asked for; `None` for the default.

    Returns
    -------
    str or None
        The ref to record: the signed commit for a deploy, `ref` for a dry run.

    Raises
    ------
    PermissionDenied
        For another environment or repository, a rollback, a kind the rule does not allow, a
        deploy without a signed commit, or a deploy of another ref than that commit.
    """
    message: str | None = None
    if environment_id != scope.environment_id:
        message = f"This CI token may deploy only to {scope.environment!r}."
    elif repository_id != scope.repository_id:
        message = f"This CI token may deploy only {scope.repository!r}, not {repository[:255]!r}."
    elif kind is DeployKind.ROLLBACK:
        message = "A CI token may not queue a rollback; a person rolls back with their own token."
    elif kind.value not in scope.kinds:
        message = (
            f"This CI token's trust rule allows {', '.join(sorted(scope.kinds))}, not {kind.value}."
        )
    elif kind is DeployKind.DEPLOY:
        if scope.sha is None:
            message = (
                "This CI token carries no commit (its ID token had no sha claim), so it may "
                "queue only a dry run."
            )
        elif ref is not None and validate_ref(ref) != scope.sha:
            message = (
                "This CI token may deploy only the commit its CI job runs at, "
                f"{scope.sha}; leave out the ref or pass that commit."
            )
        else:
            return scope.sha
    if message is not None:
        ci_refusal(authz, p, environment, message)
    return ref


def check_ci_commit(
    authz: AuthorizationService, p: PrincipalRef, scope: CiScope, kind: DeployKind, sha: str
) -> None:
    """Check, after the fetch, that a CI token's deploy is of the signed commit.

    Parameters
    ----------
    authz
        The worker's authorization service.
    p
        The run's principal, CI-scoped.
    scope
        The token's scope.
    kind
        The run's kind; only a deploy is pinned.
    sha
        The commit the worker fetched.

    Raises
    ------
    PermissionDenied
        If a deploy fetched another commit.
    """
    if kind is DeployKind.DEPLOY and sha != scope.sha:
        ci_refusal(
            authz,
            p,
            scope.environment,
            f"The ref resolved to {sha}, not to the commit the CI token is for, {scope.sha}.",
        )


def use_ci_token(session: Session, scope: CiScope, now: datetime) -> bool:
    """Mark a CI token used, in the caller's transaction, if it still may submit.

    One conditional `UPDATE`: it changes the row only while the token is unused, unexpired and
    its rule still exists, is enabled and has the revision the token was issued under, so two
    submissions racing each other cannot both succeed.

    Parameters
    ----------
    session
        Database session; the caller commits or rolls back.
    scope
        The token's scope.
    now
        The time of the submission.

    Returns
    -------
    bool
        `True` when this call used the token.
    """
    result = session.execute(
        update(CiToken)
        .where(
            CiToken.id == scope.token_id,
            CiToken.used_at.is_(None),
            CiToken.expires_at > now,
            CiToken.rule_id == scope.rule_id,
            exists().where(
                CiTrustRule.id == CiToken.rule_id,
                CiTrustRule.enabled.is_(True),
                CiTrustRule.revision == CiToken.rule_revision,
            ),
        )
        .values(used_at=now)
        .execution_options(synchronize_session=False)
    )
    return rowcount(result) == 1
