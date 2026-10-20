"""Tests for `SignInAuditLimiter`, the per-credential window on `AUTHENTICATION_SUCCEEDED` rows."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.errors import AuthenticationError
from lakana_core.identity.pats import PatService
from lakana_core.identity.task_tokens import TaskTokenService
from lakana_core.models import AuditEvent, AuditEventType, Principal, PrincipalType
from lakana_server.app import create_app
from lakana_server.deps import get_principal
from lakana_server.settings import ServerAuditConfig, Settings
from lakana_server.sign_in_audit import SignInAuditLimiter
from tests.conftest import fake_request

ALICE = uuid.uuid4()
BOB = uuid.uuid4()
WINDOW = 15 * 60.0


class _FakeClock:
    """A clock the test moves by hand.

    Parameters
    ----------
    start
        The time `__call__` returns before any `advance` call.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self._now = start

    def advance(self, seconds: float) -> None:
        """Move the clock forward.

        Parameters
        ----------
        seconds
            How many seconds to add.
        """
        self._now += seconds

    def __call__(self) -> float:
        """Return the current fake time.

        Returns
        -------
        float
            The time set so far.
        """
        return self._now


def test_polling_within_the_window_is_admitted_once_and_counted_after_it() -> None:
    clock = _FakeClock()
    limiter = SignInAuditLimiter(WINDOW, clock=clock)

    assert limiter.admit(("pat", "k1"), ALICE) == (True, 0)
    for _ in range(299):
        clock.advance(3.0)
        assert limiter.admit(("pat", "k1"), ALICE) == (False, 0)
    clock.advance(3.0)  # 900 s since the recorded one: the window is over.
    assert limiter.admit(("pat", "k1"), ALICE) == (True, 299)
    clock.advance(3.0)
    assert limiter.admit(("pat", "k1"), ALICE) == (False, 0)


def test_the_window_runs_from_the_recorded_row_not_from_the_last_request() -> None:
    clock = _FakeClock()
    limiter = SignInAuditLimiter(WINDOW, clock=clock)

    assert limiter.admit(("pat", "k1"), ALICE) == (True, 0)
    clock.advance(WINDOW - 1)
    assert limiter.admit(("pat", "k1"), ALICE) == (False, 0)
    clock.advance(1)
    assert limiter.admit(("pat", "k1"), ALICE) == (True, 1)


def test_each_credential_has_its_own_window() -> None:
    limiter = SignInAuditLimiter(WINDOW, clock=_FakeClock())

    assert limiter.admit(("pat", "k1"), ALICE) == (True, 0)
    assert limiter.admit(("pat", "k2"), ALICE) == (True, 0)
    assert limiter.admit(("task", "k1"), ALICE) == (True, 0)
    assert limiter.admit(("pat", "k1"), ALICE) == (False, 0)
    assert limiter.admit(("pat", "k2"), ALICE) == (False, 0)
    assert limiter.admit(("task", "k1"), ALICE) == (False, 0)


def test_the_same_key_id_for_another_principal_is_recorded() -> None:
    limiter = SignInAuditLimiter(WINDOW, clock=_FakeClock())

    assert limiter.admit(("pat", "k1"), ALICE) == (True, 0)
    assert limiter.admit(("pat", "k1"), ALICE) == (False, 0)
    assert limiter.admit(("pat", "k1"), BOB) == (True, 0)
    assert limiter.admit(("pat", "k1"), BOB) == (False, 0)
    assert limiter.admit(("pat", "k1"), ALICE) == (True, 0)


def test_window_zero_admits_every_success_and_keeps_nothing() -> None:
    limiter = SignInAuditLimiter(0, clock=_FakeClock())

    for _ in range(5):
        assert limiter.admit(("pat", "k1"), ALICE) == (True, 0)
    assert limiter.size() == 0


def test_a_failure_with_the_credential_makes_its_next_success_recorded() -> None:
    clock = _FakeClock()
    limiter = SignInAuditLimiter(WINDOW, clock=clock)

    assert limiter.admit(("pat", "k1"), ALICE) == (True, 0)
    assert limiter.admit(("pat", "k1"), ALICE) == (False, 0)
    limiter.forget(("pat", "k1"))
    assert limiter.admit(("pat", "k1"), ALICE) == (True, 1)
    assert limiter.admit(("pat", "k1"), ALICE) == (False, 0)


def test_forgetting_an_unknown_credential_keeps_no_state() -> None:
    limiter = SignInAuditLimiter(WINDOW, clock=_FakeClock())

    limiter.forget(("pat", "never-seen"))

    assert limiter.size() == 0


def test_a_row_that_could_not_be_written_is_due_again_and_counted() -> None:
    limiter = SignInAuditLimiter(WINDOW, clock=_FakeClock())

    assert limiter.admit(("pat", "k1"), ALICE) == (True, 0)
    assert limiter.admit(("pat", "k1"), ALICE) == (False, 0)
    limiter.forget(("pat", "k1"))
    admitted, suppressed = limiter.admit(("pat", "k1"), ALICE)
    assert (admitted, suppressed) == (True, 1)

    limiter.not_recorded(("pat", "k1"), suppressed)

    # The one suppressed before and the success whose row failed.
    assert limiter.admit(("pat", "k1"), ALICE) == (True, 2)


def test_not_recorded_for_an_evicted_credential_keeps_no_state() -> None:
    limiter = SignInAuditLimiter(WINDOW, clock=_FakeClock(), max_credentials=1)

    assert limiter.admit(("pat", "k1"), ALICE) == (True, 0)
    assert limiter.admit(("pat", "k2"), ALICE) == (True, 0)
    limiter.not_recorded(("pat", "k1"), 3)

    assert limiter.size() == 1
    assert limiter.admit(("pat", "k1"), ALICE) == (True, 0)


def test_the_credentials_kept_are_bounded_and_an_evicted_one_records_again() -> None:
    limiter = SignInAuditLimiter(WINDOW, clock=_FakeClock(), max_credentials=2)

    assert limiter.admit(("pat", "k1"), ALICE) == (True, 0)
    assert limiter.admit(("pat", "k2"), ALICE) == (True, 0)
    assert limiter.admit(("pat", "k1"), ALICE) == (False, 0)  # k1 is now the most recent
    assert limiter.admit(("pat", "k3"), ALICE) == (True, 0)  # evicts k2, the least recent

    assert limiter.size() == 2
    assert limiter.admit(("pat", "k1"), ALICE) == (False, 0)
    assert limiter.admit(("pat", "k2"), ALICE) == (True, 0)


# `get_principal` with the limiter: what reaches the audit log.


def _admin(session: Session) -> PrincipalRef:
    """Persist an active user and return its reference.

    Parameters
    ----------
    session
        The test database session.

    Returns
    -------
    PrincipalRef
        The new principal.
    """
    row = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="admin", is_active=True)
    session.add(row)
    session.commit()
    return PrincipalRef(id=row.id, name=row.name, type=row.type)


def _services(session: Session) -> tuple[PatService, TaskTokenService]:
    """Build the two verifying services `get_principal` takes.

    Parameters
    ----------
    session
        The test database session.

    Returns
    -------
    tuple of (PatService, TaskTokenService)
        Both with allow-all authorization.
    """
    return PatService(session, AllowAllAuthorization()), TaskTokenService(
        session, AllowAllAuthorization()
    )


def _sign_in(session: Session, token: str, limiter: SignInAuditLimiter) -> PrincipalRef:
    """Authenticate one request with `token`.

    Parameters
    ----------
    session
        The test database session.
    token
        The full token to present.
    limiter
        The sign-in limiter of the fake app.

    Returns
    -------
    PrincipalRef
        The authenticated principal.
    """
    pats, task_tokens = _services(session)
    return get_principal(
        fake_request(sign_in_limiter=limiter), f"Bearer {token}", session, pats, task_tokens
    )


def _succeeded(session: Session) -> list[AuditEvent]:
    """List the `AUTHENTICATION_SUCCEEDED` rows, oldest first.

    Parameters
    ----------
    session
        The test database session.

    Returns
    -------
    list of AuditEvent
        The rows.
    """
    return list(
        session.execute(
            select(AuditEvent)
            .where(AuditEvent.event_type == AuditEventType.AUTHENTICATION_SUCCEEDED)
            .order_by(AuditEvent.occurred_at)
        ).scalars()
    )


def test_polling_writes_one_row_per_window_with_the_suppressed_count(session: Session) -> None:
    admin = _admin(session)
    issued = _services(session)[0].issue(admin, "admin", name="ui")
    clock = _FakeClock()
    limiter = SignInAuditLimiter(WINDOW, clock=clock)

    for _ in range(10):
        assert _sign_in(session, issued.token, limiter).id == admin.id
        clock.advance(3.0)
    rows = _succeeded(session)
    assert len(rows) == 1
    assert rows[0].detail == {"key_id": issued.key_id}

    clock.advance(WINDOW)
    _sign_in(session, issued.token, limiter)
    rows = _succeeded(session)
    assert len(rows) == 2
    assert rows[1].detail == {"key_id": issued.key_id, "suppressed": 9}


def test_two_credentials_of_one_principal_are_recorded_separately(session: Session) -> None:
    admin = _admin(session)
    pats = _services(session)[0]
    laptop = pats.issue(admin, "admin", name="laptop")
    ci = pats.issue(admin, "admin", name="ci")
    limiter = SignInAuditLimiter(WINDOW, clock=_FakeClock())

    for token in (laptop.token, ci.token, laptop.token, ci.token):
        _sign_in(session, token, limiter)

    assert [row.detail["key_id"] for row in _succeeded(session)] == [laptop.key_id, ci.key_id]


def test_a_task_token_is_recorded_once_per_window(session: Session) -> None:
    admin = _admin(session)
    minted = _services(session)[1].mint(
        admin, "admin", environment="dev", asset_scope=["bronze.*"], ttl_seconds=3600
    )
    limiter = SignInAuditLimiter(WINDOW, clock=_FakeClock())

    for _ in range(3):
        assert _sign_in(session, minted.token, limiter).id == admin.id

    rows = _succeeded(session)
    assert len(rows) == 1
    assert rows[0].detail == {"key_id": minted.key_id}


def test_window_zero_records_every_success(session: Session) -> None:
    admin = _admin(session)
    issued = _services(session)[0].issue(admin, "admin", name="ui")
    limiter = SignInAuditLimiter(0, clock=_FakeClock())

    for _ in range(3):
        _sign_in(session, issued.token, limiter)

    assert [row.detail for row in _succeeded(session)] == [{"key_id": issued.key_id}] * 3


def test_a_revoked_and_recreated_token_is_recorded_at_its_first_use(session: Session) -> None:
    admin = _admin(session)
    pats = _services(session)[0]
    old = pats.issue(admin, "admin", name="ui")
    limiter = SignInAuditLimiter(WINDOW, clock=_FakeClock())
    _sign_in(session, old.token, limiter)
    pats.revoke(admin, "admin", old.key_id)

    with pytest.raises(AuthenticationError, match="revoked"):
        _sign_in(session, old.token, limiter)
    new = pats.issue(admin, "admin", name="ui")
    _sign_in(session, new.token, limiter)

    assert [row.detail["key_id"] for row in _succeeded(session)] == [old.key_id, new.key_id]
    failed = session.execute(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.AUTHENTICATION_FAILED)
    ).scalars()
    assert [row.detail for row in failed] == [{"key_id": old.key_id}]


def test_a_success_after_a_failure_with_the_same_credential_is_recorded(
    session: Session,
) -> None:
    admin = _admin(session)
    issued = _services(session)[0].issue(admin, "admin", name="ui")
    limiter = SignInAuditLimiter(WINDOW, clock=_FakeClock())
    _sign_in(session, issued.token, limiter)
    _sign_in(session, issued.token, limiter)

    with pytest.raises(AuthenticationError):
        _sign_in(session, f"lakana_pat_{issued.key_id}_wrongsecret", limiter)
    _sign_in(session, issued.token, limiter)

    assert [row.detail for row in _succeeded(session)] == [
        {"key_id": issued.key_id},
        {"key_id": issued.key_id, "suppressed": 1},
    ]


def test_a_suppressed_success_writes_no_row_and_commits_nothing(session: Session) -> None:
    admin = _admin(session)
    issued = _services(session)[0].issue(admin, "admin", name="ui")
    limiter = SignInAuditLimiter(WINDOW, clock=_FakeClock())
    _sign_in(session, issued.token, limiter)
    commits: list[None] = []
    statements: list[str] = []
    event.listen(session, "after_commit", lambda _session: commits.append(None))
    event.listen(
        session.get_bind(),
        "before_cursor_execute",
        lambda _conn, _cursor, statement, *_rest: statements.append(statement),
    )

    _sign_in(session, issued.token, limiter)

    # The one commit is `PatService.verify`'s `last_used_at`, as before this change.
    assert len(commits) == 1
    assert not [s for s in statements if "audit_events" in s]
    assert len(_succeeded(session)) == 1


def test_a_success_whose_row_cannot_be_written_is_recorded_by_the_next(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = _admin(session)
    issued = _services(session)[0].issue(admin, "admin", name="ui")
    limiter = SignInAuditLimiter(WINDOW, clock=_FakeClock())
    real_record = AuditService.record

    def broken(self: AuditService, *args: object, **kwargs: object) -> AuditEvent:
        """Fail the way a database that is gone fails.

        Parameters
        ----------
        self
            The audit service.
        *args
            Ignored.
        **kwargs
            Ignored.

        Raises
        ------
        OperationalError
            Always.
        """
        raise OperationalError("INSERT", {}, Exception("the database is gone"))

    monkeypatch.setattr(AuditService, "record", broken)
    with pytest.raises(OperationalError):
        _sign_in(session, issued.token, limiter)
    monkeypatch.setattr(AuditService, "record", real_record)
    session.rollback()
    _sign_in(session, issued.token, limiter)

    assert [row.detail for row in _succeeded(session)] == [
        {"key_id": issued.key_id, "suppressed": 1}
    ]


def test_the_server_builds_the_limiter_from_the_setting(settings: Settings) -> None:
    configured = settings.model_copy(update={"audit": ServerAuditConfig(sign_in_window_minutes=7)})

    with TestClient(create_app(configured)) as client:
        assert client.app.state.sign_in_audit_limiter.window_seconds == pytest.approx(420)
