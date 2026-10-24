"""The connections a write by a bound task token records as lineage sources.

A Python asset declares the connections it reads (`@asset(connections=[...])`); the deploy
records them in the project version manifest, and the token bound to one task attempt carries
them (`BoundTask.connections`, resolved from the database on every request). A table or an
artifact version that task writes gets one lineage edge per declared connection that still
exists, with `source_kind` ``connection``. Nothing a client sends can add one: `sources` names
tables only, and a personal access token or an unbound task token records none.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.identifiers import shown
from lakana_core.models import Connection, LineageEdge

logger = logging.getLogger(__name__)

CONNECTION_SOURCE = "connection"
"""`LineageEdge.source_kind` of an edge whose source is a connection."""


@dataclass(frozen=True)
class ConnectionSource:
    """A connection a write's task declared, for its lineage edge.

    Parameters
    ----------
    connection_id
        The connection's id.
    name
        Its name (a connection is never renamed).
    """

    connection_id: uuid.UUID
    name: str


def connection_sources(
    session: Session, p: PrincipalRef, environment_id: uuid.UUID
) -> list[ConnectionSource]:
    """Find the connections a write by `p` records as sources.

    Parameters
    ----------
    session
        Database session.
    p
        The acting principal.
    environment_id
        The environment of the write's target; connections are looked up there.

    Returns
    -------
    list of ConnectionSource
        One per connection the task's asset declares that the environment still has, in
        declared order. Empty unless `p` acts with a token bound to a task attempt. A declared
        name the environment no longer has is skipped and logged at info; a manifest value that
        cannot be read declares none and is logged once, as a warning.
    """
    bound = p.task_scope.bound if p.task_scope is not None else None
    if bound is None:
        return []
    if not bound.connections_readable:
        logger.warning(
            "Task run %s: the connections its deployed asset declares cannot be read, so its "
            "write records no connection as a lineage source. Deploy the repository again to "
            "replace the damaged record.",
            bound.task_run_id,
        )
        return []
    if not bound.connections:
        return []
    found = {
        name: connection_id
        for name, connection_id in session.execute(
            select(Connection.name, Connection.id).where(
                Connection.environment_id == environment_id,
                Connection.name.in_(bound.connections),
            )
        )
    }
    sources: list[ConnectionSource] = []
    for declared in bound.connections:
        connection_id = found.get(declared)
        if connection_id is None:
            logger.info(
                "Task run %s declares the connection '%s', which its environment no longer has; "
                "its write records no lineage edge for it.",
                bound.task_run_id,
                shown(declared),
            )
            continue
        sources.append(ConnectionSource(connection_id=connection_id, name=declared))
    return sources


def connection_edges(
    sources: Sequence[ConnectionSource],
    *,
    environment_id: uuid.UUID,
    target_id: uuid.UUID,
    target_name: str,
    target_snapshot_id: int | None,
    query_run_id: uuid.UUID | None,
    created_at: datetime,
) -> list[LineageEdge]:
    """Build one lineage edge per connection source, not yet added to a session.

    Parameters
    ----------
    sources
        The connections.
    environment_id
        The environment both ends live in.
    target_id
        The target table's or artifact's id.
    target_name
        Its dotted name.
    target_snapshot_id
        The snapshot (for an artifact, the version) the write committed.
    query_run_id
        The run writing, if any.
    created_at
        When the write happened.

    Returns
    -------
    list of LineageEdge
        The edges, `source_kind` ``connection`` and no source snapshot.
    """
    return [
        LineageEdge(
            id=uuid.uuid4(),
            environment_id=environment_id,
            source_kind=CONNECTION_SOURCE,
            source_table_id=source.connection_id,
            source_name=source.name,
            source_snapshot_id=None,
            target_table_id=target_id,
            target_name=target_name,
            target_snapshot_id=target_snapshot_id,
            query_run_id=query_run_id,
            created_at=created_at,
        )
        for source in sources
    ]
