"""The audit of a privileged view of other people's activity (`ACTIVITY_VIEWED`).

A manager's listing of other people's queries, the readers of a table and a read of the audit
log (`AuditService.query`) show what other principals did. `record_view` writes one
`ACTIVITY_VIEWED` row for such a view, on a session of its own that commits before the caller
builds its answer, and at most one per viewer, view, scope and targeting key within
`LAKANA_VIEW_AUDIT__WINDOW_MINUTES`: the next page, another status filter and a screen that polls
write nothing more. The window is checked by a lookup in
`audit_events`, served by the index on `(event_type, principal_id, occurred_at)`, so it holds
across server processes and restarts. Two requests at the same moment may both write a row; the
window only ever saves rows, it never loses the first.

It fails closed: if the lookup or the write fails, `AuditUnavailableError` is raised and the
caller returns none of the activity.

A view that shows nobody else's activity, and names nobody else, writes nothing. The detail
holds the filters and counts, and the targeting key the viewer typed, never the names of the
people seen, row ids or SQL.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import PrincipalRef, Securable
from lakana_core.config import ViewAuditConfig
from lakana_core.errors import AuditUnavailableError
from lakana_core.models import AuditEvent, AuditEventType

logger = logging.getLogger(__name__)


class View(enum.StrEnum):
    """What a privileged view showed; `detail["view"]` of an `ACTIVITY_VIEWED` row."""

    QUERIES = "queries"
    TABLE_READERS = "table_readers"
    AUDIT_LOG = "audit_log"


@dataclass(frozen=True)
class ViewDetail:
    """The filters and counts of one privileged view.

    Parameters
    ----------
    filters
        The filters of the read, without the targeting key and without the cursor. Values only
        the viewer chose (a status, a number of days), never a name of someone seen.
    rows_shown
        How many rows the answer holds.
    others_shown
        How many of them are another principal's activity (a deleted principal included).
    distinct_others
        How many different principals other than the viewer those rows belong to.
    """

    filters: Mapping[str, object]
    rows_shown: int
    others_shown: int
    distinct_others: int


def sessions_like(session: Session) -> Callable[[], Session]:
    """Make new sessions on the database of an existing one.

    Parameters
    ----------
    session
        A service's session; its bind is reused, never its transaction.

    Returns
    -------
    callable
        A function that opens a new session, so an audit row commits on its own.
    """
    bind = session.get_bind()
    return lambda: Session(bind)


def _already_recorded(
    session: Session,
    principal: PrincipalRef,
    view: View,
    scope: Securable,
    targeting_key: dict[str, str] | None,
    since: datetime,
) -> bool:
    """Say whether the same view already has a row within the window.

    Parameters
    ----------
    session
        The audit session.
    principal
        The viewer.
    view
        What was viewed.
    scope
        Where.
    targeting_key
        The targeting key, or `None`.
    since
        The start of the window.

    Returns
    -------
    bool
        `True` if a row of the same viewer, view, scope and key occurred at or after `since`.
    """
    namespace = scope.namespace.joined() if scope.namespace is not None else None
    details = session.scalars(
        select(AuditEvent.detail).where(
            AuditEvent.event_type == AuditEventType.ACTIVITY_VIEWED,
            AuditEvent.principal_id == principal.id,
            AuditEvent.occurred_at >= since,
            AuditEvent.securable_kind == scope.kind,
            AuditEvent.securable_environment == scope.environment,
            AuditEvent.securable_namespace == namespace,
            AuditEvent.securable_table == scope.table,
        )
    ).all()
    return any(
        isinstance(detail, dict)
        and detail.get("view") == view.value
        and detail.get("targeting_key") == targeting_key
        for detail in details
    )


def record_view(
    session_factory: Callable[[], Session],
    principal: PrincipalRef,
    view: View,
    scope: Securable,
    targeting_key: Mapping[str, str] | None,
    detail: ViewDetail,
    *,
    config: ViewAuditConfig,
) -> bool:
    """Record that `principal` viewed other people's activity, once per window.

    Nothing is written when `config.enabled` is false, or when the view shows no other
    principal's activity and names nobody (`targeting_key` is `None`). Otherwise the row is
    written unless the same viewer has one for the same view, scope and targeting key within
    `config.window_minutes`.

    Parameters
    ----------
    session_factory
        Opens the session the lookup and the row use; it is committed and closed here, apart
        from the caller's own transaction (`sessions_like`).
    principal
        The viewer.
    view
        What was viewed.
    scope
        The object viewed: the environment of a query list, the table of a readers list, the
        instance for the audit log.
    targeting_key
        The filter that names one other principal (``{"submitter": name}`` of the query list,
        ``{"principal_id": id}`` of the audit log), or `None` when there is none or it names
        the viewer. The caller passes `None` for a filter on the viewer.
    detail
        The filters and counts.
    config
        `LAKANA_VIEW_AUDIT__*`.

    Returns
    -------
    bool
        Whether a row was written.

    Raises
    ------
    AuditUnavailableError
        If the lookup or the write failed; the caller must not return the activity.
    """
    if not config.enabled:
        return False
    key = dict(targeting_key) if targeting_key is not None else None
    if detail.others_shown == 0 and key is None:
        return False
    try:
        with session_factory() as session:
            since = datetime.now(UTC) - timedelta(minutes=config.window_minutes)
            if _already_recorded(session, principal, view, scope, key, since):
                return False
            AuditService(session).record(
                principal.id,
                AuditEventType.ACTIVITY_VIEWED,
                securable=scope,
                detail={
                    "view": view.value,
                    "filters": dict(detail.filters),
                    "rows_shown": detail.rows_shown,
                    "others_shown": detail.others_shown,
                    "distinct_others": detail.distinct_others,
                    "targeting_key": key,
                },
                commit=True,
            )
    except Exception as exc:
        cause = type(exc).__name__
        logger.warning(
            "A read of other people's activity (%s) was refused: its ACTIVITY_VIEWED audit row "
            "could not be written (%s).",
            view.value,
            cause,
        )
        raise AuditUnavailableError(
            f"This read was refused because it could not be recorded in the audit log ({cause}): "
            "a read of other people's activity is always recorded. Try again in a moment; if it "
            "keeps failing, ask the operator to check the server's database (the server log "
            "names the error)."
        ) from exc
    return True
