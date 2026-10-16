"""Views in the catalog: creation, authorization, staleness, surfaces."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pyarrow as pa
import pytest
from core_test_helpers import ActionAllowList
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, Securable
from lakana_core.catalog.json_schema import ColumnSpec
from lakana_core.catalog.service import CatalogService
from lakana_core.config import StorageConfig
from lakana_core.errors import AlreadyExistsError, NotFoundError, PermissionDenied, ValidationError
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import AuditEvent, AuditEventType, LineageEdge, Table, TableType, WriteMode
from lakana_core.storage.resolver import StorageResolver

_COLUMNS = [ColumnSpec(name="id", type="long", nullable=True)]


def _ref(dotted: str) -> TableRef:
    """Address a table in `dev`.

    Parameters
    ----------
    dotted
        The dotted name.

    Returns
    -------
    TableRef
        The reference.
    """
    return TableRef.from_dotted("dev", dotted)


@pytest.fixture
def catalog(session: Session, principal: PrincipalRef, tmp_path: Path) -> CatalogService:
    """Provide `dev` with `bronze.orders` and a view `gold.v` over it.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    tmp_path
        The storage root.

    Returns
    -------
    CatalogService
        An allow-all catalog.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    data = pa.table({"id": pa.array([1, 2], pa.int64())})
    catalog.write_batches(
        principal,
        _ref("bronze.orders"),
        mode=WriteMode.CREATE,
        schema=data.schema,
        batches=data.to_batches(),
    )
    catalog.create_or_replace_view(
        principal,
        _ref("gold.v"),
        sql="SELECT id FROM bronze.orders",
        references=catalog.resolve_lineage_sources(principal, "dev", ["bronze.orders"]),
        columns=_COLUMNS,
    )
    return catalog


def test_a_view_is_a_tables_row_with_its_definition(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """SQL, references by id, columns and creator are stored."""
    row = session.execute(select(Table).where(Table.name == "v")).scalar_one()
    orders = session.execute(select(Table).where(Table.name == "orders")).scalar_one()

    assert row.table_type is TableType.VIEW
    assert row.owner_id == principal.id
    assert row.view_definition == {
        "sql": "SELECT id FROM bronze.orders",
        "references": [{"id": str(orders.id), "name": "bronze.orders"}],
        "columns": [{"name": "id", "type": "long", "nullable": True}],
    }


def test_creating_a_view_records_view_to_base_edges_and_an_audit_event(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Views record view → base edges at creation, without snapshot ids."""
    edge = session.execute(select(LineageEdge)).scalar_one()
    event = session.execute(
        select(AuditEvent).where(AuditEvent.securable_table == "v")
    ).scalar_one()

    assert (edge.source_name, edge.target_name) == ("bronze.orders", "gold.v")
    assert (edge.source_snapshot_id, edge.target_snapshot_id) == (None, None)
    assert event.event_type == AuditEventType.TABLE_WRITE
    assert event.detail["mode"] == "view"


def test_replacing_a_view_replaces_its_edges(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """A replaced view's old view → base edges go; the new ones take their place."""
    catalog.create_or_replace_view(
        principal, _ref("gold.v"), sql="SELECT 1 AS id", references=[], columns=_COLUMNS
    )

    assert session.execute(select(LineageEdge)).first() is None
    row = session.execute(select(Table).where(Table.name == "v")).scalar_one()
    assert row.view_definition is not None
    assert row.view_definition["sql"] == "SELECT 1 AS id"


def test_a_new_view_needs_create_table_and_replacing_one_needs_write_table(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Security: CREATE_TABLE for a new view, WRITE_TABLE to replace one."""
    creator = CatalogService(
        session, StorageResolver(session), ActionAllowList(Action.CREATE_TABLE)
    )
    writer = CatalogService(session, StorageResolver(session), ActionAllowList(Action.WRITE_TABLE))

    creator.create_or_replace_view(
        principal, _ref("gold.w"), sql="SELECT 1 AS id", references=[], columns=_COLUMNS
    )
    with pytest.raises(PermissionDenied, match="WRITE_TABLE"):
        creator.create_or_replace_view(
            principal, _ref("gold.v"), sql="SELECT 2 AS id", references=[], columns=_COLUMNS
        )
    with pytest.raises(PermissionDenied, match="CREATE_TABLE"):
        writer.create_or_replace_view(
            principal, _ref("gold.x"), sql="SELECT 3 AS id", references=[], columns=_COLUMNS
        )
    writer.create_or_replace_view(
        principal, _ref("gold.v"), sql="SELECT 4 AS id", references=[], columns=_COLUMNS
    )


def test_a_view_can_not_take_a_tables_name(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Tables and views share one name space."""
    with pytest.raises(AlreadyExistsError, match="is a table"):
        catalog.create_or_replace_view(
            principal, _ref("bronze.orders"), sql="SELECT 1 AS id", references=[], columns=_COLUMNS
        )


def test_views_follow_the_medallion_namespace_rule(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Views live under bronze, silver or gold, like managed tables."""
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["scratch"]))

    with pytest.raises(ValidationError, match="bronze, silver, or gold"):
        catalog.create_or_replace_view(
            principal, _ref("scratch.v"), sql="SELECT 1 AS id", references=[], columns=_COLUMNS
        )


def test_find_view_returns_the_definition_after_authorizing_read(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Reading a view needs READ on the view; tables and unknown names are not views."""
    no_read = CatalogService(session, StorageResolver(session), ActionAllowList())

    view = catalog.find_view(principal, _ref("gold.v"))

    assert view is not None
    assert (view.name, view.sql, view.references) == (
        "gold.v",
        "SELECT id FROM bronze.orders",
        ("bronze.orders",),
    )
    assert catalog.find_view(principal, _ref("bronze.orders")) is None
    assert catalog.find_view(principal, _ref("gold.nope")) is None
    with pytest.raises(PermissionDenied):
        no_read.find_view(principal, _ref("gold.v"))


def test_a_view_over_a_dropped_table_fails_naming_both(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """A stale view names the view and the missing reference; no cascade drop."""
    catalog.drop_table(principal, _ref("bronze.orders"))

    with pytest.raises(ValidationError, match=r"'gold\.v' references 'bronze\.orders'.*dropped"):
        catalog.find_view(principal, _ref("gold.v"))


def test_a_view_over_a_renamed_table_fails_naming_the_new_name(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """A rename keeps the id, so a reader who can also see the new name learns it.

    `principal` here reads under `AllowAllAuthorization` (the `catalog`
    fixture's own authz), so it can read `bronze.orders2` too, and the
    stale message is allowed to name it.
    """
    catalog.rename_table(principal, _ref("bronze.orders"), _ref("bronze.orders2"))

    with pytest.raises(ValidationError, match=r"renamed to 'bronze\.orders2'"):
        catalog.find_view(principal, _ref("gold.v"))


class _DenySecurable:
    """Authorizes everything except one specific securable's `READ_TABLE`.

    Parameters
    ----------
    denied
        The one securable `authorize`/`permits` refuse.
    """

    def __init__(self, denied: Securable) -> None:
        self._denied = denied

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Refuse `READ_TABLE` on the denied securable; permit everything else.

        Parameters
        ----------
        principal
            The acting principal (unused; this stub denies by securable
            alone, not by identity).
        action
            The action requested.
        securable
            The object acted upon.

        Raises
        ------
        PermissionDenied
            If `securable` is the denied one and `action` is `READ_TABLE`.
        """
        if action is Action.READ_TABLE and securable == self._denied:
            raise PermissionDenied("denied")

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Answer as `authorize` would, as a boolean.

        Parameters
        ----------
        principal
            The acting principal.
        action
            The action requested.
        securable
            The object acted upon.

        Returns
        -------
        bool
            `False` if `authorize` would raise, else `True`.
        """
        try:
            self.authorize(principal, action, securable)
        except PermissionDenied:
            return False
        return True


def test_a_view_over_a_renamed_table_the_reader_cant_see_keeps_the_new_name_private(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """READ on the view alone must not reveal a hidden reference's new name.

    A reader who can read `gold.v` but not `bronze.orders2` (its reference,
    renamed) must not learn the new name, or that a rename happened at all
    versus a drop: the message is word for word the one a drop produces.
    """
    catalog.rename_table(principal, _ref("bronze.orders"), _ref("bronze.orders2"))
    denied = Securable.table_scope("dev", NamespacePath.from_parts(["bronze"]), "orders2")
    reader = CatalogService(session, StorageResolver(session), _DenySecurable(denied))

    with pytest.raises(ValidationError, match="dropped or replaced") as renamed:
        reader.find_view(principal, _ref("gold.v"))
    assert "orders2" not in str(renamed.value)

    catalog.drop_table(principal, _ref("bronze.orders2"))
    with pytest.raises(ValidationError) as dropped:
        reader.find_view(principal, _ref("gold.v"))
    assert str(renamed.value) == str(dropped.value)


def test_the_iceberg_surfaces_never_see_a_view(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Views are not exposed over the Iceberg REST catalog.

    `drop_table(include_views=False)` is the Iceberg
    DELETE path's refusal; `table delete`'s own default (`include_views=True`,
    exercised in `test_dropping_a_view_keeps_its_edges`) still drops either
    kind.
    """
    gold = NamespacePath.from_parts(["gold"])

    assert catalog.list_tables(principal, "dev", gold) == [_ref("gold.v")]
    assert catalog.list_tables(principal, "dev", gold, include_views=False) == []
    assert catalog.table_exists(principal, _ref("gold.v")) is False
    with pytest.raises(NotFoundError, match="is a view"):
        catalog.load_table(principal, _ref("gold.v"))
    with pytest.raises(NotFoundError, match="is a view"):
        catalog.rename_table(principal, _ref("gold.v"), _ref("gold.w"))
    with pytest.raises(NotFoundError, match="is a view"):
        catalog.drop_table(principal, _ref("gold.v"), include_views=False)


def test_creating_a_view_races_the_unique_constraint_raises_already_exists(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """A concurrent creator that wins the insert race is still refused.

    A real concurrent writer cannot be exercised against a single
    in-process session, so the race is simulated by patching the pre-check
    to miss the row that is, in fact, already there (`gold.v`, made by the
    `catalog` fixture), forcing the insert itself to hit the
    `(namespace_id, name)` unique constraint — the same pattern
    `test_create_table_race_on_the_unique_constraint_raises_already_exists`
    uses for a managed table.
    """
    with (
        mock.patch("lakana_core.catalog.service.resolve_table_id", return_value=None),
        pytest.raises(AlreadyExistsError, match="already exists"),
    ):
        catalog.create_or_replace_view(
            principal, _ref("gold.v"), sql="SELECT 1 AS id", references=[], columns=_COLUMNS
        )


def test_dropping_a_view_keeps_its_edges(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """`table delete` drops either kind; lineage keeps the history."""
    catalog.drop_table(principal, _ref("gold.v"))

    assert session.execute(select(Table).where(Table.name == "v")).first() is None
    assert session.execute(select(LineageEdge)).scalar_one().target_name == "gold.v"


def test_a_new_view_rejects_a_dotted_name(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """A view name with a dot is refused."""
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="a.b")

    with pytest.raises(ValidationError, match=r"'a\.b'"):
        catalog.create_or_replace_view(
            principal, ref, sql="SELECT 1 AS id", references=[], columns=_COLUMNS
        )
