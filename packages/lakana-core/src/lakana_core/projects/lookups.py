"""Name lookups shared by the deploy and run services.

One implementation of "find the environment, repository or deployment pointer
or raise 404", so the two services cannot drift apart in what they disclose.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.errors import NotFoundError
from lakana_core.models import Environment, EnvironmentDeployment, Repository


def environment_by_name(session: Session, name: str) -> Environment:
    """Look an environment up by name.

    Parameters
    ----------
    session
        Database session.
    name
        Environment name.

    Returns
    -------
    Environment
        The row.

    Raises
    ------
    NotFoundError
        If none has that name.
    """
    env = session.execute(select(Environment).where(Environment.name == name)).scalar_one_or_none()
    if env is None:
        raise NotFoundError(f"No environment named {name!r}.")
    return env


def repository_by_name(session: Session, name: str) -> Repository:
    """Look a repository up by name.

    Parameters
    ----------
    session
        Database session.
    name
        Repository name.

    Returns
    -------
    Repository
        The row.

    Raises
    ------
    NotFoundError
        If none has that name.
    """
    repo = session.execute(select(Repository).where(Repository.name == name)).scalar_one_or_none()
    if repo is None:
        raise NotFoundError(f"No repository named {name!r}.")
    return repo


def deployment_row(session: Session, env: Environment, repo: Repository) -> EnvironmentDeployment:
    """Return the deployment pointer of a repository in an environment.

    Parameters
    ----------
    session
        Database session.
    env
        The environment.
    repo
        The repository.

    Returns
    -------
    EnvironmentDeployment
        The row, freshly read.

    Raises
    ------
    NotFoundError
        If the repository is not deployed there.
    """
    row = session.execute(
        select(EnvironmentDeployment)
        .where(
            EnvironmentDeployment.environment_id == env.id,
            EnvironmentDeployment.repository_id == repo.id,
        )
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"Repository {repo.name!r} is not deployed in {env.name!r}.")
    return row
