"""A task token never sees a dropped table's lineage node (real grants)."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime

from core_test_helpers import ensure_table
from sqlalchemy.orm import Session

from lakana_core.auth import Action, PrincipalRef, SecurableKind, TaskScope
from lakana_core.catalog.lineage_graph import LineageService
from lakana_core.connections.kinds import ConnectionKind
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.identifiers import TableRef
from lakana_core.models import Connection, Grant, LineageEdge, Principal, PrincipalType

_T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _bot(session: Session, environment_id: uuid.UUID, *, token: bool) -> PrincipalRef:
    """Create a principal with READ_TABLE on namespace `gold`, optionally acting by task token.

    Parameters
    ----------
    session
        Database session.
    environment_id
        The `dev` environment.
    token
        Whether the returned reference carries a task scope for `gold.t` only.

    Returns
    -------
    PrincipalRef
        The principal, with or without the task scope.
    """
    row = Principal(id=uuid.uuid4(), type=PrincipalType.SERVICE_ACCOUNT, name="bot")
    session.add(row)
    session.commit()
    session.add(
        Grant(
            id=uuid.uuid4(),
            principal_id=row.id,
            action=Action.READ_TABLE,
            kind=SecurableKind.NAMESPACE,
            environment="dev",
            namespace="gold",
            environment_id=environment_id,
            granted_by=row.id,
            granted_at=_T0,
        )
    )
    session.commit()
    scope = (
        TaskScope(environment="dev", asset_scope=["gold.t"], token_id=uuid.uuid4())
        if token
        else None
    )
    return PrincipalRef(id=row.id, name="bot", type=row.type, task_scope=scope)


def _setup(session: Session) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create `gold.t`, a live `gold.sibling`, and a dropped `gold.secret` feeding both.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    tuple of uuid.UUID
        The environment id, the dropped table's id and `gold.t`'s id.
    """
    env, t = ensure_table(session, "dev", "gold", "t")
    _, sibling = ensure_table(session, "dev", "gold", "sibling")
    dropped = uuid.uuid4()
    for target, name in ((t, "gold.t"), (sibling, "gold.sibling")):
        session.add(
            LineageEdge(
                id=uuid.uuid4(),
                environment_id=env,
                source_table_id=dropped,
                source_name="gold.secret",
                source_snapshot_id=11,
                target_table_id=target,
                target_name=name,
                target_snapshot_id=22,
                query_run_id=None,
                created_at=_T0,
            )
        )
    session.commit()
    return env, dropped, t


def test_a_task_token_never_sees_a_dropped_node_even_with_a_namespace_grant(
    session: Session,
) -> None:
    """The ruling: a token whose scope covers namespace `gold` must not reveal a dropped table."""
    env, dropped, _ = _setup(session)
    bot = _bot(session, env, token=True)

    graph = LineageService(session, GrantAuthorization(session)).lineage(
        bot, TableRef.from_dotted("dev", "gold.t"), depth=10
    )

    assert [node.name for node in graph.nodes] == ["gold.t"]
    assert all(node.id != dropped for node in graph.nodes)
    assert graph.edges == []
    assert graph.hidden_nodes == 1


def test_the_same_principal_without_a_token_still_sees_the_dropped_node(
    session: Session,
) -> None:
    """Only the token is narrowed; the namespace grant itself still shows the history."""
    env, dropped, _ = _setup(session)
    bot = _bot(session, env, token=False)

    graph = LineageService(session, GrantAuthorization(session)).lineage(
        bot, TableRef.from_dotted("dev", "gold.t"), depth=10
    )

    assert {node.id for node in graph.nodes} >= {dropped}
    assert len(graph.edges) == 1
    assert graph.hidden_nodes == 0


def _connection_setup(session: Session) -> tuple[uuid.UUID, uuid.UUID]:
    """Record that connection `crm-db` fed `gold.t`.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    tuple of uuid.UUID
        The environment id and the connection's id.
    """
    env, t = ensure_table(session, "dev", "gold", "t")
    creator = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="admin")
    session.add(creator)
    session.commit()
    connection = Connection(
        id=uuid.uuid4(),
        environment_id=env,
        name="crm-db",
        kind=ConnectionKind.POSTGRESQL,
        address={"host": "db.example.com", "port": 5432, "database": "crm"},
        settings={"user": "reader", "tls": "verify-full"},
        created_by=creator.id,
    )
    session.add(connection)
    session.add(
        LineageEdge(
            id=uuid.uuid4(),
            environment_id=env,
            source_kind="connection",
            source_table_id=connection.id,
            source_name="crm-db",
            source_snapshot_id=None,
            target_table_id=t,
            target_name="gold.t",
            target_snapshot_id=22,
            query_run_id=None,
            created_at=_T0,
        )
    )
    session.commit()
    return env, connection.id


def _grant_read_environment(session: Session, bot: PrincipalRef, environment_id: uuid.UUID) -> None:
    """Grant `bot` READ_ENVIRONMENT on `dev`.

    Parameters
    ----------
    session
        Database session.
    bot
        The grantee.
    environment_id
        The `dev` environment.
    """
    session.add(
        Grant(
            id=uuid.uuid4(),
            principal_id=bot.id,
            action=Action.READ_ENVIRONMENT,
            kind=SecurableKind.ENVIRONMENT,
            environment="dev",
            environment_id=environment_id,
            granted_by=bot.id,
            granted_at=_T0,
        )
    )
    session.commit()


def test_a_connection_needs_read_environment_with_real_grants(session: Session) -> None:
    """A table reader who may not read the environment sees the connection only as hidden."""
    env, connection = _connection_setup(session)
    bot = _bot(session, env, token=False)
    service = LineageService(session, GrantAuthorization(session))

    hidden = service.lineage(bot, TableRef.from_dotted("dev", "gold.t"))
    assert [node.name for node in hidden.nodes] == ["gold.t"]
    assert (hidden.edges, hidden.hidden_nodes) == ([], 1)

    _grant_read_environment(session, bot, env)
    shown = service.lineage(bot, TableRef.from_dotted("dev", "gold.t"))
    assert [(node.id, node.name, node.kind) for node in shown.nodes][1:] == [
        (connection, "crm-db", "CONNECTION")
    ]
    assert shown.hidden_nodes == 0


def test_a_task_token_never_sees_a_connection_node_with_real_grants(session: Session) -> None:
    """READ_ENVIRONMENT is a task-token action, but a task may not see connections."""
    env, _ = _connection_setup(session)
    plain = _bot(session, env, token=False)
    _grant_read_environment(session, plain, env)
    token = dataclasses.replace(
        plain,
        task_scope=TaskScope(environment="dev", asset_scope=["gold.t"], token_id=uuid.uuid4()),
    )

    graph = LineageService(session, GrantAuthorization(session)).lineage(
        token, TableRef.from_dotted("dev", "gold.t")
    )

    assert [node.name for node in graph.nodes] == ["gold.t"]
    assert (graph.edges, graph.hidden_nodes) == ([], 1)
