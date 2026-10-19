"""Tests for revoking grants and policies when their target is dropped.

`revoke_attached` (`lakana_core.grants.revocation`) is called from
`CatalogService.drop_table`, `drop_namespace`, and `delete_environment`.
Most of these tests exercise it through those catalog operations, not by
calling it directly, so a regression in the wiring (a call site removed, or
moved to after the drop's own commit) shows up here rather than only in a
lower-level unit test. The exception is the exactly-one-target contract
(`test_revoke_attached_rejects_*` below): no catalog operation can trigger
zero or two targets, so those call `revoke_attached` directly.
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from core_test_helpers import FakeColumnCatalog, _minimal_schema
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, Securable, SecurableKind
from lakana_core.catalog.service import CatalogService
from lakana_core.config import StorageConfig
from lakana_core.errors import PermissionDenied
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.revocation import revoke_attached
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    ColumnMask,
    Grant,
    Principal,
    PrincipalType,
    RowFilter,
)
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_core.policy.row_filters import RowFilterService


def _config(tmp_path: Path) -> StorageConfig:
    """Build a local storage configuration.

    Returns
    -------
    StorageConfig
        Local-filesystem configuration rooted at `tmp_path`.
    """
    return StorageConfig(scheme="file", root=f"file://{tmp_path}", options={})


def _add_principal(session: Session, name: str) -> PrincipalRef:
    """Insert a principal and return a PrincipalRef for it.

    Returns
    -------
    PrincipalRef
        A reference to the newly persisted principal.
    """
    row = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name=name)
    session.add(row)
    session.commit()
    return PrincipalRef(id=row.id, name=row.name, type=row.type)


def _audit_events(session: Session, event_type: AuditEventType) -> list[AuditEvent]:
    """Fetch every persisted audit event of the given type.

    Returns
    -------
    list of AuditEvent
        Every matching event, in no particular order.
    """
    return list(
        session.execute(select(AuditEvent).where(AuditEvent.event_type == event_type))
        .scalars()
        .all()
    )


def _bound_grant_and_policies(
    session: Session,
    catalog: CatalogService,
    admin: PrincipalRef,
    ref: TableRef,
) -> tuple[PrincipalRef, Grant, RowFilter, ColumnMask]:
    """Create alice, a TABLE grant, a row filter, and a column mask, all bound to `ref`.

    Returns
    -------
    tuple of (PrincipalRef, Grant, RowFilter, ColumnMask)
        Alice, and the three rows bound to the table.
    """
    alice = _add_principal(session, "alice")
    grants = GrantService(session, AllowAllAuthorization())
    grant = grants.grant(
        admin,
        principal="alice",
        action=Action.READ_TABLE,
        kind=SecurableKind.TABLE,
        environment=ref.environment,
        namespace=ref.namespace.joined(),
        table=ref.name,
    )
    row_filters = RowFilterService(session, AllowAllAuthorization(), FakeColumnCatalog())
    row_filter = row_filters.create(
        admin,
        principal="alice",
        environment=ref.environment,
        namespace=ref.namespace.joined(),
        table=ref.name,
        expression="TRUE",
    )
    column_masks = ColumnMaskService(session, AllowAllAuthorization(), FakeColumnCatalog())
    mask = column_masks.create(
        admin,
        principal="alice",
        environment=ref.environment,
        namespace=ref.namespace.joined(),
        table=ref.name,
        column="name",
        expression="NULL",
    )
    return alice, grant, row_filter, mask


def test_drop_table_revokes_its_grants_and_policies(
    session: Session, catalog: CatalogService, tmp_path: Path
) -> None:
    """Dropping a table revokes its TABLE grant, row filter, and column mask, each audited."""
    admin = _add_principal(session, "admin")
    catalog.create_environment(admin, name="dev", storage=_config(tmp_path))
    gold = NamespacePath.from_parts(["gold"])
    ref = TableRef(environment="dev", namespace=gold, name="orders")
    catalog.create_table(admin, ref, schema=_minimal_schema())
    _, grant, row_filter, mask = _bound_grant_and_policies(session, catalog, admin, ref)

    catalog.drop_table(admin, ref)

    for row in (grant, row_filter, mask):
        assert row.revoked_at is not None
        assert row.revoked_by == admin.id

    for event_type, row in (
        (AuditEventType.GRANT_REVOKED, grant),
        (AuditEventType.ROW_FILTER_REVOKED, row_filter),
        (AuditEventType.COLUMN_MASK_REVOKED, mask),
    ):
        events = _audit_events(session, event_type)
        matching = [e for e in events if e.detail.get("id") == str(row.id)]
        assert len(matching) == 1
        assert matching[0].detail["reason"] == "target_dropped"


def test_recreated_table_starts_without_grants(
    session: Session, catalog: CatalogService, tmp_path: Path
) -> None:
    """A table re-created under the same name after a drop starts with no grants."""
    admin = _add_principal(session, "admin")
    catalog.create_environment(admin, name="dev", storage=_config(tmp_path))
    gold = NamespacePath.from_parts(["gold"])
    ref = TableRef(environment="dev", namespace=gold, name="orders")
    catalog.create_table(admin, ref, schema=_minimal_schema())
    alice, _, _, _ = _bound_grant_and_policies(session, catalog, admin, ref)

    catalog.drop_table(admin, ref)
    catalog.create_table(admin, ref, schema=_minimal_schema())

    authz = GrantAuthorization(session)
    with pytest.raises(PermissionDenied):
        authz.authorize(alice, Action.READ_TABLE, Securable.table_scope("dev", gold, "orders"))


def test_drop_namespace_revokes_namespace_grants(
    session: Session, catalog: CatalogService, tmp_path: Path
) -> None:
    """Dropping an empty namespace revokes a NAMESPACE-kind grant bound to it."""
    admin = _add_principal(session, "admin")
    catalog.create_environment(admin, name="dev", storage=_config(tmp_path))
    reports = NamespacePath.from_parts(["reports"])
    catalog.create_namespace(admin, "dev", reports)
    _add_principal(session, "alice")
    grants = GrantService(session, AllowAllAuthorization())
    grant = grants.grant(
        admin,
        principal="alice",
        role=Role.VIEWER,
        kind=SecurableKind.NAMESPACE,
        environment="dev",
        namespace=reports.joined(),
    )

    catalog.drop_namespace(admin, "dev", reports)

    assert grant.revoked_at is not None
    assert grant.revoked_by == admin.id
    events = _audit_events(session, AuditEventType.GRANT_REVOKED)
    matching = [e for e in events if e.detail.get("id") == str(grant.id)]
    assert len(matching) == 1
    assert matching[0].detail["reason"] == "target_dropped"


def test_delete_environment_revokes_every_attached_row(
    session: Session, catalog: CatalogService, tmp_path: Path
) -> None:
    """Deleting an environment revokes every environment/namespace/table grant and policy in it."""
    admin = _add_principal(session, "admin")
    catalog.create_environment(admin, name="dev", storage=_config(tmp_path))
    gold = NamespacePath.from_parts(["gold"])
    ref = TableRef(environment="dev", namespace=gold, name="orders")
    catalog.create_table(admin, ref, schema=_minimal_schema())
    _, table_grant, row_filter, mask = _bound_grant_and_policies(session, catalog, admin, ref)
    grants = GrantService(session, AllowAllAuthorization())
    env_grant = grants.grant(
        admin,
        principal="alice",
        role=Role.VIEWER,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )
    ns_grant = grants.grant(
        admin,
        principal="alice",
        role=Role.VIEWER,
        kind=SecurableKind.NAMESPACE,
        environment="dev",
        namespace=gold.joined(),
    )

    catalog.delete_environment(admin, "dev")

    for row in (env_grant, ns_grant, table_grant, row_filter, mask):
        assert row.revoked_at is not None
        assert row.revoked_by == admin.id

    for event_type, row in (
        (AuditEventType.GRANT_REVOKED, env_grant),
        (AuditEventType.GRANT_REVOKED, ns_grant),
        (AuditEventType.GRANT_REVOKED, table_grant),
        (AuditEventType.ROW_FILTER_REVOKED, row_filter),
        (AuditEventType.COLUMN_MASK_REVOKED, mask),
    ):
        events = _audit_events(session, event_type)
        matching = [e for e in events if e.detail.get("id") == str(row.id)]
        assert len(matching) == 1
        assert matching[0].detail["reason"] == "target_dropped"


def test_delete_environment_revokes_task_tokens_scoped_to_it(
    session: Session, catalog: CatalogService, tmp_path: Path
) -> None:
    """Deleting an environment revokes every unrevoked task token scoped to it by name.

    `TaskToken.environment` is a plain name column, not a foreign key
    — a token minted for "prod" stays usable by name alone unless
    something revokes it when "prod" goes away. Before this fix,
    `delete_environment` left every task token scoped to the deleted
    environment unrevoked, so a worker still holding one could keep
    submitting/running queries against a *re-created* environment of the
    same name, inheriting none of the new environment's grants but still
    presenting as "environment: prod" to anything not checking further.
    A token scoped to a *different* environment is left untouched.
    """
    from lakana_core.identity.task_tokens import TaskTokenService
    from lakana_core.models import TaskToken

    admin = _add_principal(session, "admin")
    _add_principal(session, "worker-1")
    session.add(
        Grant(
            id=uuid.uuid4(),
            principal_id=admin.id,
            role=Role.ADMIN,
            kind=SecurableKind.INSTANCE,
            granted_by=admin.id,
            granted_at=datetime.now(UTC),
        )
    )
    session.commit()
    catalog.create_environment(admin, name="prod", storage=_config(tmp_path / "prod"))
    catalog.create_environment(admin, name="other", storage=_config(tmp_path / "other"))
    task_tokens = TaskTokenService(session, AllowAllAuthorization())
    minted = task_tokens.mint(
        admin, "worker-1", environment="prod", asset_scope=["gold.*"], ttl_seconds=300
    )
    other_minted = task_tokens.mint(
        admin, "worker-1", environment="other", asset_scope=["gold.*"], ttl_seconds=300
    )

    catalog.delete_environment(admin, "prod")

    revoked = session.execute(
        select(TaskToken).where(TaskToken.key_id == minted.key_id)
    ).scalar_one()
    assert revoked.revoked_at is not None
    untouched = session.execute(
        select(TaskToken).where(TaskToken.key_id == other_minted.key_id)
    ).scalar_one()
    assert untouched.revoked_at is None
    events = _audit_events(session, AuditEventType.TASK_TOKEN_REVOKED)
    matching = [e for e in events if e.detail.get("key_id") == minted.key_id]
    assert len(matching) == 1
    assert matching[0].detail["reason"] == "environment_deleted"
    assert matching[0].principal_id == admin.id


def test_drop_after_cross_namespace_rename_revokes_policies(
    session: Session, catalog: CatalogService, tmp_path: Path
) -> None:
    """A mask created before a cross-namespace rename is still revoked when the table is dropped.

    The mask's `table_id` binding follows the rename;
    `revoke_attached` must match on that id, not on whatever
    namespace/table name the row happened to display at creation time.
    """
    admin = _add_principal(session, "admin")
    catalog.create_environment(admin, name="dev", storage=_config(tmp_path))
    gold = NamespacePath.from_parts(["gold"])
    silver = NamespacePath.from_parts(["silver"])
    source = TableRef(environment="dev", namespace=gold, name="orders")
    catalog.create_table(admin, source, schema=_minimal_schema())
    _add_principal(session, "alice")
    column_masks = ColumnMaskService(session, AllowAllAuthorization(), FakeColumnCatalog())
    mask = column_masks.create(
        admin,
        principal="alice",
        environment="dev",
        namespace=gold.joined(),
        table="orders",
        column="name",
        expression="NULL",
    )
    destination = TableRef(environment="dev", namespace=silver, name="orders")
    catalog.rename_table(admin, source, destination)

    catalog.drop_table(admin, destination)

    assert mask.revoked_at is not None
    assert mask.revoked_by == admin.id
    events = _audit_events(session, AuditEventType.COLUMN_MASK_REVOKED)
    matching = [e for e in events if e.detail.get("id") == str(mask.id)]
    assert len(matching) == 1
    assert matching[0].detail["reason"] == "target_dropped"


def test_other_tables_rows_are_untouched(
    session: Session, catalog: CatalogService, tmp_path: Path
) -> None:
    """Dropping one table never revokes another table's grants or policies."""
    admin = _add_principal(session, "admin")
    catalog.create_environment(admin, name="dev", storage=_config(tmp_path))
    gold = NamespacePath.from_parts(["gold"])
    orders_ref = TableRef(environment="dev", namespace=gold, name="orders")
    customers_ref = TableRef(environment="dev", namespace=gold, name="customers")
    catalog.create_table(admin, orders_ref, schema=_minimal_schema())
    catalog.create_table(admin, customers_ref, schema=_minimal_schema())
    _, orders_grant, orders_filter, orders_mask = _bound_grant_and_policies(
        session, catalog, admin, orders_ref
    )
    _add_principal(session, "bob")
    grants = GrantService(session, AllowAllAuthorization())
    customers_grant = grants.grant(
        admin,
        principal="bob",
        action=Action.READ_TABLE,
        kind=SecurableKind.TABLE,
        environment="dev",
        namespace=gold.joined(),
        table="customers",
    )
    row_filters = RowFilterService(session, AllowAllAuthorization(), FakeColumnCatalog())
    customers_filter = row_filters.create(
        admin,
        principal="bob",
        environment="dev",
        namespace=gold.joined(),
        table="customers",
        expression="TRUE",
    )
    column_masks = ColumnMaskService(session, AllowAllAuthorization(), FakeColumnCatalog())
    customers_mask = column_masks.create(
        admin,
        principal="bob",
        environment="dev",
        namespace=gold.joined(),
        table="customers",
        column="name",
        expression="NULL",
    )

    catalog.drop_table(admin, orders_ref)

    for row in (orders_grant, orders_filter, orders_mask):
        assert row.revoked_at is not None
    for row in (customers_grant, customers_filter, customers_mask):
        assert row.revoked_at is None
        assert row.revoked_by is None


def test_already_revoked_row_is_not_re_revoked_or_re_audited(
    session: Session, catalog: CatalogService, tmp_path: Path
) -> None:
    """Dropping a table twice over (via two separate revoke_attached calls) never re-touches a row.

    `_revoke_matching`'s predicate ANDs in `revoked_at.is_(None)`
    specifically so an already-revoked row is skipped. This drives that
    path directly: revoke the same table's grant once via `drop_table`,
    then call `revoke_attached` again for the same `table_id` and confirm
    the row's `revoked_at`/`revoked_by` are unchanged and no second audit
    event is written.
    """
    admin = _add_principal(session, "admin")
    catalog.create_environment(admin, name="dev", storage=_config(tmp_path))
    gold = NamespacePath.from_parts(["gold"])
    ref = TableRef(environment="dev", namespace=gold, name="orders")
    catalog.create_table(admin, ref, schema=_minimal_schema())
    _, grant, _, _ = _bound_grant_and_policies(session, catalog, admin, ref)
    table_id = grant.table_id
    assert table_id is not None

    catalog.drop_table(admin, ref)
    first_revoked_at = grant.revoked_at
    assert first_revoked_at is not None

    second_actor = _add_principal(session, "someone-else")
    revoked = revoke_attached(session, second_actor, table_id=table_id)
    session.commit()

    assert revoked == []
    assert grant.revoked_at == first_revoked_at
    assert grant.revoked_by == admin.id
    events = _audit_events(session, AuditEventType.GRANT_REVOKED)
    matching = [e for e in events if e.detail.get("id") == str(grant.id)]
    assert len(matching) == 1


def test_revoke_attached_rejects_zero_targets(session: Session) -> None:
    """Calling revoke_attached with no target is a ValueError, never a silent no-op."""
    actor = PrincipalRef(id=uuid.uuid4(), name="admin", type=PrincipalType.USER)
    with pytest.raises(ValueError, match="exactly one"):
        revoke_attached(session, actor)


def test_revoke_attached_rejects_two_targets(session: Session) -> None:
    """Calling revoke_attached with more than one target is a ValueError."""
    actor = PrincipalRef(id=uuid.uuid4(), name="admin", type=PrincipalType.USER)
    with pytest.raises(ValueError, match="exactly one"):
        revoke_attached(session, actor, environment_id=uuid.uuid4(), table_id=uuid.uuid4())
