"""Tests for the RowFilter and ColumnMask models' structural constraints."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.models import ColumnMask, Principal, PrincipalType, RowFilter


def _add_principal(session: Session, name: str) -> Principal:
    """Insert and return a bare principal for foreign-key targets.

    Parameters
    ----------
    session : Session
        The SQLAlchemy session.
    name : str
        The principal name.

    Returns
    -------
    Principal
        The persisted principal.
    """
    principal = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name=name)
    session.add(principal)
    session.commit()
    return principal


def test_a_row_filter_needs_exactly_one_subject(session: Session) -> None:
    """Setting neither principal_id nor group_id violates the check constraint."""
    admin = _add_principal(session, "admin")
    session.add(
        RowFilter(
            id=uuid.uuid4(),
            principal_id=None,
            group_id=None,
            environment="dev",
            namespace="gold",
            table="customers",
            expression="region = current_user()",
            created_by=admin.id,
            created_at=datetime.now(UTC),
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_a_well_formed_row_filter_persists(session: Session) -> None:
    """A row filter with exactly one subject commits cleanly."""
    admin = _add_principal(session, "admin")
    row = RowFilter(
        id=uuid.uuid4(),
        principal_id=admin.id,
        group_id=None,
        environment="dev",
        namespace="gold",
        table="customers",
        expression="region = current_user()",
        created_by=admin.id,
        created_at=datetime.now(UTC),
    )
    session.add(row)
    session.commit()
    assert session.get(RowFilter, row.id) is not None


def test_a_column_mask_needs_exactly_one_subject(session: Session) -> None:
    """Setting both principal_id and group_id violates the check constraint."""
    admin = _add_principal(session, "admin")
    session.add(
        ColumnMask(
            id=uuid.uuid4(),
            principal_id=admin.id,
            group_id=uuid.uuid4(),
            environment="dev",
            namespace="gold",
            table="customers",
            column="ssn",
            expression="NULL",
            created_by=admin.id,
            created_at=datetime.now(UTC),
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_a_well_formed_column_mask_persists(session: Session) -> None:
    """A column mask with exactly one subject commits cleanly."""
    admin = _add_principal(session, "admin")
    row = ColumnMask(
        id=uuid.uuid4(),
        principal_id=admin.id,
        group_id=None,
        environment="dev",
        namespace="gold",
        table="customers",
        column="ssn",
        expression="NULL",
        created_by=admin.id,
        created_at=datetime.now(UTC),
    )
    session.add(row)
    session.commit()
    assert session.get(ColumnMask, row.id) is not None
