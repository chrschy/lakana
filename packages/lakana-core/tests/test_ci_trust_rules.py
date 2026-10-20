"""CI trust rules: who may create them, what a rule must pin, the audit, and explain."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from ci_oidc_test_helpers import ISSUER
from ci_trust_test_helpers import CiWorld, add_rule, ci_world, claims, principal, rules
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.ci_oidc.config import GITLAB_ISSUER
from lakana_core.ci_oidc.rules import (
    CiTrustRuleService,
    CiTrustRuleView,
    condition_mismatches,
    id_claim,
)
from lakana_core.errors import NotFoundError, PermissionDenied, ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.models import AuditEvent, AuditEventType, CiTrustRule, Principal, PrincipalType


@pytest.fixture
def world(session: Session, tmp_path: Path) -> CiWorld:
    """Build the CI world.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.

    Returns
    -------
    CiWorld
        The world.
    """
    return ci_world(session, tmp_path)


def _events(session: Session, kind: AuditEventType) -> list[AuditEvent]:
    """Read the audit events of one type, oldest first.

    Parameters
    ----------
    session
        Database session.
    kind
        The event type.

    Returns
    -------
    list of AuditEvent
        The rows.
    """
    return list(
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == kind).order_by(AuditEvent.occurred_at)
        ).scalars()
    )


def test_an_administrator_creates_a_rule_bound_to_ids_and_it_is_audited(
    session: Session, world: CiWorld
) -> None:
    view = rules(session).create(
        world.admin,
        issuer="GitHub",
        principal="deployer",
        environment="prod",
        repository="analytics",
        kinds=["dry_run", "deploy", "deploy"],
        conditions={"repository_id": "123456", "ref": "refs/heads/main", "event_name": "push"},
    )
    assert (view.issuer, view.principal, view.environment, view.repository) == (
        ISSUER,
        "deployer",
        "prod",
        "analytics",
    )
    assert view.kinds == ["deploy", "dry_run"]
    assert view.enabled
    row = session.get(CiTrustRule, view.id)
    assert row is not None and row.principal_id == world.deployer.id and row.revision == 1
    (event,) = _events(session, AuditEventType.CI_TRUST_RULE_CREATED)
    assert event.principal_id == world.admin.id
    assert event.detail["rule_id"] == str(view.id)
    assert event.detail["conditions"] == {
        "repository_id": "123456",
        "ref": "refs/heads/main",
        "event_name": "push",
    }
    assert event.securable_environment == "prod"


def test_only_an_instance_administrator_manages_rules(session: Session, world: CiWorld) -> None:
    rule_id = add_rule(session, world)
    service = rules(session)
    stranger = principal(session, "stranger", PrincipalType.USER)
    calls = [
        lambda: service.create(
            stranger,
            issuer="github",
            principal="deployer",
            environment="prod",
            repository="analytics",
            kinds=["deploy"],
            conditions={"repository_id": "1", "ref": "refs/heads/main", "event_name": "push"},
        ),
        lambda: service.list(stranger),
        lambda: service.get(stranger, rule_id),
        lambda: service.update(stranger, rule_id, kinds=["dry_run"]),
        lambda: service.enable(stranger, rule_id),
        lambda: service.disable(stranger, rule_id),
        lambda: service.delete(stranger, rule_id),
        lambda: service.explain(stranger, claims=claims(), environment="prod", repository="x"),
        lambda: service.get(stranger, uuid.uuid4()),
    ]
    for call in calls:
        with pytest.raises(PermissionDenied):
            call()
    assert session.get(CiTrustRule, rule_id) is not None
    # The deployer holds DEPLOY on prod: that is not enough either.
    with pytest.raises(PermissionDenied):
        service.list(world.deployer)


@pytest.mark.parametrize(
    ("conditions", "needle"),
    [
        ({"ref": "refs/heads/main", "event_name": "push"}, "repository_id"),
        ({"repository_id": "123456"}, "ref or environment"),
        ({"repository_id": "org/repo", "ref": "refs/heads/main", "event_name": "push"}, "digits"),
        ({"repository_id": "", "ref": "refs/heads/main", "event_name": "push"}, "empty"),
        ({"repository_id": "123", "ref": ""}, "empty"),
        ({"repository_id": "123", "ref": 5}, "text"),
        ({"repository_id": "123", "ref": True}, "text"),
        ({"repository_id": "123", "ref": ["refs/heads/main"]}, "text"),
        ({"repository_id": "123", "environment": "prod", "aud": "x"}, "aud"),
        ({"repository_id": "123", "environment": "prod", "Bad-Name": "x"}, "claim name"),
        ({"repository_id": "123", "environment": "prod\n"}, "control"),
        ({"repository_id": "123", "environment": "p" * 256}, "255"),
        (
            {"repository_id": "123", "environment": "prod", **{f"c{i}": "x" for i in range(15)}},
            "16",
        ),
    ],
)
def test_a_rule_must_pin_the_numeric_id_and_a_ref_or_environment(
    session: Session, world: CiWorld, conditions: dict[str, object], needle: str
) -> None:
    with pytest.raises(ValidationError, match=needle):
        rules(session).create(
            world.admin,
            issuer="github",
            principal="deployer",
            environment="prod",
            repository="analytics",
            kinds=["deploy"],
            conditions=conditions,  # ty: ignore[invalid-argument-type]
        )
    assert session.execute(select(CiTrustRule)).first() is None


def test_a_gitlab_rule_pins_project_id() -> None:
    assert id_claim(ISSUER) == "repository_id"
    assert id_claim(GITLAB_ISSUER) == "project_id"
    assert id_claim("https://gitlab.example.com") == "project_id"


@pytest.mark.parametrize(
    ("kinds", "needle"),
    [
        ([], "at least one"),
        (["rollback"], "rollback"),
        (["undeploy"], "undeploy"),
        (["deploy", "DEPLOY"], "DEPLOY"),
    ],
)
def test_a_rule_allows_only_deploys_and_dry_runs(
    session: Session, world: CiWorld, kinds: list[str], needle: str
) -> None:
    with pytest.raises(ValidationError, match=needle):
        rules(session).create(
            world.admin,
            issuer="github",
            principal="deployer",
            environment="prod",
            repository="analytics",
            kinds=kinds,
            conditions={"repository_id": "1", "ref": "refs/heads/main", "event_name": "push"},
        )


def test_the_issuer_must_be_configured(session: Session, world: CiWorld) -> None:
    for issuer in ("gitlab", "https://token.actions.githubusercontent.com/", "nope"):
        with pytest.raises(ValidationError, match="LAKANA_CI_OIDC__ISSUERS"):
            rules(session).create(
                world.admin,
                issuer=issuer,
                principal="deployer",
                environment="prod",
                repository="analytics",
                kinds=["deploy"],
                conditions={"repository_id": "1", "ref": "refs/heads/main", "event_name": "push"},
            )
    off = CiTrustRuleService(session, GrantAuthorization(session), issuers=[])
    with pytest.raises(ValidationError, match="CI OIDC is off"):
        off.create(
            world.admin,
            issuer="github",
            principal="deployer",
            environment="prod",
            repository="analytics",
            kinds=["deploy"],
            conditions={"repository_id": "1", "ref": "refs/heads/main", "event_name": "push"},
        )


def test_the_principal_must_be_an_active_service_account(session: Session, world: CiWorld) -> None:
    principal(session, "alice", PrincipalType.USER)
    retired = principal(session, "retired")
    row = session.get(Principal, retired.id)
    assert row is not None
    row.is_active = False
    session.commit()

    def create(name: str) -> None:
        """Create a rule acting as `name`.

        Parameters
        ----------
        name
            The principal.
        """
        rules(session).create(
            world.admin,
            issuer="github",
            principal=name,
            environment="prod",
            repository="analytics",
            kinds=["deploy"],
            conditions={"repository_id": "1", "ref": "refs/heads/main", "event_name": "push"},
        )

    with pytest.raises(ValidationError, match="service account"):
        create("alice")
    with pytest.raises(ValidationError, match="not active"):
        create("retired")
    with pytest.raises(NotFoundError, match="nobody"):
        create("nobody")


def test_unknown_environment_or_repository_is_named(session: Session, world: CiWorld) -> None:
    service = rules(session)
    for environment, repository, needle in (("nope", "analytics", "nope"), ("prod", "x", "x")):
        with pytest.raises(NotFoundError, match=needle):
            service.create(
                world.admin,
                issuer="github",
                principal="deployer",
                environment=environment,
                repository=repository,
                kinds=["deploy"],
                conditions={"repository_id": "1", "ref": "refs/heads/main", "event_name": "push"},
            )


def test_update_enable_disable_and_delete_are_audited_and_raise_the_revision(
    session: Session, world: CiWorld
) -> None:
    rule_id = add_rule(session, world)
    service = rules(session)
    other = principal(session, "deployer2")
    view = service.update(
        world.admin,
        rule_id,
        principal=other.name,
        kinds=["dry_run"],
        conditions={"repository_id": "123456", "environment": "prod"},
    )
    assert (view.principal, view.kinds, view.conditions) == (
        "deployer2",
        ["dry_run"],
        {"repository_id": "123456", "environment": "prod"},
    )
    row = session.get(CiTrustRule, rule_id)
    assert row is not None and row.revision == 2 and row.updated_by == world.admin.id
    assert not service.disable(world.admin, rule_id).enabled
    assert row.revision == 3
    service.disable(world.admin, rule_id)
    assert row.revision == 3
    assert service.enable(world.admin, rule_id).enabled
    service.enable(world.admin, rule_id)
    assert row.revision == 4
    assert [
        e.detail["rule_id"] for e in _events(session, AuditEventType.CI_TRUST_RULE_UPDATED)
    ] == [str(rule_id)]
    assert len(_events(session, AuditEventType.CI_TRUST_RULE_DISABLED)) == 1
    assert len(_events(session, AuditEventType.CI_TRUST_RULE_ENABLED)) == 1
    service.delete(world.admin, rule_id)
    assert session.get(CiTrustRule, rule_id) is None
    (deleted,) = _events(session, AuditEventType.CI_TRUST_RULE_DELETED)
    assert deleted.detail["rule_id"] == str(rule_id)
    with pytest.raises(NotFoundError):
        service.delete(world.admin, rule_id)


def test_an_update_is_validated_like_a_creation(session: Session, world: CiWorld) -> None:
    rule_id = add_rule(session, world)
    service = rules(session)
    with pytest.raises(ValidationError, match="repository_id"):
        service.update(
            world.admin, rule_id, conditions={"ref": "refs/heads/main", "event_name": "push"}
        )
    with pytest.raises(ValidationError, match="rollback"):
        service.update(world.admin, rule_id, kinds=["rollback"])
    principal(session, "bob", PrincipalType.USER)
    with pytest.raises(ValidationError, match="service account"):
        service.update(world.admin, rule_id, principal="bob")
    with pytest.raises(ValidationError, match="Nothing to change"):
        service.update(world.admin, rule_id)
    row = session.get(CiTrustRule, rule_id)
    assert row is not None and row.revision == 1


def test_list_and_get(session: Session, world: CiWorld) -> None:
    first = add_rule(session, world)
    second = add_rule(session, world, environment="dev")
    service = rules(session)
    assert [view.id for view in service.list(world.admin)] == [first, second]
    assert service.get(world.admin, second).environment == "dev"
    with pytest.raises(NotFoundError):
        service.get(world.admin, uuid.uuid4())


def test_condition_mismatches_are_exact_and_typed() -> None:
    conditions = {"repository_id": "123456", "ref": "refs/heads/main", "event_name": "push"}
    assert condition_mismatches(conditions, claims()) == []
    assert condition_mismatches(conditions, claims(repository_id=123456)) == [
        ("repository_id", "not_text")
    ]
    assert condition_mismatches(conditions, claims(ref="refs/heads/Main")) == [("ref", "differs")]
    assert condition_mismatches(conditions, claims(ref="refs/heads/main ")) == [("ref", "differs")]
    assert condition_mismatches(conditions, claims(ref=None)) == [("ref", "missing")]
    assert condition_mismatches(conditions, claims(ref=["refs/heads/main"])) == [
        ("ref", "not_text")
    ]


def test_explain_says_which_rule_matches_and_why_the_others_do_not(
    session: Session, world: CiWorld
) -> None:
    matching = add_rule(session, world)
    on_dev = add_rule(session, world, environment="dev")
    other_ref = add_rule(
        session,
        world,
        conditions={"repository_id": "123456", "ref": "refs/heads/release", "event_name": "push"},
    )
    disabled = add_rule(session, world)
    rules(session).disable(world.admin, disabled)
    report = rules(session).explain(
        world.admin, claims=claims(), environment="prod", repository="analytics"
    )
    assert report.matching == [matching]
    assert report.verdict == f"accepted by rule {matching}"
    by_id = {item.rule_id: item for item in report.rules}
    assert by_id[matching].matches and by_id[matching].reasons == []
    assert by_id[on_dev].reasons == ["the rule is for environment 'dev', not 'prod'"]
    assert by_id[other_ref].reasons == [
        "claim ref is 'refs/heads/main'; the rule needs 'refs/heads/release'"
    ]
    assert by_id[disabled].reasons == ["the rule is disabled"]


def test_explain_reports_ambiguity_no_rule_and_a_missing_commit(
    session: Session, world: CiWorld
) -> None:
    first = add_rule(session, world)
    second = add_rule(session, world, conditions={"repository_id": "123456", "environment": "prod"})
    service = rules(session)
    report = service.explain(
        world.admin, claims=claims(), environment="prod", repository="analytics"
    )
    assert sorted(report.matching) == sorted([first, second])
    assert report.verdict.startswith("refused: 2 rules match")
    report = service.explain(world.admin, claims=claims(), environment="prod", repository="other")
    assert report.matching == []
    assert report.verdict.startswith("refused: no enabled rule")
    service.delete(world.admin, second)
    report = service.explain(
        world.admin, claims=claims(sha=None), environment="prod", repository="analytics"
    )
    assert report.verdict.startswith("refused: the ID token has no sha claim")


def test_explain_reads_the_issuer_from_the_claims_and_names_an_inactive_principal(
    session: Session, world: CiWorld
) -> None:
    rule_id = add_rule(session, world)
    row = session.get(Principal, world.deployer.id)
    assert row is not None
    row.is_active = False
    session.commit()
    report = rules(session).explain(
        world.admin, claims=claims(), environment="prod", repository="analytics"
    )
    assert report.issuer == ISSUER
    assert report.matching == [rule_id]
    assert report.verdict == "refused: the rule's principal 'deployer' is not active"
    other = rules(session).explain(
        world.admin,
        claims=claims(iss="https://gitlab.com"),
        environment="prod",
        repository="analytics",
    )
    assert other.rules[0].reasons == [
        f"the rule is for issuer {ISSUER!r}, not 'https://gitlab.com'"
    ]
    with pytest.raises(ValidationError, match="issuer"):
        rules(session).explain(
            world.admin, claims=claims(iss=None), environment="prod", repository="analytics"
        )


def test_conditions_must_be_an_object(session: Session, world: CiWorld) -> None:
    with pytest.raises(ValidationError, match="object"):
        rules(session).create(
            world.admin,
            issuer="github",
            principal="deployer",
            environment="prod",
            repository="analytics",
            kinds=["deploy"],
            conditions=["repository_id"],  # ty: ignore[invalid-argument-type]
        )


def test_explain_names_a_missing_claim_and_one_of_the_wrong_type(
    session: Session, world: CiWorld
) -> None:
    add_rule(session, world)
    report = rules(session).explain(
        world.admin,
        claims=claims(ref=None, repository_id=123456),
        environment="prod",
        repository="analytics",
        issuer="github",
    )
    assert report.rules[0].reasons == [
        "claim ref is missing; the rule needs 'refs/heads/main'",
        "claim repository_id is not a text; the rule needs the text '123456'",
    ]


def _create(
    session: Session, world: CiWorld, conditions: dict[str, str], issuer: str = "github"
) -> CiTrustRuleView:
    """Create a rule with these conditions.

    Parameters
    ----------
    session
        Database session.
    world
        The world.
    conditions
        The claim conditions.
    issuer
        The issuer.

    Returns
    -------
    CiTrustRuleView
        The rule.
    """
    return rules(session).create(
        world.admin,
        issuer=issuer,
        principal="deployer",
        environment="prod",
        repository="analytics",
        kinds=["deploy"],
        conditions=conditions,
    )


def test_a_github_rule_must_pin_how_the_run_was_triggered(session: Session, world: CiWorld) -> None:
    with pytest.raises(ValidationError) as caught:
        _create(session, world, {"repository_id": "123456", "ref": "refs/heads/main"})
    message = str(caught.value)
    assert "--claim event_name=push" in message
    assert "--claim environment=production" in message
    assert "pull_request_target" in message
    assert session.execute(select(CiTrustRule)).first() is None
    _create(session, world, {"repository_id": "1", "ref": "refs/heads/main", "event_name": "push"})
    _create(session, world, {"repository_id": "1", "environment": "production"})


def test_a_gitlab_rule_must_pin_a_protected_ref_the_pipeline_source_or_an_environment(
    session: Session, world: CiWorld
) -> None:
    service = CiTrustRuleService(
        session, GrantAuthorization(session), issuers=[ISSUER, "https://gitlab.com"]
    )

    def create(conditions: dict[str, str]) -> None:
        """Create a GitLab rule.

        Parameters
        ----------
        conditions
            Its conditions.
        """
        service.create(
            world.admin,
            issuer="gitlab",
            principal="deployer",
            environment="prod",
            repository="analytics",
            kinds=["deploy"],
            conditions=conditions,
        )

    with pytest.raises(ValidationError, match="--claim ref_protected=true") as caught:
        create({"project_id": "22", "ref": "main"})
    assert "schedules, triggers, the web, the API" in str(caught.value)
    assert "fork" not in str(caught.value)
    create({"project_id": "22", "ref": "main", "ref_protected": "true"})
    create({"project_id": "22", "ref": "main", "pipeline_source": "push"})
    create({"project_id": "22", "environment": "production"})


def test_update_and_enable_check_the_trigger_again(session: Session, world: CiWorld) -> None:
    from sqlalchemy import update

    rule_id = add_rule(session, world)
    service = rules(session)
    with pytest.raises(ValidationError, match="event_name"):
        service.update(
            world.admin, rule_id, conditions={"repository_id": "123456", "ref": "refs/heads/main"}
        )
    # A rule written before the trigger was required (no release has one) cannot be used again.
    service.disable(world.admin, rule_id)
    session.execute(
        update(CiTrustRule).values(conditions={"repository_id": "123456", "ref": "refs/heads/main"})
    )
    session.commit()
    with pytest.raises(ValidationError, match="event_name"):
        service.enable(world.admin, rule_id)
    with pytest.raises(ValidationError, match="event_name"):
        service.update(world.admin, rule_id, kinds=["dry_run"])
    report = service.explain(
        world.admin, claims=claims(), environment="prod", repository="analytics"
    )
    (item,) = report.rules
    assert not item.matches
    assert any("not allowed" in reason and "event_name" in reason for reason in item.reasons)
