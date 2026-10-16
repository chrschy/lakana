"""What a bound task token may do: reads its inputs, writes its own table."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.auth import (
    Action,
    BoundTask,
    PrincipalRef,
    Securable,
    SecurableKind,
    TaskScope,
)
from lakana_core.errors import PermissionDenied
from lakana_core.grants.authorization import (
    BOUND_TASK_TOKEN_ACTIONS,
    TASK_TOKEN_ACTIONS,
    GrantAuthorization,
)
from lakana_core.grants.roles import Role
from lakana_core.identifiers import NamespacePath
from lakana_core.models import AuditEvent, AuditEventType, Grant, Principal, PrincipalType

_BOUND = BoundTask(
    task_run_id=uuid.uuid4(),
    claim=1,
    asset="gold.scores.daily",
    inputs=("silver.weather",),
    readable=("bronze.raw.stations", "silver.weather"),
)


def _principal(bound: BoundTask | None = _BOUND, environment: str = "dev") -> PrincipalRef:
    """Build a principal authenticated with a task token.

    Parameters
    ----------
    bound
        The bound task, or `None` for a REST-minted token.
    environment
        The token's environment.

    Returns
    -------
    PrincipalRef
        The principal; its `asset_scope` strings deliberately cover everything,
        so only the bound rules can refuse.
    """
    return PrincipalRef(
        id=uuid.uuid4(),
        name="runner",
        type=PrincipalType.SERVICE_ACCOUNT,
        task_scope=TaskScope(
            environment=environment,
            asset_scope=["gold.*", "silver.*", "bronze.*", "other.*"],
            token_id=uuid.uuid4(),
            bound=bound,
        ),
    )


def _table(dotted: str, environment: str = "dev") -> Securable:
    """Build a table securable from a dotted name.

    Parameters
    ----------
    dotted
        ``namespace.table``.
    environment
        Environment name.

    Returns
    -------
    Securable
        The table.
    """
    *namespace, name = dotted.split(".")
    return Securable.table_scope(environment, NamespacePath.from_parts(namespace), name)


def _namespace(dotted: str) -> Securable:
    """Build a namespace securable in `dev`.

    Parameters
    ----------
    dotted
        The dotted namespace.

    Returns
    -------
    Securable
        The namespace.
    """
    return Securable.namespace_scope("dev", NamespacePath.from_parts(dotted.split(".")))


def _allowed(action: Action, securable: Securable, principal: PrincipalRef | None = None) -> bool:
    """Run the scope check.

    Parameters
    ----------
    action
        The action.
    securable
        The securable.
    principal
        The principal; the bound one when `None`.

    Returns
    -------
    bool
        Whether the check passed.
    """
    p = principal or _principal()
    assert p.task_scope is not None
    try:
        GrantAuthorization._check_task_scope(p, p.task_scope, action, securable)
    except PermissionDenied:
        return False
    return True


def test_bound_actions_are_the_task_token_actions_without_drop() -> None:
    assert TASK_TOKEN_ACTIONS - {Action.DROP_TABLE} == BOUND_TASK_TOKEN_ACTIONS
    assert {
        Action.READ_ENVIRONMENT,
        Action.READ_NAMESPACE,
        Action.READ_TABLE,
        Action.SUBMIT_QUERY,
        Action.CREATE_TABLE,
        Action.WRITE_TABLE,
    } == BOUND_TASK_TOKEN_ACTIONS


@pytest.mark.parametrize("action", sorted(Action, key=str))
def test_table_actions_on_the_own_asset(action: Action) -> None:
    expected = action in (Action.CREATE_TABLE, Action.WRITE_TABLE)
    assert _allowed(action, _table("gold.scores.daily")) is expected


@pytest.mark.parametrize("action", sorted(Action, key=str))
def test_table_actions_on_a_declared_input(action: Action) -> None:
    assert _allowed(action, _table("silver.weather")) is (action is Action.READ_TABLE)


@pytest.mark.parametrize("action", sorted(Action, key=str))
def test_table_actions_on_any_other_table(action: Action) -> None:
    for dotted in ("silver.other", "gold.scores.weekly", "gold.scores", "other.t"):
        assert _allowed(action, _table(dotted)) is False


def test_the_table_behind_a_view_input_is_readable_and_nothing_more() -> None:
    assert _allowed(Action.READ_TABLE, _table("bronze.raw.stations"))
    assert not _allowed(Action.WRITE_TABLE, _table("bronze.raw.stations"))
    assert not _allowed(Action.CREATE_TABLE, _table("bronze.raw.stations"))


def test_the_own_asset_is_not_readable_unless_declared() -> None:
    assert not _allowed(Action.READ_TABLE, _table("gold.scores.daily"))
    declared = BoundTask(
        task_run_id=uuid.uuid4(),
        claim=1,
        asset="gold.scores.daily",
        inputs=("gold.scores.daily",),
        readable=("gold.scores.daily",),
    )
    assert _allowed(Action.READ_TABLE, _table("gold.scores.daily"), _principal(declared))


def test_drop_is_refused_everywhere() -> None:
    for dotted in ("gold.scores.daily", "silver.weather"):
        with pytest.raises(PermissionDenied, match="may not DROP_TABLE"):
            p = _principal()
            assert p.task_scope is not None
            GrantAuthorization._check_task_scope(p, p.task_scope, Action.DROP_TABLE, _table(dotted))
    assert _allowed(Action.DROP_TABLE, _table("gold.x"), _principal(bound=None))


@pytest.mark.parametrize("action", sorted(Action, key=str))
def test_environment_actions(action: Action) -> None:
    expected = action in (Action.READ_ENVIRONMENT, Action.READ_NAMESPACE, Action.SUBMIT_QUERY)
    assert _allowed(action, Securable.environment_scope("dev")) is expected


@pytest.mark.parametrize(
    ("namespace", "expected"),
    [
        ("gold", True),
        ("gold.scores", True),
        ("silver", True),
        ("bronze", True),
        ("bronze.raw", True),
        ("gold.scores.daily", False),
        ("gold.other", False),
        ("other", False),
        ("silver.weather", False),
    ],
)
def test_namespace_navigation(namespace: str, expected: bool) -> None:
    assert _allowed(Action.READ_NAMESPACE, _namespace(namespace)) is expected


@pytest.mark.parametrize("action", sorted(set(Action) - {Action.READ_NAMESPACE}, key=str))
def test_no_other_action_on_a_namespace(action: Action) -> None:
    """Listing tables, creating tables and writing are table-scoped for a bound token."""
    assert _allowed(action, _namespace("gold.scores")) is False


@pytest.mark.parametrize("action", sorted(Action, key=str))
def test_another_environment_and_the_instance_are_refused(action: Action) -> None:
    assert _allowed(action, _table("gold.scores.daily", environment="prod")) is False
    assert _allowed(action, Securable.environment_scope("prod")) is False
    assert _allowed(action, Securable.instance_scope()) is False


def test_messages_name_the_rule() -> None:
    p = _principal()
    scope = p.task_scope
    assert scope is not None
    with pytest.raises(
        PermissionDenied, match=r"may read only its declared inputs, not 'silver\.other'"
    ):
        GrantAuthorization._check_task_scope(p, scope, Action.READ_TABLE, _table("silver.other"))
    with pytest.raises(
        PermissionDenied, match=r"may write only its own asset 'gold\.scores\.daily', not 'gold\.x'"
    ):
        GrantAuthorization._check_task_scope(p, scope, Action.WRITE_TABLE, _table("gold.x"))


def test_a_legacy_dotted_component_never_matches_by_its_display_name() -> None:
    """`["gold", "scores.daily"]` renders like the own asset but is another table."""
    lookalike = Securable.table_scope(
        "dev", NamespacePath.from_parts(["gold"], allow_dots=True), "scores.daily"
    )
    assert _allowed(Action.WRITE_TABLE, lookalike) is False
    odd_namespace = Securable.table_scope(
        "dev", NamespacePath.from_parts(["gold.scores"], allow_dots=True), "daily"
    )
    assert _allowed(Action.WRITE_TABLE, odd_namespace) is False


def test_an_unbound_token_keeps_the_pattern_rules() -> None:
    unbound = _principal(bound=None)
    assert _allowed(Action.READ_TABLE, _table("gold.anything"), unbound)
    assert _allowed(Action.WRITE_TABLE, _table("silver.anything"), unbound)
    assert _allowed(Action.READ_TABLE, _namespace("gold"), unbound)


def test_the_bound_scope_only_narrows(session: Session) -> None:
    """Without a grant the own asset is still refused; with one, only the scope decides."""
    row = Principal(
        id=uuid.uuid4(), type=PrincipalType.SERVICE_ACCOUNT, name="runner", is_active=True
    )
    session.add(row)
    session.commit()
    scope = TaskScope(environment="dev", asset_scope=[], token_id=uuid.uuid4(), bound=_BOUND)
    p = PrincipalRef(id=row.id, name=row.name, type=row.type, task_scope=scope)
    authz = GrantAuthorization(session)
    with pytest.raises(PermissionDenied, match="may not WRITE_TABLE this"):
        authz.authorize(p, Action.WRITE_TABLE, _table("gold.scores.daily"))
    session.add(
        Grant(
            id=uuid.uuid4(),
            principal_id=row.id,
            role=Role.ADMIN,
            kind=SecurableKind.INSTANCE,
            granted_by=row.id,
            granted_at=datetime.now(UTC),
        )
    )
    session.commit()
    authz.authorize(p, Action.WRITE_TABLE, _table("gold.scores.daily"))
    authz.authorize(p, Action.READ_TABLE, _table("silver.weather"))
    with pytest.raises(PermissionDenied, match="declared inputs"):
        authz.authorize(p, Action.READ_TABLE, _table("gold.scores.daily"))
    with pytest.raises(PermissionDenied, match="own asset"):
        authz.authorize(p, Action.WRITE_TABLE, _table("silver.weather"))


_MALFORMED = (
    "",
    ".",
    "nodots",
    "bronze..orders",
    ".orders",
    "bronze.",
    "bronze/x.orders",
    "bronze.\x1fx",
    "bronze.\x00x",
    " . ",
    "..",
    "a" * 5000,
    "a" * 2500 + "." + "b" * 2500 + "\x1f",
)


def _malformed_principal() -> PrincipalRef:
    """Build a principal whose bound task carries only unparseable names.

    Returns
    -------
    PrincipalRef
        The principal.
    """
    bound = BoundTask(
        task_run_id=uuid.uuid4(),
        claim=1,
        asset="nodots",
        inputs=_MALFORMED,
        readable=_MALFORMED,
    )
    return _principal(bound)


@pytest.mark.parametrize("action", sorted(Action, key=str))
def test_malformed_names_match_nothing(action: Action) -> None:
    """An unparsed name is never a prefix, a wildcard or an empty namespace."""
    p = _malformed_principal()
    candidates = [
        _table("gold.orders"),
        _table("bronze.orders"),
        _table("a.b"),
        Securable.table_scope("dev", NamespacePath.from_parts(["bronze"]), "orders"),
        Securable.table_scope("dev", NamespacePath.from_parts(["nodots"]), "nodots"),
        # Never built by the parsers, but the dataclass itself does not stop them.
        Securable.table_scope("dev", NamespacePath(("bronze", "")), "x"),
        Securable.table_scope("dev", NamespacePath(("", "")), ""),
        Securable.table_scope("dev", NamespacePath.from_parts(["a" * 5000]), "a"),
        _namespace("bronze"),
        _namespace("a"),
        _namespace("gold"),
    ]
    for securable in candidates:
        assert _allowed(action, securable, p) is False


def test_a_malformed_name_does_not_spoil_the_valid_ones() -> None:
    bound = BoundTask(
        task_run_id=uuid.uuid4(),
        claim=1,
        asset="gold.scores.daily",
        inputs=("",),
        readable=("", ".", "silver.weather", "bronze..orders"),
    )
    p = _principal(bound)
    assert _allowed(Action.READ_TABLE, _table("silver.weather"), p)
    assert _allowed(Action.WRITE_TABLE, _table("gold.scores.daily"), p)
    assert _allowed(Action.READ_NAMESPACE, _namespace("silver"), p)
    assert not _allowed(Action.READ_TABLE, _table("bronze.orders"), p)
    assert not _allowed(Action.READ_NAMESPACE, _namespace("bronze"), p)


def test_a_malformed_own_asset_is_writable_nowhere() -> None:
    bound = BoundTask(
        task_run_id=uuid.uuid4(), claim=1, asset="gold..daily", inputs=(), readable=()
    )
    p = _principal(bound)
    for dotted in ("gold.daily", "gold.x.daily", "gold.scores.daily"):
        assert not _allowed(Action.WRITE_TABLE, _table(dotted), p)
        assert not _allowed(Action.CREATE_TABLE, _table(dotted), p)
    assert not _allowed(Action.READ_NAMESPACE, _namespace("gold"), p)


def test_a_bound_denial_is_audited(session: Session) -> None:
    """The bound rules refuse inside the same wrapper every other denial goes through."""
    row = Principal(
        id=uuid.uuid4(), type=PrincipalType.SERVICE_ACCOUNT, name="runner", is_active=True
    )
    session.add(row)
    session.commit()
    session.add(
        Grant(
            id=uuid.uuid4(),
            principal_id=row.id,
            role=Role.ADMIN,
            kind=SecurableKind.INSTANCE,
            granted_by=row.id,
            granted_at=datetime.now(UTC),
        )
    )
    session.commit()
    scope = TaskScope(environment="dev", asset_scope=[], token_id=uuid.uuid4(), bound=_BOUND)
    p = PrincipalRef(id=row.id, name=row.name, type=row.type, task_scope=scope)
    authz = AuditingAuthorization(GrantAuthorization(session), session, DenialAuditLimiter(10_000))
    with pytest.raises(PermissionDenied, match="declared inputs"):
        authz.authorize(p, Action.READ_TABLE, _table("silver.other"))
    events = session.query(AuditEvent).all()
    assert len(events) == 1
    assert events[0].event_type == AuditEventType.AUTHORIZATION_DENIED
    assert events[0].securable_table == "other"
    assert events[0].detail["action"] == "READ_TABLE"


def test_environment_namespace_and_table_denials_name_the_rule() -> None:
    p = _principal()
    scope = p.task_scope
    assert scope is not None
    with pytest.raises(
        PermissionDenied, match=r"^'runner''s task token may not WRITE_TABLE on an environment\.$"
    ):
        GrantAuthorization._check_task_scope(
            p, scope, Action.WRITE_TABLE, Securable.environment_scope("dev")
        )
    with pytest.raises(
        PermissionDenied,
        match=r"^'runner''s task token may not READ_NAMESPACE namespace 'gold\.other'\.$",
    ):
        GrantAuthorization._check_task_scope(
            p, scope, Action.READ_NAMESPACE, _namespace("gold.other")
        )
    with pytest.raises(
        PermissionDenied,
        match=r"^'runner''s task token may not READ_TABLE namespace 'gold\.scores'\.$",
    ):
        GrantAuthorization._check_task_scope(p, scope, Action.READ_TABLE, _namespace("gold.scores"))
    with pytest.raises(
        PermissionDenied, match=r"^'runner''s task token may not SUBMIT_QUERY a table\.$"
    ):
        GrantAuthorization._check_task_scope(
            p, scope, Action.SUBMIT_QUERY, _table("gold.scores.daily")
        )
    with pytest.raises(PermissionDenied, match=r"^A task's token may not DROP_TABLE\.$"):
        GrantAuthorization._check_task_scope(p, scope, Action.DROP_TABLE, _table("gold.x"))


_ARTIFACT_TASK = BoundTask(
    task_run_id=uuid.uuid4(),
    claim=1,
    asset="gold.scores",
    inputs=("models.churn", "silver.rows"),
    readable=("models.churn", "silver.rows"),
)
_PRODUCER = BoundTask(
    task_run_id=uuid.uuid4(),
    claim=1,
    asset="models.churn",
    inputs=("silver.rows",),
    readable=("silver.rows",),
    output="artifact",
    keep_versions=2,
)


def test_a_bound_token_reads_only_declared_artifacts() -> None:
    """An artifact is in the name registry, so the input rule covers it."""
    p = _principal(_ARTIFACT_TASK)
    scope = p.task_scope
    assert scope is not None
    check = GrantAuthorization._check_task_scope
    check(p, scope, Action.READ_TABLE, _table("models.churn"))
    for other in ("models.other", "models.churn.v2", "gold.scores"):
        with pytest.raises(PermissionDenied, match="may read only its declared inputs"):
            check(p, scope, Action.READ_TABLE, _table(other))
    for action in (Action.WRITE_TABLE, Action.CREATE_TABLE):
        with pytest.raises(PermissionDenied, match="may write only its own asset"):
            check(p, scope, action, _table("models.churn"))
    with pytest.raises(PermissionDenied, match="may not DROP_TABLE"):
        check(p, scope, Action.DROP_TABLE, _table("models.churn"))


def test_a_producer_writes_its_own_artifact_and_cannot_delete_or_read_it() -> None:
    p = _principal(_PRODUCER)
    scope = p.task_scope
    assert scope is not None
    check = GrantAuthorization._check_task_scope
    check(p, scope, Action.CREATE_TABLE, _table("models.churn"))
    check(p, scope, Action.WRITE_TABLE, _table("models.churn"))
    with pytest.raises(PermissionDenied):
        check(p, scope, Action.READ_TABLE, _table("models.churn"))
    with pytest.raises(PermissionDenied):
        check(p, scope, Action.DROP_TABLE, _table("models.churn"))
    with pytest.raises(PermissionDenied):
        check(p, scope, Action.WRITE_TABLE, _table("models.other"))


def test_bound_task_defaults_describe_a_table_asset() -> None:
    assert (_ARTIFACT_TASK.output, _ARTIFACT_TASK.keep_versions) == ("table", None)
    assert (_PRODUCER.output, _PRODUCER.keep_versions) == ("artifact", 2)


def _refusal(action: Action, securable: Securable) -> str:
    """Run the scope check on the bound principal and return the refusal's text.

    Parameters
    ----------
    action
        The action.
    securable
        The securable.

    Returns
    -------
    str
        The `PermissionDenied` message.
    """
    p = _principal()
    scope = p.task_scope
    assert scope is not None
    with pytest.raises(PermissionDenied) as info:
        GrantAuthorization._check_task_scope(p, scope, action, securable)
    return str(info.value)


def test_a_lookalike_of_an_input_is_not_refused_as_itself() -> None:
    """The refusal of a dotted lookalike does not say "only X, not X"."""
    read = Securable.table_scope(
        "dev", NamespacePath.from_parts(["bronze.raw"], allow_dots=True), "stations"
    )
    message = _refusal(Action.READ_TABLE, read)
    assert "not 'bronze.raw.stations'" not in message
    assert message == (
        "'runner''s task may read only its declared inputs; 'bronze.raw.stations' has a dot "
        "inside one of its names, which makes it another table than the input of that name."
    )
    write = Securable.table_scope(
        "dev", NamespacePath.from_parts(["gold"], allow_dots=True), "scores.daily"
    )
    message = _refusal(Action.WRITE_TABLE, write)
    assert "not 'gold.scores.daily'" not in message
    assert message == (
        "'runner''s task may write only its own asset 'gold.scores.daily'; 'gold.scores.daily' "
        "has a dot inside one of its names, which makes it another table."
    )


def test_a_long_table_name_is_cut_in_the_refusal() -> None:
    """A 403 never echoes a name of any length."""
    long_name = "t" * 5000
    for action in (Action.READ_TABLE, Action.WRITE_TABLE):
        message = _refusal(action, _table(f"gold.{long_name}"))
        assert len(message) < 400
    assert len(_refusal(Action.READ_NAMESPACE, _namespace("n" * 5000))) < 400
    assert len(_refusal(Action.CREATE_TABLE, _namespace("n" * 5000))) < 400


def test_an_escape_in_a_table_name_is_written_out_in_the_refusal() -> None:
    """A control character in the name reaches the message as text."""
    securable = Securable.table_scope(
        "dev", NamespacePath.from_parts(["gold"], allow_dots=True), "a\x1b[31mb"
    )
    message = _refusal(Action.READ_TABLE, securable)
    assert "\x1b" not in message
    assert "a\\x1b[31mb" in message
