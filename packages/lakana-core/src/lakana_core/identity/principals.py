"""Principal management: create, deactivate, look up, list."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.errors import AlreadyExistsError, NotFoundError
from lakana_core.grants.service import assert_admin_headroom
from lakana_core.models import AuditEventType, Principal, PrincipalType


class PrincipalService:
    """Create, deactivate, and look up principals.

    Parameters
    ----------
    session
        Database session. Each method commits internally on success, the
        same contract `CatalogService` uses.
    authz
        Authorization service consulted before every mutating operation.
    """

    def __init__(self, session: Session, authz: AuthorizationService) -> None:
        self._session = session
        self._authz = authz

    def create_principal(self, p: PrincipalRef, *, name: str, type: PrincipalType) -> Principal:
        """Create a new principal.

        Parameters
        ----------
        p
            The acting principal.
        name
            Unique name for the new principal.
        type
            Whether this is a human user or a service account. Today,
            both authenticate identically, via PAT — `type` is a label for
            display and audit until M7 gives `USER` a second, distinct
            authentication path.

        Returns
        -------
        Principal
            The created row, active by default.

        Raises
        ------
        AlreadyExistsError
            If a principal with that name already exists.
        PermissionDenied
            If the acting principal may not manage principals.
        """
        self._authz.authorize(p, Action.MANAGE_PRINCIPAL, Securable.instance_scope())
        existing = self._session.execute(
            select(Principal).where(Principal.name == name)
        ).scalar_one_or_none()
        if existing is not None:
            raise AlreadyExistsError(f"Principal {name!r} already exists.")
        principal = Principal(id=uuid.uuid4(), type=type, name=name, is_active=True)
        self._session.add(principal)
        AuditService(self._session).record(
            p.id,
            AuditEventType.PRINCIPAL_CREATED,
            detail={"principal": name, "type": type.value},
        )
        self._session.commit()
        return principal

    def deactivate_principal(self, p: PrincipalRef, name: str) -> None:
        """Deactivate a principal, revoking its access without deleting it.

        Idempotent: deactivating an already-inactive principal is a no-op
        — it records no second `PRINCIPAL_DEACTIVATED` event, and does not
        re-run the admin-headroom check (an already-inactive principal
        holds no headroom to lose).

        Parameters
        ----------
        p
            The acting principal.
        name
            Name of the principal to deactivate.

        Raises
        ------
        NotFoundError
            If no principal has that name.
        ConflictError
            If deactivating this principal would leave zero active
            instance admins.
        PermissionDenied
            If the acting principal may not manage principals.
        """
        self._authz.authorize(p, Action.MANAGE_PRINCIPAL, Securable.instance_scope())
        principal = self.get_principal_by_name(p, name)
        if not principal.is_active:
            return
        assert_admin_headroom(self._session, excluding_principal_id=principal.id)
        principal.is_active = False
        AuditService(self._session).record(
            p.id,
            AuditEventType.PRINCIPAL_DEACTIVATED,
            detail={"principal": name, "type": principal.type.value},
        )
        self._session.commit()

    def get_principal_by_name(self, p: PrincipalRef, name: str) -> Principal:
        """Fetch a principal by name.

        Parameters
        ----------
        p
            The acting principal.
        name
            Principal name.

        Returns
        -------
        Principal
            The matching row.

        Raises
        ------
        NotFoundError
            If no principal has that name.
        PermissionDenied
            If the acting principal may not manage principals.
        """
        self._authz.authorize(p, Action.MANAGE_PRINCIPAL, Securable.instance_scope())
        principal = self._session.execute(
            select(Principal).where(Principal.name == name)
        ).scalar_one_or_none()
        if principal is None:
            raise NotFoundError(f"No principal named {name!r}.")
        return principal

    def list_principals(self, p: PrincipalRef) -> list[Principal]:
        """List every principal.

        Unfiltered: per-row filtering is sub-project 2's call-site
        change, not this service's (Constraint 9 carry-forward).

        Parameters
        ----------
        p
            The acting principal.

        Returns
        -------
        list of Principal
            Every principal.

        Raises
        ------
        PermissionDenied
            If the acting principal may not manage principals.
        """
        self._authz.authorize(p, Action.MANAGE_PRINCIPAL, Securable.instance_scope())
        return list(self._session.execute(select(Principal)).scalars().all())
