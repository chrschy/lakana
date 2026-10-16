"""Tests for bootstrap principal creation and admin lockout recovery."""

import uuid
from datetime import UTC, datetime
from unittest import mock

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import lakana_core.bootstrap as bootstrap_module
from lakana_core.audit.service import AuditService
from lakana_core.auth import AllowAllAuthorization, PrincipalRef, Securable, SecurableKind
from lakana_core.bootstrap import (
    admin_exists,
    bootstrap_if_needed,
    ensure_bootstrap_admin_grant,
    ensure_bootstrap_principal,
)
from lakana_core.errors import ConflictError
from lakana_core.grants.admin import holds_instance_admin
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identity.groups import GroupService
from lakana_core.identity.pats import PatService
from lakana_core.identity.principals import PrincipalService
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Grant,
    PersonalAccessToken,
    Principal,
    PrincipalType,
)


def test_the_principal_is_created_on_first_call(session: Session) -> None:
    """A fresh database gains exactly one principal."""
    ref = ensure_bootstrap_principal(session, "admin")

    rows = session.execute(select(Principal)).scalars().all()
    assert [r.name for r in rows] == ["admin"]
    assert ref.name == "admin"
    assert ref.type is PrincipalType.USER


def test_first_boot_records_principal_created_exactly_once(session: Session) -> None:
    """Creating the bootstrap principal records `PRINCIPAL_CREATED`, attributed to itself.

    No other principal exists yet at first boot, so the new principal is
    its own actor.
    """
    ref = ensure_bootstrap_principal(session, "admin")

    events = (
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.PRINCIPAL_CREATED)
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].principal_id == ref.id
    assert events[0].detail == {"principal": "admin", "type": "USER"}


def test_a_restart_records_no_second_principal_created(session: Session) -> None:
    """The idempotent second call finds the existing row and records nothing new."""
    ensure_bootstrap_principal(session, "admin")

    ensure_bootstrap_principal(session, "admin")

    events = (
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.PRINCIPAL_CREATED)
        )
        .scalars()
        .all()
    )
    assert len(events) == 1


def test_the_second_call_returns_the_same_principal(session: Session) -> None:
    """Called on every start, so it must be idempotent."""
    first = ensure_bootstrap_principal(session, "admin")
    second = ensure_bootstrap_principal(session, "admin")

    assert first.id == second.id
    assert len(session.execute(select(Principal)).scalars().all()) == 1


def test_a_different_name_creates_a_second_principal(session: Session) -> None:
    """The name is configuration, not a constant."""
    ensure_bootstrap_principal(session, "admin")
    other = ensure_bootstrap_principal(session, "operator")

    names = sorted(r.name for r in session.execute(select(Principal)).scalars().all())
    assert names == ["admin", "operator"]
    assert other.name == "operator"


def test_a_lost_race_on_first_start_returns_the_winners_principal(session: Session) -> None:
    """Two servers racing to bootstrap must not crash the loser.

    "Two servers starting at once against one database" is the ordinary
    Docker Compose topology, not an exotic race. A real concurrent writer
    cannot be exercised against a single in-process session, so the race is
    simulated by patching the pre-check to miss the row that is, in fact,
    already there (as `test_create_table_race_on_the_unique_constraint_raises_already_exists`
    does for `create_table`), forcing the insert itself to hit the real
    `Principal.name` unique constraint.
    """
    ensure_bootstrap_principal(session, "admin")
    winner = session.execute(select(Principal).where(Principal.name == "admin")).scalar_one()

    with mock.patch.object(bootstrap_module, "_lookup_principal", side_effect=[None, winner]):
        result = ensure_bootstrap_principal(session, "admin")

    assert result.id == winner.id
    assert len(session.execute(select(Principal)).scalars().all()) == 1


def test_an_integrity_error_that_is_not_a_name_collision_is_not_misreported(
    session: Session,
) -> None:
    """An `IntegrityError` that is not a name collision must not be swallowed.

    If the post-failure re-select also comes back empty, the failure is not
    the race this function exists to tolerate, and must not silently return
    nothing useful or crash on an `AttributeError`.
    """
    with (
        mock.patch.object(bootstrap_module, "_lookup_principal", return_value=None),
        mock.patch.object(Session, "flush", side_effect=IntegrityError("INSERT", {}, Exception())),
        pytest.raises(ConflictError),
    ):
        ensure_bootstrap_principal(session, "admin")


def test_ensure_bootstrap_admin_grant_creates_one(session: Session) -> None:
    """The bootstrap principal gets an admin grant at instance scope."""
    principal = ensure_bootstrap_principal(session, "admin")
    ensure_bootstrap_admin_grant(session, principal, AllowAllAuthorization())

    grants = (
        session.execute(select(Grant).where(Grant.principal_id == principal.id)).scalars().all()
    )
    assert len(grants) == 1
    assert grants[0].role == Role.ADMIN
    assert grants[0].kind == SecurableKind.INSTANCE


def test_ensure_bootstrap_admin_grant_is_idempotent(session: Session) -> None:
    """Calling it twice does not create a second grant."""
    principal = ensure_bootstrap_principal(session, "admin")
    ensure_bootstrap_admin_grant(session, principal, AllowAllAuthorization())
    ensure_bootstrap_admin_grant(session, principal, AllowAllAuthorization())

    grants = (
        session.execute(select(Grant).where(Grant.principal_id == principal.id)).scalars().all()
    )
    assert len(grants) == 1


def test_admin_exists_is_false_on_a_fresh_database(session: Session) -> None:
    """No principal, no grant: no admin exists yet."""
    assert admin_exists(session) is False


def test_admin_exists_is_true_for_a_direct_active_grant(session: Session) -> None:
    """A principal with a direct, unrevoked instance ADMIN grant counts."""
    admin = ensure_bootstrap_principal(session, "admin")
    ensure_bootstrap_admin_grant(session, admin, AllowAllAuthorization())

    assert admin_exists(session) is True


def test_admin_exists_is_false_when_the_only_admin_grant_is_revoked(session: Session) -> None:
    """A revoked grant does not count, even though the row still exists.

    Revoked directly on the row, bypassing `GrantService.revoke` — which
    would itself refuse to leave zero active admins. This simulates the
    lockout scenario `admin_exists`/`bootstrap_if_needed` exist to recover
    from: an admin grant gone by some path other than that guarded
    service call (a direct database edit, a bug, a restore from backup).
    """
    admin = ensure_bootstrap_principal(session, "admin")
    ensure_bootstrap_admin_grant(session, admin, AllowAllAuthorization())
    [grant] = session.execute(select(Grant).where(Grant.principal_id == admin.id)).scalars().all()
    grant.revoked_at = datetime.now(UTC)
    session.commit()

    assert admin_exists(session) is False


def test_admin_exists_is_false_for_an_inactive_principal(session: Session) -> None:
    """A deactivated principal's still-unrevoked grant does not count."""
    admin = ensure_bootstrap_principal(session, "admin")
    ensure_bootstrap_admin_grant(session, admin, AllowAllAuthorization())
    row = session.get(Principal, admin.id)
    assert row is not None
    row.is_active = False
    session.commit()

    assert admin_exists(session) is False


def test_admin_via_group_counts_as_existing_admin(session: Session) -> None:
    """An instance ADMIN grant on a group, held by an active member, counts too."""
    root = ensure_bootstrap_principal(session, "root")
    principals = PrincipalService(session, AllowAllAuthorization())
    groups = GroupService(session, AllowAllAuthorization())
    grants = GrantService(session, AllowAllAuthorization())
    principals.create_principal(root, name="alice", type=PrincipalType.USER)
    groups.create_group(root, name="admins")
    groups.add_member(root, "admins", "alice")
    grants.grant(root, group="admins", role=Role.ADMIN, kind=SecurableKind.INSTANCE)

    assert admin_exists(session) is True


def test_bootstrap_if_needed_does_nothing_when_an_admin_already_exists(session: Session) -> None:
    """The fix for the lockout exploit: an existing admin blocks every bootstrap action."""
    carol = ensure_bootstrap_principal(session, "carol")
    ensure_bootstrap_admin_grant(session, carol, AllowAllAuthorization())

    token = bootstrap_if_needed(session, "admin", key_id="testkey", secret="testsecret")

    assert token is None
    assert (
        session.execute(select(Principal).where(Principal.name == "admin")).scalar_one_or_none()
        is None
    )


def test_bootstrap_if_needed_creates_grants_and_mints_when_no_admin_exists(
    session: Session,
) -> None:
    """The recovery path: principal, grant, and PAT all come into being."""
    token = bootstrap_if_needed(session, "admin", key_id="testkey", secret="testsecret")

    assert token == "lakana_pat_testkey_testsecret"
    admin = session.execute(select(Principal).where(Principal.name == "admin")).scalar_one()
    grants = session.execute(select(Grant).where(Grant.principal_id == admin.id)).scalars().all()
    assert any(
        g.role == Role.ADMIN and g.kind == SecurableKind.INSTANCE and g.revoked_at is None
        for g in grants
    )


def test_bootstrap_if_needed_reactivates_an_inactive_principal(session: Session) -> None:
    """A deactivated bootstrap principal is reactivated as part of recovery."""
    admin = ensure_bootstrap_principal(session, "admin")
    row = session.get(Principal, admin.id)
    assert row is not None
    row.is_active = False
    session.commit()

    bootstrap_if_needed(session, "admin", key_id="testkey", secret="testsecret")

    session.refresh(row)
    assert row.is_active is True


def test_bootstrap_if_needed_regrants_after_the_only_admin_grant_was_revoked(
    session: Session,
) -> None:
    """A revoked-but-only admin grant is treated as no admin, and re-granted."""
    admin = ensure_bootstrap_principal(session, "admin")
    ensure_bootstrap_admin_grant(session, admin, AllowAllAuthorization())
    [grant] = session.execute(select(Grant).where(Grant.principal_id == admin.id)).scalars().all()
    grant.revoked_at = datetime.now(UTC)
    session.commit()

    bootstrap_if_needed(session, "admin", key_id="testkey", secret="testsecret")

    active_grants = (
        session.execute(
            select(Grant).where(Grant.principal_id == admin.id, Grant.revoked_at.is_(None))
        )
        .scalars()
        .all()
    )
    assert any(g.role == Role.ADMIN and g.kind == SecurableKind.INSTANCE for g in active_grants)


def test_bootstrap_if_needed_records_bootstrap_admin_restored(session: Session) -> None:
    """The recovery path is audited, attributed to the bootstrap principal's own id."""
    bootstrap_if_needed(session, "admin", key_id="testkey", secret="testsecret")

    admin = session.execute(select(Principal).where(Principal.name == "admin")).scalar_one()
    events = (
        session.execute(
            select(AuditEvent).where(
                AuditEvent.event_type == AuditEventType.BOOTSTRAP_ADMIN_RESTORED
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].principal_id == admin.id


def test_bootstrap_if_needed_mints_a_random_pat_when_no_fixed_credential_is_given(
    session: Session,
) -> None:
    """Production configuration (key_id/secret both None) still gets a usable PAT."""
    token = bootstrap_if_needed(session, "admin")

    assert token is not None
    assert token.startswith("lakana_pat_")


def test_bootstrap_if_needed_raises_if_the_principal_vanishes_immediately(
    session: Session,
) -> None:
    """Fix round 1: the production `assert` here is now an explicit raise.

    Should be unreachable in practice — `ensure_bootstrap_principal` just
    created or found this exact row — but needs its own test to stay
    covered now that it is a real `raise` rather than an `assert`.
    """
    with (
        mock.patch.object(Session, "get", return_value=None),
        pytest.raises(ConflictError, match="vanished"),
    ):
        bootstrap_if_needed(session, "admin", key_id="testkey", secret="testsecret")


def test_bootstrap_if_needed_falls_back_to_a_random_pat_when_the_configured_key_id_is_taken(
    session: Session,
) -> None:
    """A later recovery reusing the first boot's fixed key_id mints a random PAT instead.

    Fix round 1: revoking a PAT never frees its `key_id` in the
    unique index, so a second recovery blindly retrying the same
    configured (key_id, secret) pair used to raise `IntegrityError` and
    fail the whole recovery outright. It now falls back to a random
    credential, and the recovery still succeeds and returns a usable
    token.
    """
    first_token = bootstrap_if_needed(session, "admin", key_id="testkey", secret="testsecret")
    assert first_token == "lakana_pat_testkey_testsecret"

    # Wipe the admin grant directly, simulating the lockout without
    # touching the PAT row already minted for "testkey".
    [grant] = session.execute(select(Grant)).scalars().all()
    grant.revoked_at = datetime.now(UTC)
    session.commit()

    second_token = bootstrap_if_needed(session, "admin", key_id="testkey", secret="testsecret")

    assert second_token is not None
    assert second_token != first_token
    assert not second_token.startswith("lakana_pat_testkey_")
    assert admin_exists(session) is True


def test_bootstrap_if_needed_revokes_the_principals_earlier_pats(session: Session) -> None:
    """Recovery revokes every unrevoked PAT the principal already had.

    So the freshly minted, freshly logged PAT is the only working
    credential afterward — an operator relying on the newly logged secret
    can trust that no older, unlogged credential for this principal still
    works.
    """
    bootstrap_if_needed(session, "admin", key_id="testkey", secret="testsecret")
    admin = session.execute(select(Principal).where(Principal.name == "admin")).scalar_one()
    [first_pat] = (
        session.execute(
            select(PersonalAccessToken).where(PersonalAccessToken.principal_id == admin.id)
        )
        .scalars()
        .all()
    )
    assert first_pat.revoked_at is None

    [grant] = session.execute(select(Grant).where(Grant.principal_id == admin.id)).scalars().all()
    grant.revoked_at = datetime.now(UTC)
    session.commit()

    bootstrap_if_needed(session, "admin")

    session.refresh(first_pat)
    assert first_pat.revoked_at is not None


def test_bootstrap_if_needed_rolls_back_cleanly_if_pat_minting_fails(session: Session) -> None:
    """A failure while minting the PAT leaves no committed grant to block a retry.

    Once, the admin grant committed on its own, before the
    PAT was minted — so a PAT-minting failure left a committed admin grant
    with no PAT and no audit event, and, because that principal was then
    an active admin, `admin_exists` would be `True` on the next restart,
    so nothing would ever retry the recovery. Grant, PAT, and audit event
    now share one commit, so a failure here leaves none of them behind.
    """
    with (
        mock.patch.object(PatService, "issue", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError, match="boom"),
    ):
        bootstrap_if_needed(session, "admin", key_id="testkey", secret="testsecret")
    session.rollback()

    assert admin_exists(session) is False
    assert session.execute(select(Grant)).scalars().all() == []
    assert session.execute(select(PersonalAccessToken)).scalars().all() == []
    assert (
        session.execute(
            select(AuditEvent).where(
                AuditEvent.event_type == AuditEventType.BOOTSTRAP_ADMIN_RESTORED
            )
        )
        .scalars()
        .all()
        == []
    )

    token = bootstrap_if_needed(session, "admin", key_id="testkey", secret="testsecret")

    assert token == "lakana_pat_testkey_testsecret"
    assert admin_exists(session) is True


def test_bootstrap_if_needed_rolls_back_a_real_issue_commit_false_pat_too(
    session: Session,
) -> None:
    """A failure *after* `PatService.issue(..., commit=False)` has really run still rolls back.

    The test above patches `PatService.issue` away
    entirely, so it never exercises `issue`'s own `commit=False` branch
    (`pats.py`'s `if not commit: break`) actually adding a
    `PersonalAccessToken` row and a `PAT_ISSUED` audit event to the
    session. This test lets that branch run for real, and fails only at
    the very last step — recording `BOOTSTRAP_ADMIN_RESTORED` — to prove
    bootstrap's single-commit guarantee still holds when the failure
    happens *after* `issue`'s own session-mutating code, not just when
    `issue` itself is prevented from running.
    """
    real_record = AuditService.record

    def _record_or_raise(
        self: AuditService,
        principal_id: uuid.UUID | None,
        event_type: AuditEventType,
        *,
        securable: Securable | None = None,
        detail: dict | None = None,
        commit: bool = False,
    ) -> AuditEvent:
        if event_type == AuditEventType.BOOTSTRAP_ADMIN_RESTORED:
            raise RuntimeError("boom")
        return real_record(
            self, principal_id, event_type, securable=securable, detail=detail, commit=commit
        )

    with (
        mock.patch.object(AuditService, "record", _record_or_raise),
        pytest.raises(RuntimeError, match="boom"),
    ):
        bootstrap_if_needed(session, "admin", key_id="testkey", secret="testsecret")
    session.rollback()

    assert admin_exists(session) is False
    assert session.execute(select(Grant)).scalars().all() == []
    assert session.execute(select(PersonalAccessToken)).scalars().all() == []
    # `PRINCIPAL_CREATED` survives: `ensure_bootstrap_principal` commits it
    # in its own, separate transaction, before this attempt's later
    # single-commit (grant + PAT + BOOTSTRAP_ADMIN_RESTORED) ever begins —
    # so it is not part of what this rollback undoes.
    surviving = session.execute(select(AuditEvent)).scalars().all()
    assert [e.event_type for e in surviving] == [AuditEventType.PRINCIPAL_CREATED]

    token = bootstrap_if_needed(session, "admin", key_id="testkey", secret="testsecret")

    assert token == "lakana_pat_testkey_testsecret"
    assert admin_exists(session) is True


def test_the_bootstrap_still_creates_the_first_administrator(session: Session) -> None:
    """The bootstrap principal grants itself ADMIN before it holds it."""
    token = bootstrap_if_needed(session, "admin")
    assert token is not None and token.startswith("lakana_pat_")
    row = session.execute(select(Principal).where(Principal.name == "admin")).scalar_one()
    assert holds_instance_admin(session, row.id)
    admin = PrincipalRef(id=row.id, name=row.name, type=row.type)
    real = GrantAuthorization(session)
    GrantService(session, real).grant(
        admin, principal="admin", role=Role.VIEWER, kind=SecurableKind.INSTANCE
    )
    assert len(PatService(session, real).list(admin, "admin")) == 1
