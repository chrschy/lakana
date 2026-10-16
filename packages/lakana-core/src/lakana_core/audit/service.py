"""Recording and querying audit events.

`record` never authorizes anything — it is a side effect of an action whose
own authorization already happened (or was denied) one call earlier, and
requiring authorization to log a denial would be circular. `query` is the
one method here that is itself a principal-facing, authorized operation,
gated by `Action.READ_AUDIT_LOG`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.errors import ValidationError
from lakana_core.models import AuditEvent, AuditEventType


@dataclass(frozen=True)
class AuditCursor:
    """A position in `AuditService.query`'s newest-first order, for keyset paging.

    Parameters
    ----------
    occurred_at
        The `occurred_at` of the last event already seen. Must be aware.
    id
        The `id` of that event, breaking ties between events that share an
        `occurred_at`.
    """

    occurred_at: datetime
    id: uuid.UUID


class AuditService:
    """Record audit events, and read them back with authorization.

    Parameters
    ----------
    session
        Database session.
    authz
        Authorization service consulted by `query`. Optional and `None` by
        default: `record` never uses it, and five of this plan's six call
        sites only ever call `record` — requiring a real
        `AuthorizationService` there would force each of them to construct
        or pass through one purely as ceremony. A `ValidationError` is
        raised instead, at the point `query` is actually called without one.
    """

    def __init__(self, session: Session, authz: AuthorizationService | None = None) -> None:
        self._session = session
        self._authz = authz

    def record(
        self,
        principal_id: uuid.UUID | None,
        event_type: AuditEventType,
        *,
        securable: Securable | None = None,
        detail: dict | None = None,
        commit: bool = False,
    ) -> AuditEvent:
        """Create one audit event.

        Parameters
        ----------
        principal_id
            The principal the event is attributed to, or `None` for a
            failed authentication (no principal was ever verified).
        event_type
            The kind of event.
        securable
            The object the event's action targets, if any. Populates the
            four `securable_*` columns; left `None` for events with no
            natural target (authentication).
        detail
            Event-specific facts not already captured by the columns
            above. Defaults to `{}`.
        commit
            If `True`, commits immediately after adding the row — for a
            call site with no other action already about to commit in the
            same transaction (an authentication check, an authorization
            denial). If `False` (the default), the row is only added to
            the session; the caller's own subsequent commit persists it
            atomically alongside whatever action it records.

        Returns
        -------
        AuditEvent
            The created (not yet necessarily committed) row.
        """
        event = AuditEvent(
            id=uuid.uuid4(),
            occurred_at=datetime.now(UTC),
            principal_id=principal_id,
            event_type=event_type,
            securable_kind=securable.kind if securable is not None else None,
            securable_environment=securable.environment if securable is not None else None,
            securable_namespace=(
                securable.namespace.joined()
                if securable is not None and securable.namespace is not None
                else None
            ),
            securable_table=securable.table if securable is not None else None,
            detail=detail or {},
        )
        self._session.add(event)
        if commit:
            self._session.commit()
        return event

    def query(
        self,
        p: PrincipalRef,
        *,
        event_type: AuditEventType | None = None,
        environment: str | None = None,
        principal_id: uuid.UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
        cursor: AuditCursor | None = None,
    ) -> list[AuditEvent]:
        """Read audit events, most recently occurred first.

        Parameters
        ----------
        p
            The acting principal.
        event_type
            Only return events of this type, if given.
        environment
            Only return events whose `securable_environment` matches, if
            given.
        principal_id
            Only return events attributed to this principal, if given.
        since
            Only return events at or after this time, if given.
        until
            Only return events at or before this time, if given.
        limit
            The maximum number of events to return. Defaults to 100. The
            caller (the REST endpoint) is responsible for bounding this to a
            sane range before it reaches here.
        cursor
            Only return events strictly after this position in newest-first
            order, if given — the last event of the previous page. Keyset
            paging on `(occurred_at, id)`: a later write never shifts a page
            boundary, since it sorts before any cursor already handed out.

        Returns
        -------
        list of AuditEvent
            Matching events, newest `occurred_at` first.

        Raises
        ------
        ValidationError
            If this `AuditService` was constructed with no
            `AuthorizationService`.
        PermissionDenied
            If `p` may not read the audit log.
        """
        if self._authz is None:
            raise ValidationError(
                "AuditService.query requires an AuthorizationService; "
                "this instance was constructed with none."
            )
        self._authz.authorize(p, Action.READ_AUDIT_LOG, Securable.instance_scope())
        stmt = select(AuditEvent)
        if event_type is not None:
            stmt = stmt.where(AuditEvent.event_type == event_type)
        if environment is not None:
            stmt = stmt.where(AuditEvent.securable_environment == environment)
        if principal_id is not None:
            stmt = stmt.where(AuditEvent.principal_id == principal_id)
        if since is not None:
            stmt = stmt.where(AuditEvent.occurred_at >= since)
        if until is not None:
            stmt = stmt.where(AuditEvent.occurred_at <= until)
        if cursor is not None:
            stmt = stmt.where(
                or_(
                    AuditEvent.occurred_at < cursor.occurred_at,
                    and_(AuditEvent.occurred_at == cursor.occurred_at, AuditEvent.id < cursor.id),
                )
            )
        stmt = stmt.order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc()).limit(limit)
        return list(self._session.execute(stmt).scalars().all())
