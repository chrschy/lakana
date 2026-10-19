"""The two kinds of work, and which worker takes which.

The one definition of the kind of a query run: a SQL condition for statements and the same
rule for a loaded row. The dequeue, the claim, the cap's count, the wait reasons and the worker
counts use it and nothing else. Deploy runs and task runs are pipeline work by their table.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import (
    ColumnElement,
    Integer,
    ScalarSelect,
    and_,
    cast,
    func,
    literal,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, aliased
from sqlalchemy.orm.util import AliasedClass

from lakana_core.db import db_now
from lakana_core.models import Principal, QueryRun, QueryRunStatus, WriteMode

ALL, INTERACTIVE, PIPELINE = "all", "interactive", "pipeline"
"""The values of `LAKANA_WORKER__TAKES`; the last two are also the two kinds of work."""

logger = logging.getLogger(__name__)

CAP_LOCK_TIMEOUT_SECONDS = 5
"""How long a claim waits for another claim of the same principal (Postgres). The same value as
`catalog.service.LOCK_TIMEOUT_SECONDS`, defined here because core's runs package does not import
the catalog. A healthy holder commits within milliseconds; the bound only matters when a claimer
is frozen between the lock and its commit."""

CAP_LOCK_KEY = 0x6C6B6331  # "lkc1"; the second key is 32 bits of the principal's id
"""The first of the two keys of the cap's advisory lock: a key space apart from other locks."""


def is_pipeline(run: type[QueryRun] | AliasedClass[QueryRun] = QueryRun) -> ColumnElement[bool]:
    """Build "this query run is pipeline work": it belongs to a task run, or is maintenance.

    Parameters
    ----------
    run
        `QueryRun` or an alias of it.

    Returns
    -------
    ColumnElement[bool]
        The condition; never `NULL`, whatever the row holds.
    """
    return or_(
        run.task_run_id.is_not(None),
        and_(run.write_mode.is_not(None), run.write_mode == WriteMode.MAINTAIN),
    )


def is_interactive(run: type[QueryRun] | AliasedClass[QueryRun] = QueryRun) -> ColumnElement[bool]:
    """Build "this query run is interactive work": every query run that is not pipeline work.

    Written out and not as ``~is_pipeline()``. Both conditions test `IS NULL` first, so neither
    is ever `NULL` and they are complements; spelling both out keeps that from depending on the
    other's guard. A pipeline query whose task run was deleted (`task_run_id` is `ON DELETE SET
    NULL`) is interactive afterwards, and then counts for the cap.

    Parameters
    ----------
    run
        `QueryRun` or an alias of it.

    Returns
    -------
    ColumnElement[bool]
        The condition; never `NULL`, and the complement of `is_pipeline`.
    """
    return and_(
        run.task_run_id.is_(None),
        or_(run.write_mode.is_(None), run.write_mode != WriteMode.MAINTAIN),
    )


def kind_of(task_run_id: uuid.UUID | None, write_mode: WriteMode | None) -> str:
    """Say the kind of a loaded query run: the row form of the two conditions above.

    Parameters
    ----------
    task_run_id
        The run's `task_run_id`.
    write_mode
        The run's `write_mode`.

    Returns
    -------
    str
        `PIPELINE` or `INTERACTIVE`.
    """
    return PIPELINE if task_run_id is not None or write_mode is WriteMode.MAINTAIN else INTERACTIVE


def takes_kind(takes: str, kind: str) -> bool:
    """Say whether a worker with this `takes` takes work of this kind.

    Parameters
    ----------
    takes
        The worker's `takes`: `ALL`, `INTERACTIVE` or `PIPELINE`.
    kind
        `INTERACTIVE` or `PIPELINE`.

    Returns
    -------
    bool
        `True` for `ALL`, and for the worker whose `takes` is the kind.
    """
    return takes in (ALL, kind)


def cap_for(setting: int | Literal["auto"], interactive_workers: int) -> int:
    """Resolve the cap on one principal's running ad-hoc queries.

    Parameters
    ----------
    setting
        `LAKANA_QUERY_LIMITS__RUNNING_PER_PRINCIPAL`: a whole number of 1 or more, or `auto`.
    interactive_workers
        How many alive workers take interactive work (`interactive_workers`).

    Returns
    -------
    int
        The fixed number; for `auto` every such worker while there are one or two, then all
        but one, and 1 when there is none. At least 1.
    """
    if isinstance(setting, int):
        return setting
    return max(1, interactive_workers if interactive_workers <= 2 else interactive_workers - 1)


def interactive_workers(takes: Iterable[str]) -> int:
    """Count the workers that take interactive work.

    Parameters
    ----------
    takes
        The `takes` of each alive worker.

    Returns
    -------
    int
        How many are `all` or `interactive`.
    """
    return sum(1 for value in takes if takes_kind(value, INTERACTIVE))


def _running(run: type[QueryRun] | AliasedClass[QueryRun]) -> tuple[ColumnElement[bool], ...]:
    """Build what the cap counts: `RUNNING`, interactive, and with a principal.

    Parameters
    ----------
    run
        `QueryRun` or an alias of it.

    Returns
    -------
    tuple[ColumnElement[bool], ...]
        The conditions, to be joined by `AND`.
    """
    return (
        run.status == QueryRunStatus.RUNNING,
        run.principal_id.is_not(None),
        is_interactive(run),
    )


def running_interactive(principal_id: uuid.UUID) -> ScalarSelect[int]:
    """Build the count of a principal's `RUNNING` interactive query runs, in every environment.

    The count is over an alias of `query_runs`, so that it can stand in the `WHERE` of an
    `UPDATE` of `query_runs`. A run whose worker died still counts until the sweep ends it
    (about a lease, one minute): the cap accepts that.

    Parameters
    ----------
    principal_id
        The principal.

    Returns
    -------
    ScalarSelect[int]
        A scalar subquery.
    """
    other = aliased(QueryRun)
    return (
        select(func.count())
        .select_from(other)
        .where(other.principal_id == principal_id, *_running(other))
        .scalar_subquery()
    )


def running_count(session: Session, principal_id: uuid.UUID) -> int:
    """Read how many interactive query runs a principal has `RUNNING`.

    Parameters
    ----------
    session
        Database session.
    principal_id
        The principal.

    Returns
    -------
    int
        The count, over every environment.
    """
    return int(session.execute(select(running_interactive(principal_id))).scalar_one())


def running_and_passed_by(
    session: Session, principal_id: uuid.UUID, now: datetime
) -> tuple[int, datetime | None]:
    """Read a principal's running ad-hoc queries and the time it is passed by until: one statement.

    Parameters
    ----------
    session
        Database session.
    principal_id
        The principal.
    now
        The database's current time.

    Returns
    -------
    tuple
        The count (`running_count`) and `Principal.query_passed_by_until`, or `None` when none
        is set or it is not after `now`.
    """
    until = (
        select(Principal.query_passed_by_until)
        .where(Principal.id == principal_id)
        .scalar_subquery()
    )
    count, stored = session.execute(select(running_interactive(principal_id), until)).one()
    return int(count), stored if stored is not None and stored > now else None


def passed_by_until(
    session: Session, principal_ids: Iterable[uuid.UUID], now: datetime
) -> dict[uuid.UUID, datetime]:
    """Read which of some principals are passed by now, and until when: one statement.

    Parameters
    ----------
    session
        Database session.
    principal_ids
        The principals to ask about.
    now
        The database's current time.

    Returns
    -------
    dict
        The time per principal whose `query_passed_by_until` is after `now`; empty, with no
        statement, for no principal.
    """
    ids = list(principal_ids)
    if not ids:
        return {}
    rows = session.execute(
        select(Principal.id, Principal.query_passed_by_until).where(
            Principal.id.in_(ids),
            Principal.query_passed_by_until.is_not(None),
            Principal.query_passed_by_until > now,
        )
    )
    return {who: until for who, until in rows if until is not None}


def pass_by(session: Session, principal_id: uuid.UUID, seconds: float) -> None:
    """Write the time until which workers pass a principal's ad-hoc queries by, and commit.

    Called by a worker whose claim timed out on the principal's lock, so that the wait reason
    (`waiting.LOCK_TIMEOUT`), which cannot see the worker's memory, can say so. The time is the
    database's clock plus `seconds`. The principal's own `updated_at` is left as it is: this is
    not a change of the principal.

    Parameters
    ----------
    session
        Database session; its transaction must be a fresh one (the claim was rolled back).
    principal_id
        The principal whose claim timed out.
    seconds
        How long the principal is passed by, from now.
    """
    session.execute(
        update(Principal)
        .where(Principal.id == principal_id)
        .values(
            query_passed_by_until=db_now(session) + timedelta(seconds=seconds),
            updated_at=Principal.updated_at,
        )
        .execution_options(synchronize_session=False)
    )
    session.commit()


def running_by_principal(session: Session, at_least: int = 1) -> dict[uuid.UUID, int]:
    """Read the principals that have `at_least` or more interactive query runs `RUNNING`.

    One grouped statement. A run without a principal (a deleted one) is in no group: it is
    never counted and never capped.

    Parameters
    ----------
    session
        Database session.
    at_least
        The smallest count that is listed.

    Returns
    -------
    dict[uuid.UUID, int]
        Each such principal's id to its count.
    """
    rows = session.execute(
        select(QueryRun.principal_id, func.count())
        .where(*_running(QueryRun))
        .group_by(QueryRun.principal_id)
        .having(func.count() >= at_least)
    )
    return {principal_id: int(count) for principal_id, count in rows if principal_id is not None}


def under_cap(principal_id: uuid.UUID, cap: int) -> ColumnElement[bool]:
    """Build "this principal runs fewer interactive queries than the cap", for a `WHERE`.

    Parameters
    ----------
    principal_id
        The principal.
    cap
        The cap (`cap_for`).

    Returns
    -------
    ColumnElement[bool]
        The condition.
    """
    return running_interactive(principal_id) < cap


def on_postgres(session: Session) -> bool:
    """Say whether a session talks to PostgreSQL.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    bool
        `True` on PostgreSQL, `False` on SQLite and any other dialect.
    """
    return session.get_bind().dialect.name == "postgresql"


def is_lock_timeout(error: DBAPIError) -> bool:
    """Say whether a database error is PostgreSQL's ``lock_timeout`` firing (SQLSTATE 55P03).

    Parameters
    ----------
    error
        The error a statement raised.

    Returns
    -------
    bool
        `True` when the statement gave up waiting for a lock.
    """
    return "55P03" in str(error.orig) or "lock timeout" in str(error.orig).lower()


def lock_principal(session: Session, principal_id: uuid.UUID) -> bool:
    """Take the cap's advisory transaction lock for a principal; nothing off Postgres.

    Two claims for one principal's different runs touch no common row, and under READ
    COMMITTED each would count the other's uncommitted start as absent. The lock makes the
    second wait until the first has committed, so that its statement, which starts after the
    lock was granted, counts it. It ends with the transaction, blocks only another claim for
    the same principal (two principals whose ids share 32 bits wait for each other's claim, no
    more), and the two integer casts make Postgres choose the two-key form whatever type the
    driver sends. On SQLite the count and the update are one statement of the one writer.

    The wait is bounded: ``SET LOCAL lock_timeout`` (`CAP_LOCK_TIMEOUT_SECONDS`) is issued right
    before the lock. It lasts until the transaction ends with the claim's commit or rollback and
    touches nothing else. A timeout is not an error: the claim is refused and the run stays
    queued for the next poll.

    Parameters
    ----------
    session
        Database session; the lock is held until its transaction ends.
    principal_id
        The principal whose claims are serialised.

    Returns
    -------
    bool
        `True` when the lock is held (always off Postgres); `False` when the wait timed out. The
        transaction is then aborted: the caller rolls back.

    Raises
    ------
    DBAPIError
        For any failure of the statements other than the lock timeout.
    """
    if not on_postgres(session):
        return True
    key = int.from_bytes(principal_id.bytes[:4], "big", signed=True)
    lock = func.pg_advisory_xact_lock(
        cast(literal(CAP_LOCK_KEY), Integer), cast(literal(key), Integer)
    )
    try:
        session.execute(text(f"SET LOCAL lock_timeout = '{int(CAP_LOCK_TIMEOUT_SECONDS)}s'"))  # nosec B608
        session.execute(select(lock))
    except DBAPIError as error:
        if not is_lock_timeout(error):
            raise
        logger.debug(
            "The claim for principal %s waited %s s for another claim; it will retry.",
            principal_id,
            CAP_LOCK_TIMEOUT_SECONDS,
        )
        return False
    return True
