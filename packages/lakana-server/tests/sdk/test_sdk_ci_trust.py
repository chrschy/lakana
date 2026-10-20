"""`client.ci_trust_rules` and `exchange_id_token` against the in-process server."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

import lakana
from lakana.errors import AuthenticationError, NotFound, PermissionDenied, ValidationError
from lakana.resources.ci_trust_rules import exchange_id_token
from lakana_core.ci_oidc.config import GITHUB_ISSUER, CiOidcConfig
from lakana_server.settings import Settings
from tests.api.platform.test_ci_oidc import AUDIENCE, SHA, _app_client, _claims, _sign
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment


@pytest.fixture
def ci_server(settings: Settings) -> Iterator[TestClient]:
    """Run a server with CI OIDC on, ``prod``, ``analytics`` and a deployer holding DEPLOY.

    Parameters
    ----------
    settings
        The base settings.

    Yields
    ------
    TestClient
        The client.
    """
    configured = settings.model_copy(
        update={"ci_oidc": CiOidcConfig(issuers=["github"], audience=AUDIENCE)}
    )
    for client in _app_client(configured):
        create_environment(client, "prod")
        client.post(
            "/api/v1/repositories",
            headers=AUTH_HEADER,
            json={"name": "analytics", "kind": "https", "url": "https://github.com/o/a.git"},
        )
        client.post(
            "/api/v1/principals",
            headers=AUTH_HEADER,
            json={"name": "deployer", "type": "SERVICE_ACCOUNT"},
        )
        client.post(
            "/api/v1/grants",
            headers=AUTH_HEADER,
            json={
                "principal": "deployer",
                "action": "DEPLOY",
                "kind": "ENVIRONMENT",
                "environment": "prod",
            },
        )
        yield client


@pytest.fixture
def admin(ci_server: TestClient) -> Iterator[lakana.Client]:
    """Provide an SDK client as the bootstrap administrator.

    Parameters
    ----------
    ci_server
        The server.

    Yields
    ------
    lakana.Client
        The client.
    """
    token = AUTH_HEADER["Authorization"].removeprefix("Bearer ")
    with lakana.Client(url="http://testserver", token=token, http_client=ci_server) as client:
        yield client


def test_rule_lifecycle_explain_and_exchange(admin: lakana.Client, ci_server: TestClient) -> None:
    rule = admin.ci_trust_rules.create(
        issuer="github",
        principal="deployer",
        environment="prod",
        repository="analytics",
        conditions={"repository_id": "123456", "ref": "refs/heads/main"},
    )
    assert (rule.issuer, rule.kinds, rule.enabled) == (GITHUB_ISSUER, ["deploy"], True)
    assert [r.id for r in admin.ci_trust_rules.list()] == [rule.id]
    assert admin.ci_trust_rules.get(rule.id).principal == "deployer"
    rule = admin.ci_trust_rules.update(rule.id, kinds=["deploy", "dry_run"])
    assert rule.kinds == ["deploy", "dry_run"]
    explained = admin.ci_trust_rules.explain(
        _claims(), environment="prod", repository="analytics", issuer="github"
    )
    assert explained.matching == [rule.id]
    assert explained.rules[0].matches
    issued = exchange_id_token(
        "http://testserver",
        _sign(),
        environment="prod",
        repository="analytics",
        http_client=ci_server,
    )
    assert issued.sha == SHA and issued.kinds == ["deploy", "dry_run"]
    assert issued.token not in repr(issued)
    with lakana.Client(url="http://testserver", token=issued.token, http_client=ci_server) as ci:
        assert issued.token not in repr(ci)
        accepted = ci.deploys.create("prod", "analytics", kind="deploy")
        assert ci.deploys.get("prod", accepted.id).requested_ref == SHA
        with pytest.raises(PermissionDenied, match="already submitted"):
            ci.deploys.create("prod", "analytics", kind="dry_run")
    assert not admin.ci_trust_rules.disable(rule.id).enabled
    assert admin.ci_trust_rules.enable(rule.id).enabled
    admin.ci_trust_rules.update(rule.id, conditions={"repository_id": "1", "environment": "x"})
    admin.ci_trust_rules.update(rule.id, principal="deployer")
    admin.ci_trust_rules.delete(rule.id)
    with pytest.raises(NotFound):
        admin.ci_trust_rules.get(rule.id)


def test_refusals_map_to_sdk_errors(admin: lakana.Client, ci_server: TestClient) -> None:
    with pytest.raises(ValidationError, match="repository_id"):
        admin.ci_trust_rules.create(
            issuer="github",
            principal="deployer",
            environment="prod",
            repository="analytics",
            conditions={"ref": "refs/heads/main"},
        )
    with pytest.raises(PermissionDenied, match="ci-trust explain"):
        exchange_id_token(
            "http://testserver",
            _sign(),
            environment="prod",
            repository="analytics",
            http_client=ci_server,
        )
    with pytest.raises(AuthenticationError, match="LAKANA_CI_OIDC__AUDIENCE"):
        exchange_id_token(
            "http://testserver",
            _sign(_claims(aud="https://elsewhere.example")),
            environment="prod",
            repository="analytics",
            http_client=ci_server,
        )
