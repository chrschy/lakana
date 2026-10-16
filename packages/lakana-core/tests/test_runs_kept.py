"""The note on a table that keeps data that failed a `keep` check."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from core_test_helpers import _prepared_catalog, python_task_run
from sqlalchemy import delete, event, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.kept_mark import KeptSeen
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Base,
    CheckOutcome,
    CheckResult,
    Environment,
    Table,
    TaskRun,
    TaskStatus,
)
from lakana_core.models.audit_event import SecurableKind
from lakana_core.projects.checks import CheckSpec
from lakana_core.runs.checks import (
    kept_check_message,
    kept_no_checks_error,
    record_result,
    results_of_task,
)
from lakana_core.runs.kept import (
    KEPT_EARLIER_LIMIT,
    KEPT_SNAPSHOT_UNKNOWN,
    KeptNote,
    clear_kept,
    clear_kept_in_attempt,
    kept_message,
    kept_run_of,
    kept_tables,
    kept_unknown_of,
    note_kept,
    record_clean_in_attempt,
    refused_message,
    restore_kept,
    shown_checks,
)

_TABLE = "silver.orders"


@pytest.fixture
def engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Engine]:
    """Provide a SQLite file engine, so that a second connection sees only what is committed.

    Parameters
    ----------
    tmp_path_factory
        Makes the directory of the database file.

    Yields
    ------
    Engine
        The engine, with the schema created.
    """
    built = create_engine(
        DatabaseConfig(url=f"sqlite:///{tmp_path_factory.mktemp('meta') / 'catalog.db'}")
    )
    Base.metadata.create_all(built)
    yield built
    built.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Provide the session under test (this module's own, on the file engine).

    Parameters
    ----------
    engine
        The file engine.

    Yields
    ------
    Session
        A session.
    """
    with Session(engine) as s:
        yield s


@pytest.fixture
def task(session: Session, tmp_path: Path, principal: PrincipalRef) -> TaskRun:
    """Provide a running task run of ``silver.orders`` (claim 1, worker ``w1``), table created.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root of the ``dev`` environment.
    principal
        The run principal.

    Returns
    -------
    TaskRun
        The running task run.
    """
    _prepared_catalog(
        session,
        tmp_path,
        AllowAllAuthorization(),
        namespaces=[NamespacePath.from_parts(["silver"])],
        tables=[TableRef.from_dotted("dev", _TABLE)],
    )
    return python_task_run(session, principal.id, asset=_TABLE, kind="sql")


def _table(session: Session) -> Table:
    """Read the table row on a second connection, so only committed state is seen.

    Parameters
    ----------
    session
        The session under test; its engine is reused for a new connection.

    Returns
    -------
    Table
        The row as committed.
    """
    with Session(session.get_bind()) as other:
        row = other.scalars(select(Table).where(Table.name == "orders")).one()
        other.expunge(row)
        return row


def _events(session: Session, *types: AuditEventType) -> list[AuditEvent]:
    """Read the audit events of the given types on a second connection, oldest first.

    Parameters
    ----------
    session
        The session under test.
    *types
        The event types.

    Returns
    -------
    list of AuditEvent
        The events as committed.
    """
    with Session(session.get_bind()) as other:
        rows = list(
            other.scalars(
                select(AuditEvent)
                .where(AuditEvent.event_type.in_(types))
                .order_by(AuditEvent.occurred_at, AuditEvent.id)
            )
        )
        other.expunge_all()
        return rows


def _failed_keep_results(session: Session, task: TaskRun, *names: str) -> list[CheckResult]:
    """Record a failed `keep` result per name and read them back, by name.

    Parameters
    ----------
    session
        Database session.
    task
        The task run.
    *names
        The checks' short names.

    Returns
    -------
    list of CheckResult
        The results.
    """
    for name in names:
        assert record_result(
            session,
            task_run_id=task.id,
            worker="w1",
            claim=1,
            spec=CheckSpec(
                name=f"{_TABLE}/{name}",
                table=_TABLE,
                check=name,
                path=f"checks/silver/orders/{name}.sql",
                on_failure="keep",
                description=None,
                sql="SELECT 1",
            ),
            outcome=CheckOutcome.FAILED,
            failing_rows=2,
            error=None,
            table_id=None,
            checked_snapshot_id=7,
            policy_applied=False,
            query_run_id=None,
            started_at=datetime(2026, 10, 12, 12, 0, tzinfo=UTC),
        )
    return results_of_task(session, task.id)


def _note(
    session: Session,
    task: TaskRun,
    kept: list[CheckResult],
    *,
    worker: str = "w1",
    claim: int = 1,
    table_id: uuid.UUID | None = None,
) -> bool:
    """Call `note_kept` for the task's table with the values most tests share.

    Parameters
    ----------
    session
        Database session.
    task
        The task run.
    kept
        The failed results.
    worker
        The worker claiming to hold the task.
    claim
        The claim it claims to hold.
    table_id
        The table; the task's table when `None`.

    Returns
    -------
    bool
        Whether `note_kept` noted the table.
    """
    return _note_result(session, task, kept, worker, claim, table_id) is KeptNote.NOTED


def _note_result(
    session: Session,
    task: TaskRun,
    kept: list[CheckResult],
    worker: str = "w1",
    claim: int = 1,
    table_id: uuid.UUID | None = None,
    unchecked: bool = False,
) -> KeptNote:
    """Call `note_kept` for the task's table and return what it says.

    Parameters
    ----------
    session
        Database session.
    task
        The task run.
    kept
        The failed results.
    worker
        The worker claiming to hold the task.
    claim
        The claim it claims to hold.
    table_id
        The table; the task's table when `None`.
    unchecked
        Whether the checks could not run.

    Returns
    -------
    KeptNote
        What `note_kept` returned.
    """
    return note_kept(
        session,
        task_run_id=task.id,
        worker=worker,
        claim=claim,
        table_id=_table(session).id if table_id is None else table_id,
        kept=kept,
        unchecked=unchecked,
    )


def _clear(
    session: Session,
    task: TaskRun,
    *,
    worker: str = "w1",
    claim: int = 1,
    table_id: uuid.UUID | None = None,
    snapshot_id: int | None = None,
) -> bool:
    """Call `clear_kept` for the task's table with the values most tests share.

    Parameters
    ----------
    session
        Database session.
    task
        The task run.
    worker
        The worker claiming to hold the task.
    claim
        The claim it claims to hold.
    table_id
        The table; the task's table when `None`.
    snapshot_id
        The snapshot the passing checks judged.

    Returns
    -------
    bool
        What `clear_kept` returned.
    """
    return clear_kept(
        session,
        task_run_id=task.id,
        worker=worker,
        claim=claim,
        table_id=_table(session).id if table_id is None else table_id,
        why="checks_passed",
        snapshot_id=snapshot_id,
        seen=_seen(_table(session)),
    )


def test_note_kept_sets_the_note_and_audits_it(session: Session, task: TaskRun) -> None:
    kept = _failed_keep_results(session, task, "b", "a")

    assert _note(session, task, kept)

    row = _table(session)
    assert (row.kept_run_id, row.kept_task_run_id) == (task.run_id, task.id)
    assert row.kept_check == "silver.orders/a and 1 more"
    (event,) = _events(session, AuditEventType.TABLE_KEPT)
    assert event.detail == {
        "checks": ["silver.orders/a", "silver.orders/b"],
        "run_id": str(task.run_id),
        "task_run_id": str(task.id),
    }
    assert (event.securable_kind, event.securable_table) == (SecurableKind.TABLE, "orders")


@pytest.mark.parametrize("case", ["other-worker", "other-claim", "not-running"])
def test_note_kept_writes_nothing_for_a_claim_that_is_not_held(
    session: Session, task: TaskRun, case: str
) -> None:
    kept = _failed_keep_results(session, task, "a")
    worker, claim = "w1", 1
    if case == "other-worker":
        worker = "w2"
    elif case == "other-claim":
        claim = 2
    else:
        task.status = TaskStatus.PENDING
        session.commit()

    assert _note(session, task, kept, worker=worker, claim=claim) is False

    assert _table(session).kept_run_id is None
    assert _events(session, AuditEventType.TABLE_KEPT) == []


def test_note_kept_has_nothing_to_note_on_a_dropped_table(session: Session, task: TaskRun) -> None:
    kept = _failed_keep_results(session, task, "a")
    table_id = _table(session).id
    session.execute(delete(Table).where(Table.id == table_id))
    session.commit()

    assert _note_result(session, task, kept, table_id=table_id) is KeptNote.GONE

    assert _events(session, AuditEventType.TABLE_KEPT) == []


def test_the_same_check_phase_notes_a_table_once(session: Session, task: TaskRun) -> None:
    kept = _failed_keep_results(session, task, "a")

    assert _note(session, task, kept) is True
    assert _note(session, task, kept) is True  # a reclaim repeats the call

    (event,) = _events(session, AuditEventType.TABLE_KEPT)
    assert event.detail["task_run_id"] == str(task.id)
    assert _table(session).kept_check == "silver.orders/a"


def test_a_different_task_run_with_the_same_check_text_still_writes_a_note(
    session: Session, task: TaskRun, principal: PrincipalRef
) -> None:
    """Only the same task run's repeat is left alone: the text alone never decides."""
    assert _note(session, task, _failed_keep_results(session, task, "a"))
    later = python_task_run(session, principal.id, asset=_TABLE, kind="sql")
    assert later.id != task.id

    assert _note(session, later, _failed_keep_results(session, later, "a"))

    row = _table(session)
    assert (row.kept_run_id, row.kept_task_run_id, row.kept_check) == (
        later.run_id,
        later.id,
        "silver.orders/a",
    )
    assert len(_events(session, AuditEventType.TABLE_KEPT)) == 2


def test_another_verdict_of_the_same_task_run_is_noted_again(
    session: Session, task: TaskRun
) -> None:
    assert _note(session, task, _failed_keep_results(session, task, "a"))

    assert _note(session, task, _failed_keep_results(session, task, "a", "b"))

    assert _table(session).kept_check == "silver.orders/a and 1 more"
    assert len(_events(session, AuditEventType.TABLE_KEPT)) == 2


def test_clear_kept_clears_and_audits_once(session: Session, task: TaskRun) -> None:
    assert _note(session, task, _failed_keep_results(session, task, "a"))

    assert _clear(session, task) is True
    assert _clear(session, task) is False  # no row changed, no second event

    row = _table(session)
    assert (row.kept_run_id, row.kept_task_run_id, row.kept_check) == (None, None, None)
    (event,) = _events(session, AuditEventType.TABLE_KEPT_CLEARED)
    assert event.detail == {
        "why": "checks_passed",
        "run_id": str(task.run_id),
        "task_run_id": str(task.id),
    }


def test_clear_kept_on_a_table_without_a_note_writes_no_event(
    session: Session, task: TaskRun
) -> None:
    assert _clear(session, task) is False

    assert _events(session, AuditEventType.TABLE_KEPT_CLEARED) == []


@pytest.mark.parametrize("case", ["other-worker", "other-claim", "not-running", "dropped"])
def test_clear_kept_changes_nothing_when_it_cannot(
    session: Session, task: TaskRun, case: str
) -> None:
    assert _note(session, task, _failed_keep_results(session, task, "a"))
    worker, claim, table_id = "w1", 1, None
    if case == "other-worker":
        worker = "w2"
    elif case == "other-claim":
        claim = 2
    elif case == "not-running":
        task.status = TaskStatus.PENDING
        session.commit()
    else:
        table_id = uuid.uuid4()

    assert _clear(session, task, worker=worker, claim=claim, table_id=table_id) is False

    assert _table(session).kept_run_id == task.run_id
    assert _events(session, AuditEventType.TABLE_KEPT_CLEARED) == []


def test_the_state_message_of_a_kept_table(session: Session, task: TaskRun) -> None:
    assert kept_message(_table(session)) is None  # a table without a note says nothing
    assert _note(session, task, _failed_keep_results(session, task, "unique_id"))
    row = _table(session)

    assert kept_message(row) == (
        f"Kept after a failed check (`silver.orders/unique_id`, run {task.run_id}): the table "
        "holds the data that failed. Tasks do not read it until its checks pass."
    )


def test_the_refusal_of_a_kept_table_names_the_check_the_run_and_the_way_out(
    session: Session, task: TaskRun
) -> None:
    assert _note(session, task, _failed_keep_results(session, task, "unique_id"))

    text = refused_message(_table(session), "silver.orders", "dev")

    assert text == (
        f"`silver.orders` was kept after a failed check (`silver.orders/unique_id`, run "
        f"{task.run_id}) and has not passed its checks since, so this task does not read it. "
        "A run of the project that builds `silver.orders` clears this: build it again there "
        "(`lakana run --select silver.orders --env dev`), or repair the data and run that "
        "project. If no project builds it any more, drop it: "
        "`lakana table delete silver.orders --env dev`."
    )


def test_a_note_of_checks_that_could_not_run_says_no_check_could_run_on_the_data(
    session: Session, task: TaskRun
) -> None:
    """The table holds new data that no check ran on; the state and the refusal say so."""
    results = _failed_keep_results(session, task, "known_key")
    assert _note_result(session, task, results, unchecked=True) is KeptNote.NOTED
    row = _table(session)

    assert row.kept_unchecked is True
    assert kept_message(row) == (
        f"Kept after a check that could not run (`silver.orders/known_key`, run {task.run_id}): "
        "the table holds data no check could run on. Tasks do not read it until its checks pass."
    )
    refused = refused_message(row, "silver.orders", "dev")
    assert refused.startswith(
        "`silver.orders` was kept after a check that could not run (`silver.orders/known_key`, "
        f"run {task.run_id}) and has not passed its checks since, so this task does not read it. "
        "A run of the project that builds `silver.orders` clears this: "
    )


def test_a_note_that_moves_to_a_failed_check_is_no_longer_unchecked(
    session: Session, task: TaskRun
) -> None:
    results = _failed_keep_results(session, task, "known_key")
    assert _note_result(session, task, results, unchecked=True) is KeptNote.NOTED
    other = _failed_keep_results(session, task, "unique_id")[-1:]
    assert _note_result(session, task, other) is KeptNote.NOTED

    row = _table(session)
    assert (row.kept_check, row.kept_unchecked) == ("silver.orders/unique_id", False)


def test_clearing_a_note_clears_its_unchecked_mark(session: Session, task: TaskRun) -> None:
    results = _failed_keep_results(session, task, "known_key")
    assert _note_result(session, task, results, unchecked=True) is KeptNote.NOTED
    assert _clear(session, task) is True

    assert _table(session).kept_unchecked is False


def test_the_messages_of_a_kept_table_escape_the_names_they_are_given(
    session: Session, task: TaskRun
) -> None:
    assert _note(session, task, _failed_keep_results(session, task, "a"))

    text = refused_message(_table(session), "silver.caf\u00e9", "d\n")

    assert "`silver.caf\\xe9`" in text
    assert "--env d\\n`" in text


def test_the_shown_checks_name_the_first_and_count_the_rest(
    session: Session, task: TaskRun
) -> None:
    results = _failed_keep_results(session, task, "c", "a", "b")

    assert shown_checks(results[:1]) == "silver.orders/a"
    assert shown_checks(results) == "silver.orders/a and 2 more"


def _environment_id(session: Session) -> uuid.UUID:
    """Read the id of the ``dev`` environment.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    uuid.UUID
        The id.
    """
    return session.scalars(select(Environment.id).where(Environment.name == "dev")).one()


def test_kept_tables_names_each_table_with_a_note_by_its_run(
    session: Session, task: TaskRun
) -> None:
    environment = _environment_id(session)
    assert kept_tables(session, environment) == {}
    assert _note(session, task, _failed_keep_results(session, task, "a"))

    assert kept_tables(session, environment) == {_TABLE: task.run_id}
    assert kept_tables(session, uuid.uuid4()) == {}  # another environment has none


def test_kept_tables_is_one_query(session: Session, task: TaskRun) -> None:
    assert _note(session, task, _failed_keep_results(session, task, "a"))
    environment = _environment_id(session)
    seen: list[str] = []

    def record(conn: object, cursor: object, statement: str, *args: object) -> None:
        seen.append(statement)

    bind = session.get_bind()
    event.listen(bind, "before_cursor_execute", record)
    try:
        kept_tables(session, environment)
    finally:
        event.remove(bind, "before_cursor_execute", record)

    assert len(seen) == 1


def test_kept_run_of_names_the_run_of_the_note_and_none_without_one(
    session: Session, task: TaskRun
) -> None:
    table_id = _table(session).id
    assert kept_run_of(session, table_id) is None
    assert _note(session, task, _failed_keep_results(session, task, "a"))

    assert kept_run_of(session, table_id) == task.run_id
    assert kept_run_of(session, uuid.uuid4()) is None  # a dropped table


def _later_task(
    session: Session, principal: PrincipalRef, before: int | None = None, after: int | None = None
) -> TaskRun:
    """Insert a running task run of the table in a new run, stamped with the write it made.

    Parameters
    ----------
    session
        Database session; commits.
    principal
        The run principal.
    before
        The snapshot the table had before the task run's write.
    after
        The snapshot the write made.

    Returns
    -------
    TaskRun
        The task run, of a run no earlier task belongs to.
    """
    later = python_task_run(session, principal.id, asset=_TABLE, kind="sql")
    return _stamped(session, later, before, after)


def _stamped(session: Session, task: TaskRun, before: int | None, after: int | None) -> TaskRun:
    """Stamp a task run with the snapshots of its write, as the write's commit does.

    Parameters
    ----------
    session
        Database session; commits.
    task
        The task run.
    before
        The snapshot before the write.
    after
        The snapshot the write made.

    Returns
    -------
    TaskRun
        The same task run.
    """
    session.execute(
        update(TaskRun)
        .where(TaskRun.id == task.id)
        .values(written_snapshot_before=before, written_snapshot_after=after)
    )
    session.commit()
    return task


def _restore(session: Session, task: TaskRun, before: int | None) -> dict[str, object] | None:
    """Call `restore_kept` for the table in its own transaction and commit it.

    Parameters
    ----------
    session
        Database session; commits.
    task
        The task run whose data is set back.
    before
        The snapshot the table is set back to.

    Returns
    -------
    dict or None
        What `restore_kept` returned.
    """
    after = session.scalar(select(TaskRun.written_snapshot_after).where(TaskRun.id == task.id))
    found = restore_kept(
        session,
        _table(session).id,
        run_id=task.run_id,
        task_run_id=task.id,
        snapshot_before=before,
        snapshot_after=after,
    )
    session.commit()
    return found


def _entry(task: TaskRun, check: str, snapshot: int | None, *, unchecked: bool = False) -> dict:
    """Build the entry that stands for a task run's mark.

    Parameters
    ----------
    task
        The task run that set the mark.
    check
        The mark's check text.
    snapshot
        The snapshot the mark was a verdict on.
    unchecked
        Whether the mark names a check that could not run.

    Returns
    -------
    dict
        The entry.
    """
    return {
        "run_id": str(task.run_id),
        "task_run_id": str(task.id),
        "check": check,
        "unchecked": unchecked,
        "snapshot_id": snapshot,
    }


def test_the_first_note_stacks_the_unmarked_state_of_the_snapshot_before(
    session: Session, task: TaskRun
) -> None:
    _stamped(session, task, 10, 11)
    assert _table(session).kept_earlier == []

    assert _note(session, task, _failed_keep_results(session, task, "a"))

    row = _table(session)
    assert row.kept_earlier == [{"snapshot_id": 10}]  # no mark, about the data before the write
    assert row.kept_snapshot_id == 11  # the mark is a verdict on the run's own write


def test_a_note_that_moves_to_a_new_run_stacks_the_one_it_replaces(
    session: Session, task: TaskRun, principal: PrincipalRef
) -> None:
    _stamped(session, task, 10, 11)
    assert _note(session, task, _failed_keep_results(session, task, "a"))
    second = _later_task(session, principal, 11, 12)

    assert _note(session, second, _failed_keep_results(session, second, "b"))

    row = _table(session)
    assert (row.kept_run_id, row.kept_check, row.kept_snapshot_id) == (
        second.run_id,
        "silver.orders/b",
        12,
    )
    assert row.kept_earlier == [
        {"snapshot_id": 10},
        _entry(task, "silver.orders/a", 11),
    ]


def test_another_verdict_of_the_same_task_stacks_nothing_more(
    session: Session, task: TaskRun
) -> None:
    _stamped(session, task, 10, 11)
    assert _note(session, task, _failed_keep_results(session, task, "a"))

    assert _note(session, task, _failed_keep_results(session, task, "a", "b"))

    assert _table(session).kept_earlier == [{"snapshot_id": 10}]


def test_a_repeated_call_stacks_nothing(session: Session, task: TaskRun) -> None:
    kept = _failed_keep_results(session, task, "a")

    assert _note(session, task, kept)
    assert _note(session, task, kept)

    assert len(_table(session).kept_earlier) == 1


def test_an_unchecked_note_is_restored_with_its_flag(
    session: Session, task: TaskRun, principal: PrincipalRef
) -> None:
    _stamped(session, task, 10, 11)
    assert _note_result(
        session, task, _failed_keep_results(session, task, "a"), unchecked=True
    ) is (KeptNote.NOTED)
    second = _later_task(session, principal, 11, 12)
    assert _note_result(session, second, _failed_keep_results(session, second, "b")) is (
        KeptNote.NOTED
    )

    assert _restore(session, second, 11) == {
        "kept_restored": _entry(task, "silver.orders/a", 11, unchecked=True)
    }

    row = _table(session)
    assert (row.kept_run_id, row.kept_task_run_id, row.kept_unchecked, row.kept_snapshot_id) == (
        task.run_id,
        task.id,
        True,
        11,
    )
    assert "could run on" in (kept_message(row) or "")
    assert "could not run" in refused_message(row, _TABLE, "dev")


def test_a_note_moved_by_an_unchecked_rebuild_stacks_the_mark_it_replaces(
    session: Session, task: TaskRun, principal: PrincipalRef
) -> None:
    _stamped(session, task, 10, 11)
    assert _note(session, task, _failed_keep_results(session, task, "a"))
    second = _later_task(session, principal, 11, 12)

    assert _note_result(
        session, second, _failed_keep_results(session, second, "b"), unchecked=True
    ) is (KeptNote.NOTED)

    row = _table(session)
    assert row.kept_unchecked is True
    assert row.kept_earlier[-1]["unchecked"] is False
    assert _restore(session, second, 11) is not None
    assert _table(session).kept_unchecked is False


def test_clear_kept_pushes_the_mark_it_removes_and_records_the_snapshot_it_judged(
    session: Session, task: TaskRun, principal: PrincipalRef
) -> None:
    _stamped(session, task, 10, 11)
    assert _note(session, task, _failed_keep_results(session, task, "a"))
    second = _later_task(session, principal, 11, 12)
    assert _note(session, second, _failed_keep_results(session, second, "b"))
    assert len(_table(session).kept_earlier) == 2

    assert _clear(session, second, snapshot_id=12) is True

    row = _table(session)
    assert (row.kept_run_id, row.kept_snapshot_id) == (None, 12)
    assert row.kept_earlier == [
        {"snapshot_id": 10},
        _entry(task, "silver.orders/a", 11),
        _entry(second, "silver.orders/b", 12),
    ]


def test_a_clear_that_does_not_know_its_snapshot_records_that_it_does_not(
    session: Session, task: TaskRun
) -> None:
    assert _note(session, task, _failed_keep_results(session, task, "a"))

    assert _clear(session, task) is True

    assert _table(session).kept_snapshot_id == KEPT_SNAPSHOT_UNKNOWN


def test_a_chain_of_three_marks_is_restored_twice_and_then_to_no_mark(
    session: Session, task: TaskRun, principal: PrincipalRef
) -> None:
    _stamped(session, task, 10, 11)
    b = _later_task(session, principal, 11, 12)
    c = _later_task(session, principal, 12, 13)
    assert _note(session, task, _failed_keep_results(session, task, "a"))
    assert _note(session, b, _failed_keep_results(session, b, "b"))
    assert _note(session, c, _failed_keep_results(session, c, "c"))

    assert _restore(session, c, 12) is not None
    row = _table(session)
    assert (row.kept_run_id, row.kept_check, row.kept_snapshot_id) == (
        b.run_id,
        "silver.orders/b",
        12,
    )
    assert _restore(session, b, 11) is not None
    row = _table(session)
    assert (row.kept_run_id, row.kept_check) == (task.run_id, "silver.orders/a")
    assert row.kept_earlier == [{"snapshot_id": 10}]

    # The data of A had no mark before it: setting it back leaves the table with no mark.
    assert _restore(session, task, 10) == {"kept_restored": None}
    row = _table(session)
    assert (row.kept_run_id, row.kept_task_run_id, row.kept_check) == (None, None, None)
    assert (row.kept_unchecked, row.kept_earlier, row.kept_snapshot_id) == (False, [], 10)
    assert kept_message(row) is None


def test_a_set_back_to_another_snapshot_than_any_entry_describes_marks_the_checks_unknown(
    session: Session, task: TaskRun
) -> None:
    _stamped(session, task, 10, 11)
    assert _note(session, task, _failed_keep_results(session, task, "a"))

    assert _restore(session, task, 9) == {"kept_unknown": True}

    row = _table(session)
    assert (row.kept_run_id, row.kept_task_run_id) == (task.run_id, task.id)
    assert (row.kept_unknown, row.kept_check, row.kept_unchecked) == (True, None, False)
    assert (row.kept_snapshot_id, row.kept_earlier) == (9, [{"snapshot_id": 10}])


def test_a_clear_between_the_write_and_its_note_still_restores_the_mark_when_set_back(
    session: Session, task: TaskRun, principal: PrincipalRef
) -> None:
    """The clear stacked the mark it removed, so the data before the write has its verdict."""
    _stamped(session, task, 10, 11)
    assert _note(session, task, _failed_keep_results(session, task, "a"))
    racing = _later_task(session, principal, 11, 12)  # writes 12 over the failed data of 11
    # Another run's check task judged 12 clean and cleared, before racing's note.
    assert _clear(session, task, snapshot_id=12) is True
    assert _note(session, racing, _failed_keep_results(session, racing, "b"))

    assert _restore(session, racing, 11) == {"kept_restored": _entry(task, "silver.orders/a", 11)}

    row = _table(session)
    assert (row.kept_run_id, row.kept_snapshot_id) == (task.run_id, 11)
    assert row.kept_earlier == [{"snapshot_id": 10}]


def test_an_entry_without_a_snapshot_never_restores(session: Session, task: TaskRun) -> None:
    assert _note(session, task, _failed_keep_results(session, task, "a"))  # not stamped
    assert _table(session).kept_earlier == [{"snapshot_id": None}]

    assert _restore(session, task, None) == {"kept_not_restored": True}

    assert _table(session).kept_run_id == task.run_id


def test_the_set_back_of_a_run_the_note_does_not_name_changes_nothing(
    session: Session, task: TaskRun
) -> None:
    _stamped(session, task, 10, 11)
    assert _note(session, task, _failed_keep_results(session, task, "a"))
    before = _table(session)
    other = task.id

    assert (
        restore_kept(
            session,
            before.id,
            run_id=uuid.uuid4(),
            task_run_id=other,
            snapshot_before=10,
            snapshot_after=12,
        )
        is None
    )
    assert (
        restore_kept(
            session,
            before.id,
            run_id=task.run_id,
            task_run_id=uuid.uuid4(),
            snapshot_before=10,
            snapshot_after=12,
        )
        is None
    )  # the same run, another task: not the data the mark names
    session.rollback()

    after = _table(session)
    assert (after.kept_run_id, after.kept_check, after.kept_earlier) == (
        before.kept_run_id,
        before.kept_check,
        before.kept_earlier,
    )


def test_a_table_without_a_note_has_nothing_to_restore(session: Session, task: TaskRun) -> None:
    assert _restore(session, task, 10) is None


def test_a_note_without_stacked_history_becomes_a_note_of_unknown_checks(
    session: Session, task: TaskRun
) -> None:
    """A note made before the stack existed (or whose history ran out) stays a note."""
    _stamped(session, task, 10, 11)
    assert _note(session, task, _failed_keep_results(session, task, "a"))
    with Session(session.get_bind()) as other:
        other.execute(update(Table).where(Table.name == "orders").values(kept_earlier=[]))
        other.commit()

    assert _restore(session, task, 10) == {"kept_unknown": True}

    row = _table(session)
    assert (row.kept_run_id, row.kept_unknown, row.kept_snapshot_id) == (task.run_id, True, 10)


def test_the_stack_keeps_only_the_newest_entries(
    session: Session, task: TaskRun, principal: PrincipalRef
) -> None:
    _stamped(session, task, 0, 1)
    tasks = [task] + [
        _later_task(session, principal, i, i + 1) for i in range(1, KEPT_EARLIER_LIMIT + 3)
    ]
    for index, each in enumerate(tasks):
        assert _note(session, each, _failed_keep_results(session, each, f"c{index}"))

    stack = _table(session).kept_earlier
    assert len(stack) == KEPT_EARLIER_LIMIT
    assert stack[-1]["run_id"] == str(tasks[-2].run_id)  # the newest entry is kept
    assert "run_id" in stack[0]  # the oldest ones went, the unmarked state with them


def test_clear_kept_in_attempt_clears_in_the_callers_transaction(
    session: Session, task: TaskRun
) -> None:
    assert _note(session, task, _failed_keep_results(session, task, "a"))
    task.status = TaskStatus.SUCCEEDED  # the attempt is recorded before the clear runs
    session.commit()
    table_id = _table(session).id

    seen = _seen(_table(session))
    assert clear_kept_in_attempt(
        session,
        task_run_id=task.id,
        table_id=table_id,
        why="checks_passed",
        snapshot_id=7,
        seen=seen,
    )
    session.rollback()  # the caller's transaction did not commit
    assert _table(session).kept_run_id == task.run_id
    assert _events(session, AuditEventType.TABLE_KEPT_CLEARED) == []

    assert clear_kept_in_attempt(
        session,
        task_run_id=task.id,
        table_id=table_id,
        why="checks_passed",
        snapshot_id=7,
        seen=seen,
    )
    session.commit()
    row = _table(session)
    assert (row.kept_run_id, row.kept_snapshot_id) == (None, 7)
    assert row.kept_earlier == [{"snapshot_id": None}, _entry(task, "silver.orders/a", None)]
    (event,) = _events(session, AuditEventType.TABLE_KEPT_CLEARED)
    assert event.detail["why"] == "checks_passed"


def test_clear_kept_in_attempt_has_nothing_to_clear_without_a_note(
    session: Session, task: TaskRun
) -> None:
    assert not clear_kept_in_attempt(
        session,
        task_run_id=task.id,
        table_id=_table(session).id,
        why="checks_passed",
        snapshot_id=None,
        seen=KeptSeen(None, None, None),
    )


def _seen(row: Table) -> KeptSeen:
    """Say what a check phase that read this row at its start has seen of the mark.

    Parameters
    ----------
    row
        The table's row.

    Returns
    -------
    KeptSeen
        The mark's run and task run, and the snapshot of the verdict.
    """
    return KeptSeen(row.kept_run_id, row.kept_task_run_id, row.kept_snapshot_id)


def test_a_clear_does_not_remove_a_mark_another_task_set_after_it_looked(
    session: Session, task: TaskRun, principal: PrincipalRef
) -> None:
    """The check phase judged the mark it saw at its start, not one noted while its counts ran."""
    _stamped(session, task, 10, 11)
    assert _note(session, task, _failed_keep_results(session, task, "a"))
    seen = _seen(_table(session))
    racing = _later_task(session, principal, 11, 12)
    assert _note(session, racing, _failed_keep_results(session, racing, "b"))
    task.status = TaskStatus.SUCCEEDED  # the clearing task's success is being recorded
    session.commit()
    before = _table(session)

    assert not clear_kept_in_attempt(
        session,
        task_run_id=task.id,
        table_id=before.id,
        why="checks_passed",
        snapshot_id=12,
        seen=seen,
    )
    session.commit()

    after = _table(session)
    assert (after.kept_run_id, after.kept_earlier, after.kept_snapshot_id) == (
        racing.run_id,
        before.kept_earlier,
        12,
    )
    assert _events(session, AuditEventType.TABLE_KEPT_CLEARED) == []


def test_a_clean_verdict_refreshes_the_snapshot_of_an_unmarked_table_only(
    session: Session, task: TaskRun
) -> None:
    _stamped(session, task, 10, 11)
    assert _note(session, task, _failed_keep_results(session, task, "a"))
    table_id = _table(session).id

    record_clean_in_attempt(session, table_id, 99)  # a mark stands: nothing to refresh
    session.commit()
    assert _table(session).kept_snapshot_id == 11

    assert _clear(session, task, snapshot_id=12) is True
    record_clean_in_attempt(session, table_id, 13)
    session.commit()
    assert _table(session).kept_snapshot_id == 13


def test_a_set_back_after_a_clear_that_did_not_know_its_snapshot_still_restores_the_mark(
    session: Session, task: TaskRun, principal: PrincipalRef
) -> None:
    _stamped(session, task, 10, 11)
    assert _note(session, task, _failed_keep_results(session, task, "a"))
    assert _clear(session, task) is True  # the snapshot its checks judged was not known
    later = _later_task(session, principal, 11, 12)
    assert _note(session, later, _failed_keep_results(session, later, "b"))
    assert _table(session).kept_earlier[-1] == {"snapshot_id": KEPT_SNAPSHOT_UNKNOWN}

    assert _restore(session, later, 11) == {"kept_restored": _entry(task, "silver.orders/a", 11)}

    assert _table(session).kept_run_id == task.run_id


def test_an_entry_nothing_can_read_makes_the_note_one_of_unknown_checks(
    session: Session, task: TaskRun
) -> None:
    _stamped(session, task, 10, 11)
    assert _note(session, task, _failed_keep_results(session, task, "a"))
    broken = {"run_id": "not-an-id", "check": "x", "unchecked": False, "snapshot_id": 10}
    with Session(session.get_bind()) as other:
        other.execute(update(Table).where(Table.name == "orders").values(kept_earlier=[broken]))
        other.commit()

    assert _restore(session, task, 10) == {"kept_unknown": True}

    row = _table(session)
    assert (row.kept_run_id, row.kept_unknown, row.kept_earlier) == (task.run_id, True, [broken])


@pytest.mark.parametrize("case", ["task-run-gone", "table-dropped"])
def test_clear_kept_in_attempt_clears_nothing_without_its_task_or_table(
    session: Session, task: TaskRun, case: str
) -> None:
    assert _note(session, task, _failed_keep_results(session, task, "a"))
    seen = _seen(_table(session))
    table_id = _table(session).id
    task_run_id = task.id
    if case == "task-run-gone":
        task_run_id = uuid.uuid4()
    else:
        session.execute(delete(Table).where(Table.id == table_id))
        session.commit()

    assert not clear_kept_in_attempt(
        session,
        task_run_id=task_run_id,
        table_id=table_id,
        why="checks_passed",
        snapshot_id=None,
        seen=seen,
    )
    session.rollback()

    assert _events(session, AuditEventType.TABLE_KEPT_CLEARED) == []


def _unmarked_set_back_fixture(
    session: Session, task: TaskRun, principal: PrincipalRef
) -> tuple[TaskRun, TaskRun]:
    """Build the sequence of a set-back after another run's passing check cleared the mark.

    Run A marks snapshot 11 (it wrote it over 10). Run R writes 12 over it. A check of another run
    judges 12 and clears A's mark, so R's data is set back to a table that carries no mark.

    Parameters
    ----------
    session
        Database session.
    task
        The task run of run A.
    principal
        The run principal.

    Returns
    -------
    tuple
        Run A's task run and run R's.
    """
    _stamped(session, task, 10, 11)
    assert _note(session, task, _failed_keep_results(session, task, "a"))
    writer = _later_task(session, principal, 11, 12)
    assert _clear(session, task, snapshot_id=12) is True
    assert _table(session).kept_run_id is None
    return task, writer


def test_a_set_back_after_another_runs_clear_restores_the_mark_of_the_data_it_goes_back_to(
    session: Session, task: TaskRun, principal: PrincipalRef
) -> None:
    marked, writer = _unmarked_set_back_fixture(session, task, principal)

    assert _restore(session, writer, 11) == {"kept_restored": _entry(marked, "silver.orders/a", 11)}

    row = _table(session)
    assert (row.kept_run_id, row.kept_task_run_id, row.kept_check) == (
        marked.run_id,
        marked.id,
        "silver.orders/a",
    )
    assert (row.kept_snapshot_id, row.kept_unknown) == (11, False)
    assert row.kept_earlier == [{"snapshot_id": 10}]


def test_a_set_back_after_another_runs_clear_and_a_full_stack_marks_the_checks_unknown(
    session: Session, task: TaskRun, principal: PrincipalRef
) -> None:
    _, writer = _unmarked_set_back_fixture(session, task, principal)
    table_id = _table(session).id
    for index in range(KEPT_EARLIER_LIMIT + 1):  # clean writes push the mark out of the stack
        record_clean_in_attempt(session, table_id, 100 + index)
    session.commit()
    assert all(entry.get("snapshot_id") != 11 for entry in _table(session).kept_earlier)

    assert _restore(session, writer, 11) == {"kept_unknown": True}

    row = _table(session)
    assert (row.kept_run_id, row.kept_task_run_id) == (writer.run_id, writer.id)
    assert (row.kept_unknown, row.kept_check, row.kept_snapshot_id) == (True, None, 11)


def test_a_set_back_to_a_snapshot_the_current_verdict_describes_changes_nothing(
    session: Session, task: TaskRun, principal: PrincipalRef
) -> None:
    marked, _ = _unmarked_set_back_fixture(session, task, principal)
    writer = _later_task(session, principal, 12, 13)  # judged 12 clean, then wrote 13
    before = _table(session)

    assert _restore(session, writer, 12) is None

    after = _table(session)
    assert (after.kept_run_id, after.kept_snapshot_id, after.kept_earlier) == (
        None,
        12,
        before.kept_earlier,
    )
    assert marked.run_id is not None


def test_a_table_that_never_had_a_verdict_has_nothing_to_restore_or_mark(
    session: Session, task: TaskRun
) -> None:
    assert _table(session).kept_snapshot_id is None

    assert _restore(session, task, 10) is None

    row = _table(session)
    assert (row.kept_run_id, row.kept_unknown, row.kept_earlier) == (None, False, [])


def test_a_clean_table_set_back_to_its_clean_snapshot_stays_clean(
    session: Session, task: TaskRun, principal: PrincipalRef
) -> None:
    """The entry of the clean snapshot is restored: no mark, and no refusal of readers."""
    _stamped(session, task, 10, 11)
    assert _note(session, task, _failed_keep_results(session, task, "a"))
    assert _clear(session, task, snapshot_id=11) is True  # clean, a verdict on 11
    writer = _later_task(session, principal, 11, 12)
    assert _note(session, writer, _failed_keep_results(session, writer, "b"))  # stacks clean 11
    assert _clear(session, writer, snapshot_id=12) is True  # another run judged 12 clean

    assert _restore(session, writer, 11) == {"kept_restored": None}

    row = _table(session)
    assert (row.kept_run_id, row.kept_unknown, row.kept_snapshot_id) == (None, False, 11)
    assert kept_message(row) is None


def test_a_set_back_drops_the_entries_of_data_newer_than_the_one_it_goes_back_to(
    session: Session, task: TaskRun, principal: PrincipalRef
) -> None:
    marked, writer = _unmarked_set_back_fixture(session, task, principal)
    newer = _later_task(session, principal, 12, 13)
    assert _note(session, newer, _failed_keep_results(session, newer, "n"))
    stack = _table(session).kept_earlier
    assert [entry["snapshot_id"] for entry in stack] == [10, 11, 12]

    assert _restore(session, newer, 11) is not None

    assert _table(session).kept_earlier == [{"snapshot_id": 10}]
    assert writer.run_id != marked.run_id


def test_a_newer_verdict_on_a_snapshot_replaces_the_older_entry_of_it(
    session: Session, task: TaskRun, principal: PrincipalRef
) -> None:
    _stamped(session, task, 10, 11)
    assert _note(session, task, _failed_keep_results(session, task, "a"))
    assert _clear(session, task, snapshot_id=11) is True  # stacks the mark of 11
    writer = _later_task(session, principal, 11, 12)

    assert _note(session, writer, _failed_keep_results(session, writer, "b"))  # stacks clean 11

    assert [entry["snapshot_id"] for entry in _table(session).kept_earlier] == [10, 11]
    assert "run_id" not in _table(session).kept_earlier[-1]


def test_a_passing_write_stacks_the_clean_state_it_replaces(
    session: Session, task: TaskRun
) -> None:
    _stamped(session, task, 10, 11)
    assert _note(session, task, _failed_keep_results(session, task, "a"))
    assert _clear(session, task, snapshot_id=11) is True
    table_id = _table(session).id

    record_clean_in_attempt(session, table_id, 12)
    record_clean_in_attempt(session, table_id, 12)  # the same verdict again stacks nothing
    session.commit()

    row = _table(session)
    assert row.kept_snapshot_id == 12
    assert row.kept_earlier == [{"snapshot_id": 10}, {"snapshot_id": 11}]


def test_the_note_of_unknown_checks_refuses_readers_and_says_so(
    session: Session, task: TaskRun
) -> None:
    _stamped(session, task, 10, 11)
    assert _note(session, task, _failed_keep_results(session, task, "a"))
    assert _restore(session, task, 9) == {"kept_unknown": True}
    row = _table(session)

    state = kept_message(row) or ""
    refusal = refused_message(row, _TABLE, "dev")

    assert "checks are not known" in state and str(task.run_id) in state
    assert "checks are not known" in refusal and str(task.run_id) in refusal
    assert "lakana run --select silver.orders --env dev" in refusal
    assert "failed check" not in state and "failed check" not in refusal
    assert kept_tables(session, _environment_id(session)) == {_TABLE: task.run_id}


def test_passing_checks_clear_the_note_of_unknown_checks_and_stack_it(
    session: Session, task: TaskRun
) -> None:
    _stamped(session, task, 10, 11)
    assert _note(session, task, _failed_keep_results(session, task, "a"))
    assert _restore(session, task, 9) == {"kept_unknown": True}

    assert _clear(session, task, snapshot_id=9) is True

    row = _table(session)
    assert (row.kept_run_id, row.kept_unknown, row.kept_snapshot_id) == (None, False, 9)
    assert row.kept_earlier[-1] == {
        **_entry(task, "silver.orders/a", 9),
        "check": None,
        "unknown": True,
    }


def test_a_note_of_unknown_checks_is_restored_with_its_flag(
    session: Session, task: TaskRun, principal: PrincipalRef
) -> None:
    _stamped(session, task, 10, 11)
    assert _note(session, task, _failed_keep_results(session, task, "a"))
    assert _restore(session, task, 9) == {"kept_unknown": True}
    second = _later_task(session, principal, 9, 12)
    assert _note(session, second, _failed_keep_results(session, second, "b"))

    found = _restore(session, second, 9)

    assert found is not None
    restored = found["kept_restored"]
    assert isinstance(restored, dict) and restored["unknown"] is True
    row = _table(session)
    assert (row.kept_run_id, row.kept_unknown, row.kept_check) == (task.run_id, True, None)


def test_a_dropped_table_has_nothing_to_restore(session: Session, task: TaskRun) -> None:
    found = restore_kept(
        session,
        uuid.uuid4(),
        run_id=task.run_id,
        task_run_id=task.id,
        snapshot_before=10,
        snapshot_after=11,
    )
    session.rollback()

    assert found is None


def test_a_set_back_of_the_data_a_foreign_mark_is_a_verdict_on_acts_on_it(
    session: Session, task: TaskRun, principal: PrincipalRef
) -> None:
    """Two writers set back in the reverse order of their writes: the first one's data is S1."""
    _stamped(session, task, 10, 11)
    assert _note(session, task, _failed_keep_results(session, task, "a"))
    first = _later_task(session, principal, 11, 12)
    second = _later_task(session, principal, 12, 13)
    assert _clear(session, task, snapshot_id=13) is True  # another run judged 13 and cleared
    assert _restore(session, second, 12) == {"kept_unknown": True}  # no state is known for 12
    assert _table(session).kept_run_id == second.run_id

    found = _restore(session, first, 11)  # the mark names `second`, a verdict on `first`'s data

    assert found == {"kept_restored": _entry(task, "silver.orders/a", 11)}
    row = _table(session)
    assert (row.kept_run_id, row.kept_unknown, row.kept_snapshot_id) == (task.run_id, False, 11)


def test_a_foreign_mark_that_is_a_verdict_on_other_data_stays(
    session: Session, task: TaskRun, principal: PrincipalRef
) -> None:
    _stamped(session, task, 10, 11)
    assert _note(session, task, _failed_keep_results(session, task, "a"))
    other = _later_task(session, principal, 11, 12)

    assert _restore(session, other, 11) is None  # the mark is a verdict on 11, not on 12

    assert _table(session).kept_run_id == task.run_id


def test_a_set_back_to_the_unknown_snapshot_marker_marks_the_checks_unknown(
    session: Session, task: TaskRun
) -> None:
    """A placeholder is no snapshot: an unmarked table judged at -1 must not match it."""
    _stamped(session, task, 10, 11)
    assert _note(session, task, _failed_keep_results(session, task, "a"))
    assert _clear(session, task) is True  # the judged snapshot was not known: -1
    assert _table(session).kept_snapshot_id == KEPT_SNAPSHOT_UNKNOWN

    assert _restore(session, task, KEPT_SNAPSHOT_UNKNOWN) == {"kept_unknown": True}

    assert _table(session).kept_unknown is True


def test_a_set_back_without_an_entry_drops_the_entries_of_the_data_it_removed(
    session: Session, task: TaskRun, principal: PrincipalRef
) -> None:
    marked, writer = _unmarked_set_back_fixture(session, task, principal)  # v=12, [10, A@11]
    table_id = _table(session).id
    record_clean_in_attempt(session, table_id, 13)  # stacks the clean state of 12
    session.commit()
    assert [entry["snapshot_id"] for entry in _table(session).kept_earlier] == [10, 11, 12]

    assert _restore(session, writer, 9) == {"kept_unknown": True}  # no entry for 9

    # The writer's own data (12) is gone: no set-back can match its entry, so it frees its slot.
    assert [entry["snapshot_id"] for entry in _table(session).kept_earlier] == [10, 11]
    assert marked.run_id != writer.run_id


def test_the_texts_of_a_note_of_unknown_checks_name_no_failed_check(
    session: Session, task: TaskRun
) -> None:
    results = _failed_keep_results(session, task, "a")
    again = kept_check_message(
        results,
        table=_TABLE,
        run_id=uuid.uuid4(),
        environment="dev",
        kept_run_id=task.run_id,
        without_checks=False,
        unknown=True,
    )
    none_left = kept_no_checks_error(
        table=_TABLE, kept_run_id=task.run_id, environment="dev", unknown=True
    )

    for text in (again, none_left):
        assert f"set back to data whose checks are not known (run {task.run_id} was set back)" in (
            text
        )
        assert "kept after a failed check" not in text
        assert "lakana run --select silver.orders --env dev" in text
    assert (
        kept_check_message(
            results,
            table=_TABLE,
            run_id=uuid.uuid4(),
            environment="dev",
            kept_run_id=task.run_id,
            without_checks=True,
            unknown=True,
        )
        == none_left
    )


def test_kept_unknown_of_says_whether_the_note_is_one_of_unknown_checks(
    session: Session, task: TaskRun
) -> None:
    table_id = _table(session).id
    assert kept_unknown_of(session, table_id) is False
    _stamped(session, task, 10, 11)
    assert _note(session, task, _failed_keep_results(session, task, "a"))
    assert kept_unknown_of(session, table_id) is False
    assert _restore(session, task, 9) == {"kept_unknown": True}

    assert kept_unknown_of(session, table_id) is True
    assert kept_unknown_of(session, uuid.uuid4()) is False


def test_a_clean_record_never_overrides_a_recorded_failing_verdict_on_its_snapshot(
    session: Session, task: TaskRun, principal: PrincipalRef
) -> None:
    """A phase that began before the failing verdict cannot show that its own is the newer one."""
    _stamped(session, task, 10, 11)
    assert _note(session, task, _failed_keep_results(session, task, "a"))
    marked = _later_task(session, principal, 11, 12)
    assert _note(session, marked, _failed_keep_results(session, marked, "b"))  # fails on 12
    writer = _later_task(session, principal, 12, 13)
    assert _clear(session, marked, snapshot_id=13) is True  # another run judged 13 and cleared
    before = _table(session)

    record_clean_in_attempt(session, before.id, 12)  # a count of 12 that passed, judged earlier
    session.commit()

    after = _table(session)
    assert (after.kept_snapshot_id, after.kept_earlier) == (13, before.kept_earlier)
    assert _restore(session, writer, 12) == {"kept_restored": _entry(marked, "silver.orders/b", 12)}
    assert _table(session).kept_run_id == marked.run_id
