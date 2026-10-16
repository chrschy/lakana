"""Shared test fixtures for lakana-core.

Plain helper functions and classes live in `core_test_helpers.py`, not here
— see the module docstring there for why.
"""

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.service import CatalogService
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.models import Base, Principal, PrincipalType
from lakana_core.storage.resolver import StorageResolver


@pytest.fixture
def session() -> Iterator[Session]:
    """Provide a session against a fresh in-memory SQLite database.

    The engine comes from `lakana_core.db.create_engine`, the same factory the
    server uses, so foreign key enforcement here is the enforcement that
    ships rather than a fixture-only approximation of it.

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
def principal(session: Session) -> PrincipalRef:
    """Provide the acting principal, persisted as a `Principal` row.

    Persisted, not just a bare `PrincipalRef`:
    `CatalogService.create_environment`/`delete_environment`/
    `update_environment_storage`/`set_environment_protection` now each
    record an `AuditEvent` in the same transaction, and
    `AuditEvent.principal_id` is a foreign key onto `principals.id`. The
    column is nullable, but SQLite (with `PRAGMA foreign_keys=ON`, which
    `lakana_core.db.create_engine` sets) still enforces the constraint for
    any non-`NULL` value, so a principal that exists only as a
    `PrincipalRef` would fail every one of those calls with a foreign-key
    `IntegrityError` the moment it tried to audit itself.

    Parameters
    ----------
    session
        Database session to persist the row into.

    Returns
    -------
    PrincipalRef
        A reference to a principal that actually exists as a row.
    """
    ref = PrincipalRef(id=uuid.uuid4(), name="admin", type=PrincipalType.USER)
    session.add(Principal(id=ref.id, type=ref.type, name=ref.name, is_active=True))
    session.commit()
    return ref


@pytest.fixture
def catalog(session: Session) -> CatalogService:
    """Provide a catalog service wired to allow-all authorization.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    CatalogService
        The service under test.
    """
    return CatalogService(session, StorageResolver(session), AllowAllAuthorization())
