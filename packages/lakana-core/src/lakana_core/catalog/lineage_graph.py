# packages/lakana-core/src/lakana_core/catalog/lineage_graph.py
"""Reading persisted table-level lineage back, pruned by grants.

Edges are written by every governed write (`CatalogService.write_batches`,
`append`, `create_or_replace_view`). This walks them from one table, a
bounded number of hops in one direction, and shows only what the caller may
read: a node the caller can't READ is omitted, counted in `hidden_nodes`,
and never walked through, so lineage can't reveal a hidden table's name or
that two readable tables are connected through one. A dropped
table's node is judged at the namespace it was recorded in, never as a
table, so a table recreated under its name does not reveal it.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.catalog.identity import resolve_environment_id, resolve_table_id
from lakana_core.errors import NotFoundError, ValidationError
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import LineageEdge, Namespace, Table

DEFAULT_LINEAGE_DEPTH = 3
"""Hops walked when the caller names none."""

MAX_LINEAGE_DEPTH = 10
"""The most hops a caller may ask for."""

MAX_LINEAGE_NODES = 500
"""The most nodes one request returns; the walk stops there and flags `truncated`."""


class LineageDirection(enum.StrEnum):
    """Which way to walk from the root table."""

    UPSTREAM = "upstream"
    DOWNSTREAM = "downstream"


@dataclass(frozen=True)
class LineageNode:
    """One table, external table or view in a lineage graph.

    Parameters
    ----------
    id
        Its id.
    name
        Its current dotted name, or the name recorded on its edges once
        dropped.
    kind
        ``TABLE``, ``EXTERNAL``, ``VIEW`` or ``ARTIFACT``; `None` once dropped. It says
        what a link's snapshot ids at this node mean: for an ``ARTIFACT`` they are
        version numbers.
    dropped
        Whether the table no longer exists.
    """

    id: uuid.UUID
    name: str
    kind: str | None
    dropped: bool


@dataclass(frozen=True)
class LineageLink:
    """One edge: `source` fed a write into `target`, the most recent such write.

    Parameters
    ----------
    source_id
        The source table's id.
    target_id
        The target table's id.
    source_snapshot_id
        The source's snapshot the write read, if known; the version number when
        the source is an artifact.
    target_snapshot_id
        The target's snapshot the write committed, if any; the version number when
        the target is an artifact.
    query_run_id
        The run that wrote it, if any.
    created_at
        When it was written.
    """

    source_id: uuid.UUID
    target_id: uuid.UUID
    source_snapshot_id: int | None
    target_snapshot_id: int | None
    query_run_id: uuid.UUID | None
    created_at: datetime


@dataclass(frozen=True)
class LineageGraph:
    """What `LineageService.lineage` returns.

    Parameters
    ----------
    root_id
        The table asked about.
    direction
        The direction walked.
    depth
        The hops walked.
    nodes
        Every visible node, the root first.
    edges
        One edge per `(source, target)` between visible nodes.
    hidden_nodes
        How many nodes were omitted because the caller can't read them.
    truncated
        Whether the walk stopped at `MAX_LINEAGE_NODES` with a node the
        caller may read left out. A hidden node never sets it.
    """

    root_id: uuid.UUID
    direction: LineageDirection
    depth: int
    nodes: list[LineageNode]
    edges: list[LineageLink]
    hidden_nodes: int
    truncated: bool


class LineageService:
    """Read persisted lineage.

    Parameters
    ----------
    session
        Database session.
    authz
        Authorization: `authorize` for the root, `permits` for every other
        node (visibility, not an action).
    """

    def __init__(self, session: Session, authz: AuthorizationService) -> None:
        self._session = session
        self._authz = authz

    def lineage(
        self,
        p: PrincipalRef,
        ref: TableRef,
        *,
        direction: LineageDirection = LineageDirection.UPSTREAM,
        depth: int = DEFAULT_LINEAGE_DEPTH,
    ) -> LineageGraph:
        """Walk a table's lineage.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The root table.
        direction
            Upstream (what fed it) or downstream (what it fed).
        depth
            Hops to walk, 1 to `MAX_LINEAGE_DEPTH`.

        Returns
        -------
        LineageGraph
            The visible nodes and edges, and how many nodes were hidden.

        Raises
        ------
        ValidationError
            If `depth` is out of range.
        PermissionDenied
            If `p` may not read the root.
        NotFoundError
            If the environment or the root table does not exist.
        """
        if not 1 <= depth <= MAX_LINEAGE_DEPTH:
            raise ValidationError(f"depth must be between 1 and {MAX_LINEAGE_DEPTH}; got {depth}.")
        self._authz.authorize(
            p, Action.READ_TABLE, Securable.table_scope(ref.environment, ref.namespace, ref.name)
        )
        environment_id = resolve_environment_id(self._session, ref.environment)
        root_id = resolve_table_id(self._session, ref.environment, ref.namespace.joined(), ref.name)
        if environment_id is None or root_id is None:
            raise NotFoundError(f"Table {ref.dotted()!r} not found.")
        root, _ = self._node(ref.environment, root_id, ref.dotted())
        nodes: dict[uuid.UUID, LineageNode] = {root_id: root}
        links: list[LineageEdge] = []
        hidden: set[uuid.UUID] = set()
        truncated = False
        frontier = [root_id]
        for _ in range(depth):
            if not frontier:
                break
            next_frontier: list[uuid.UUID] = []
            for edge in self._edges(environment_id, frontier, direction):
                other_id, other_name = (
                    (edge.source_table_id, edge.source_name)
                    if direction is LineageDirection.UPSTREAM
                    else (edge.target_table_id, edge.target_name)
                )
                if other_id in hidden:
                    continue
                if other_id not in nodes:
                    node, securable = self._node(ref.environment, other_id, other_name)
                    # A dropped node is never shown to a task token: a token's scope
                    # patterns cannot say which dropped table they were meant to cover.
                    if (
                        securable is None
                        or (node.dropped and p.task_scope is not None)
                        or not self._authz.permits(p, Action.READ_TABLE, securable)
                    ):
                        hidden.add(other_id)
                        continue
                    # Checked after visibility: a node the caller can't read
                    # is counted as hidden, never as truncation, so
                    # `truncated` can't reveal one lying past the cap.
                    if len(nodes) >= MAX_LINEAGE_NODES:
                        truncated = True
                        break
                    nodes[other_id] = node
                    next_frontier.append(other_id)
                links.append(edge)
            if truncated:
                break
            frontier = next_frontier
        return LineageGraph(
            root_id=root_id,
            direction=direction,
            depth=depth,
            nodes=list(nodes.values()),
            edges=[_link(edge) for edge in links],
            hidden_nodes=len(hidden),
            truncated=truncated,
        )

    def _edges(
        self,
        environment_id: uuid.UUID,
        frontier: list[uuid.UUID],
        direction: LineageDirection,
    ) -> list[LineageEdge]:
        """Fetch the edges one hop from a whole level, latest per pair.

        One query per level; the newest edge per `(source, target)` pair is
        chosen in SQL, so edge history is never loaded.

        Parameters
        ----------
        environment_id
            The environment.
        frontier
            The nodes of this level.
        direction
            Which end the frontier nodes are.

        Returns
        -------
        list of LineageEdge
            One edge per pair, oldest first.
        """
        end = (
            LineageEdge.target_table_id
            if direction is LineageDirection.UPSTREAM
            else LineageEdge.source_table_id
        )
        newest = (
            select(
                LineageEdge.source_table_id.label("source_id"),
                LineageEdge.target_table_id.label("target_id"),
                func.max(LineageEdge.created_at).label("created_at"),
            )
            .where(LineageEdge.environment_id == environment_id, end.in_(frontier))
            .group_by(LineageEdge.source_table_id, LineageEdge.target_table_id)
            .subquery()
        )
        rows = self._session.execute(
            select(LineageEdge)
            .join(
                newest,
                (LineageEdge.source_table_id == newest.c.source_id)
                & (LineageEdge.target_table_id == newest.c.target_id)
                & (LineageEdge.created_at == newest.c.created_at),
            )
            .where(LineageEdge.environment_id == environment_id)
            .order_by(LineageEdge.created_at, LineageEdge.id)
        ).scalars()
        chosen: dict[tuple[uuid.UUID, uuid.UUID], LineageEdge] = {}
        for edge in rows:
            chosen[edge.source_table_id, edge.target_table_id] = edge
        return list(chosen.values())

    def _node(
        self, environment: str, table_id: uuid.UUID, recorded_name: str
    ) -> tuple[LineageNode, Securable | None]:
        """Describe a node, and the securable its visibility is checked against.

        A live table is checked under its current name, which resolves to
        this very row. A dropped table is checked at the *namespace* its
        edges recorded, never as a table: its name may since belong to a
        different table, so a table-level grant must not decide.
        Only a grant on that namespace or above, which would have covered
        the table, shows it.
        The caller (`lineage`) hides a dropped node from any principal acting
        with a task token.

        Parameters
        ----------
        environment
            The environment.
        table_id
            The node's id.
        recorded_name
            The dotted name its edge recorded.

        Returns
        -------
        tuple
            The node, and its securable — `None` when a dropped node's
            recorded name doesn't parse, which is treated as unreadable.
        """
        row = self._session.get(Table, table_id)
        namespace = None if row is None else self._session.get(Namespace, row.namespace_id)
        if row is not None and namespace is not None:
            path = NamespacePath.parse(namespace.path)
            node = LineageNode(id=table_id, name=f"{path}.{row.name}", kind=row.kind, dropped=False)
            return node, Securable.table_scope(environment, path, row.name)
        node = LineageNode(id=table_id, name=recorded_name, kind=None, dropped=True)
        try:
            recorded = TableRef.from_dotted(environment, recorded_name)
        except ValidationError:
            return node, None
        return node, Securable.namespace_scope(environment, recorded.namespace)


def _link(edge: LineageEdge) -> LineageLink:
    """Convert a stored edge.

    Parameters
    ----------
    edge
        The row.

    Returns
    -------
    LineageLink
        The API-facing edge.
    """
    return LineageLink(
        source_id=edge.source_table_id,
        target_id=edge.target_table_id,
        source_snapshot_id=edge.source_snapshot_id,
        target_snapshot_id=edge.target_snapshot_id,
        query_run_id=edge.query_run_id,
        created_at=edge.created_at,
    )
