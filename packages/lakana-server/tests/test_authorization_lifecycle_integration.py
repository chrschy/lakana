"""The exit test: grants, roles, task-token scope, and the lockout guard, for real.

Real Postgres via testcontainers, a live uvicorn server in a background
thread, mirroring `test_identity_lifecycle_integration.py`'s own pattern.
"""

import socket
import threading
import time

import pytest
import requests
import uvicorn
from testcontainers.postgres import PostgresContainer

from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.models import Base
from lakana_server.app import create_app
from lakana_server.settings import Settings

pytestmark = pytest.mark.integration


def _free_port() -> int:
    """Find an available TCP port on localhost.

    Returns
    -------
    int
        A port not currently bound, suitable for uvicorn to listen on.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_grants_roles_task_scope_and_the_lockout_guard() -> None:
    """A role escalation changes access; a task token's scope narrows it; the lockout guard holds.

    Uses `CREATE_NAMESPACE` for the role-escalation half (in `data_engineer`'s
    bundle, not `analyst`'s — see `lakana_core.grants.roles.ROLE_ACTIONS`) and
    `READ_TABLE` for the task-scope half. Both `CatalogService.create_namespace`
    and `CatalogService.load_table` call `authorize()` *before* touching the
    database row, so the task-scope check below returns 403 without needing a
    real `bronze.orders` table to exist first.
    """
    with PostgresContainer("postgres:16-alpine", driver="pg8000") as pg:
        settings = Settings(
            database=DatabaseConfig(url=pg.get_connection_url()),
            bootstrap_pat_key_id="exitcriterion",
            bootstrap_pat_secret="faketoken",
        )
        schema_engine = create_engine(settings.database)
        Base.metadata.create_all(schema_engine)
        schema_engine.dispose()

        app = create_app(settings)
        port = _free_port()
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.05)
            assert server.started, "uvicorn did not start within 10 seconds"

            base_url = f"http://127.0.0.1:{port}"
            admin_headers = {"Authorization": "Bearer lakana_pat_exitcriterion_faketoken"}

            requests.post(
                f"{base_url}/api/v1/environments",
                headers=admin_headers,
                json={
                    "name": "dev",
                    "storage": {
                        "scheme": "file",
                        "root": "file:///tmp/lakana-m3b-test",
                        "options": {},
                    },
                },
                timeout=10,
            ).raise_for_status()

            created = requests.post(
                f"{base_url}/api/v1/principals",
                headers=admin_headers,
                json={"name": "alice", "type": "SERVICE_ACCOUNT"},
                timeout=10,
            )
            assert created.status_code == 201, created.text
            issued = requests.post(
                f"{base_url}/api/v1/principals/alice/tokens",
                headers=admin_headers,
                json={"name": "alice-pat"},
                timeout=10,
            )
            alice_headers = {"Authorization": f"Bearer {issued.json()['token']}"}

            # Granted 'analyst' on dev: analyst does not include CREATE_NAMESPACE.
            grant = requests.post(
                f"{base_url}/api/v1/grants",
                headers=admin_headers,
                json={
                    "principal": "alice",
                    "role": "ANALYST",
                    "kind": "ENVIRONMENT",
                    "environment": "dev",
                },
                timeout=10,
            )
            assert grant.status_code == 201, grant.text
            grant_id = grant.json()["id"]

            create_namespace_as_analyst = requests.post(
                f"{base_url}/api/v1/environments/dev/namespaces",
                headers=alice_headers,
                json={"path": "reporting", "properties": {}},
                timeout=10,
            )
            assert create_namespace_as_analyst.status_code == 403

            # Revoke analyst, grant data_engineer instead: CREATE_NAMESPACE now succeeds.
            revoke = requests.delete(
                f"{base_url}/api/v1/grants/{grant_id}", headers=admin_headers, timeout=10
            )
            assert revoke.status_code == 204
            regrant = requests.post(
                f"{base_url}/api/v1/grants",
                headers=admin_headers,
                json={
                    "principal": "alice",
                    "role": "DATA_ENGINEER",
                    "kind": "ENVIRONMENT",
                    "environment": "dev",
                },
                timeout=10,
            )
            assert regrant.status_code == 201, regrant.text

            create_namespace_as_engineer = requests.post(
                f"{base_url}/api/v1/environments/dev/namespaces",
                headers=alice_headers,
                json={"path": "reporting", "properties": {}},
                timeout=10,
            )
            assert create_namespace_as_engineer.status_code == 201, (
                create_namespace_as_engineer.text
            )

            # A task token scoped to a *different* namespace pattern than 'bronze' is refused
            # reading a bronze table, even though alice's own grant (data_engineer on all of
            # dev) would otherwise permit it — narrowing, never expanding, her real access.
            minted = requests.post(
                f"{base_url}/api/v1/task-tokens",
                headers=admin_headers,
                json={
                    "principal": "alice",
                    "environment": "dev",
                    "asset_scope": ["silver.*"],
                    "ttl_seconds": 300,
                },
                timeout=10,
            )
            assert minted.status_code == 201, minted.text
            task_headers = {"Authorization": f"Bearer {minted.json()['token']}"}

            read_out_of_scope = requests.get(
                f"{base_url}/api/v1/environments/dev/namespaces/bronze/tables/orders",
                headers=task_headers,
                timeout=10,
            )
            assert read_out_of_scope.status_code == 403

            # Positive control: a token scoped to bronze is *not* refused there. Authorization
            # passes and the lookup then finds no such table (404, not 403), so the 403 above is
            # the narrowing, not "task tokens are refused everything".
            minted_in_scope = requests.post(
                f"{base_url}/api/v1/task-tokens",
                headers=admin_headers,
                json={
                    "principal": "alice",
                    "environment": "dev",
                    "asset_scope": ["bronze.*"],
                    "ttl_seconds": 300,
                },
                timeout=10,
            )
            assert minted_in_scope.status_code == 201, minted_in_scope.text
            read_in_scope = requests.get(
                f"{base_url}/api/v1/environments/dev/namespaces/bronze/tables/orders",
                headers={"Authorization": f"Bearer {minted_in_scope.json()['token']}"},
                timeout=10,
            )
            assert read_in_scope.status_code == 404, read_in_scope.text

            # The lockout guard: revoking the bootstrap admin's own instance-admin grant is refused.
            admin_grants = requests.get(
                f"{base_url}/api/v1/grants",
                headers=admin_headers,
                params={"principal_name": "admin"},
                timeout=10,
            ).json()
            admin_grant_id = next(g["id"] for g in admin_grants if g["role"] == "ADMIN")
            lockout = requests.delete(
                f"{base_url}/api/v1/grants/{admin_grant_id}", headers=admin_headers, timeout=10
            )
            assert lockout.status_code == 409

            # And deactivating the bootstrap admin principal itself is refused the same way.
            deactivate = requests.delete(
                f"{base_url}/api/v1/principals/admin", headers=admin_headers, timeout=10
            )
            assert deactivate.status_code == 409
        finally:
            server.should_exit = True
            thread.join(timeout=10)
