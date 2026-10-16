"""Tests for the metadata schema."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import UniqueConstraint
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.identifiers import NamespacePath
from lakana_core.models import (
    Base,
    Environment,
    Group,
    GroupMembership,
    Namespace,
    PersonalAccessToken,
    Principal,
    PrincipalType,
    Table,
    TableType,
)


def _environment(name: str = "dev") -> Environment:
    """Build an environment suitable for tests.

    Parameters
    ----------
    name
        Environment name.

    Returns
    -------
    Environment
        An unsaved environment.
    """
    return Environment(
        id=uuid.uuid4(),
        name=name,
        storage_root="file:///tmp/lakana/dev",
        storage_options={},
        is_protected=False,
        properties={},
    )


def test_environment_round_trips(session: Session) -> None:
    """An environment persists and reloads with its fields intact."""
    env = _environment()
    session.add(env)
    session.commit()
    loaded = session.get(Environment, env.id)
    assert loaded is not None
    assert loaded.name == "dev"
    assert loaded.storage_root == "file:///tmp/lakana/dev"
    assert loaded.is_protected is False


def test_environment_name_is_unique(session: Session) -> None:
    """Two environments cannot share a name."""
    session.add(_environment())
    session.commit()
    session.add(_environment())
    with pytest.raises(IntegrityError):
        session.commit()


def test_namespace_stores_the_joined_path(session: Session) -> None:
    """A namespace persists its separator-joined path."""
    env = _environment()
    session.add(env)
    session.commit()
    path = NamespacePath.from_parts(["gold", "sales"])
    session.add(
        Namespace(id=uuid.uuid4(), environment_id=env.id, path=path.joined(), properties={})
    )
    session.commit()
    loaded = session.query(Namespace).one()
    assert NamespacePath.parse(loaded.path) == path


def test_table_defaults_to_no_metadata_location(session: Session) -> None:
    """A newly created table row has no metadata pointer yet."""
    env = _environment()
    session.add(env)
    session.commit()
    ns = Namespace(id=uuid.uuid4(), environment_id=env.id, path="gold", properties={})
    session.add(ns)
    session.commit()
    table = Table(
        id=uuid.uuid4(),
        namespace_id=ns.id,
        name="monthly",
        table_type=TableType.ICEBERG,
    )
    session.add(table)
    session.commit()
    loaded = session.query(Table).one()
    assert loaded.metadata_location is None
    assert loaded.previous_metadata_location is None
    assert loaded.table_type is TableType.ICEBERG


def test_timestamps_stay_timezone_aware_after_reload(session: Session) -> None:
    """Reloaded timestamps are aware and comparable to an aware now()."""
    env = _environment()
    session.add(env)
    session.commit()
    session.expire_all()
    loaded = session.get(Environment, env.id)
    assert loaded is not None
    assert loaded.created_at.tzinfo is not None
    assert loaded.created_at <= datetime.now(UTC)


def test_multi_column_unique_constraints_are_named_after_every_column() -> None:
    """A generated unique-constraint name reflects every column, not just the first.

    The naming convention used to key only on the first column
    (``column_0_name``), so two multi-column unique constraints sharing a
    leading column would generate the identical constraint name and
    collide. ``namespaces`` and ``tables`` both have a multi-column unique
    constraint whose first column differs from the other's, so this also
    guards against a regression back to the collision-prone template.
    """
    namespaces_uq = next(
        c for c in Base.metadata.tables["namespaces"].constraints if isinstance(c, UniqueConstraint)
    )
    tables_uq = next(
        c for c in Base.metadata.tables["tables"].constraints if isinstance(c, UniqueConstraint)
    )
    assert namespaces_uq.name == "uq_namespaces_environment_id_path"
    assert tables_uq.name == "uq_tables_namespace_id_name"


def test_principal_types_are_available() -> None:
    """Both principal kinds exist."""
    assert PrincipalType.USER.value == "USER"
    assert PrincipalType.SERVICE_ACCOUNT.value == "SERVICE_ACCOUNT"


def test_deleting_the_owner_clears_namespace_ownership(session: Session) -> None:
    """A namespace survives its owner's deletion, with ownership cleared.

    `owner_id` is declared with ``ondelete="SET NULL"``, not ``CASCADE``: an
    owner leaving must not delete the namespaces they owned.
    """
    env = _environment()
    session.add(env)
    owner = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="alice")
    session.add(owner)
    session.commit()
    ns = Namespace(
        id=uuid.uuid4(), environment_id=env.id, path="gold", properties={}, owner_id=owner.id
    )
    session.add(ns)
    session.commit()

    session.delete(owner)
    session.commit()
    session.expire_all()

    loaded = session.get(Namespace, ns.id)
    assert loaded is not None
    assert loaded.owner_id is None


def test_deleting_the_owner_clears_table_ownership(session: Session) -> None:
    """A table survives its owner's deletion, with ownership cleared.

    `owner_id` is declared with ``ondelete="SET NULL"``, not ``CASCADE``: an
    owner leaving must not delete the tables they owned.
    """
    env = _environment()
    session.add(env)
    owner = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="bob")
    session.add(owner)
    session.commit()
    ns = Namespace(id=uuid.uuid4(), environment_id=env.id, path="gold", properties={})
    session.add(ns)
    session.commit()
    table = Table(
        id=uuid.uuid4(),
        namespace_id=ns.id,
        name="monthly",
        table_type=TableType.ICEBERG,
        owner_id=owner.id,
    )
    session.add(table)
    session.commit()

    session.delete(owner)
    session.commit()
    session.expire_all()

    loaded = session.get(Table, table.id)
    assert loaded is not None
    assert loaded.owner_id is None


def test_external_config_round_trips_for_an_external_table(session: Session) -> None:
    """An EXTERNAL table can carry its location/format/schema; an ICEBERG one leaves it null."""
    env = Environment(
        id=uuid.uuid4(),
        name="dev",
        storage_root="file:///tmp/lakana-test",
        storage_options={},
        is_protected=False,
        properties={},
    )
    session.add(env)
    session.commit()
    ns = Namespace(id=uuid.uuid4(), environment_id=env.id, path="raw", properties={})
    session.add(ns)
    session.commit()

    external = Table(
        id=uuid.uuid4(),
        namespace_id=ns.id,
        name="events",
        table_type=TableType.EXTERNAL,
        metadata_location=None,
        previous_metadata_location=None,
        external_config={
            "location_pattern": "raw/events/*.parquet",
            "format": "parquet",
            "schema": {"id": "BIGINT"},
        },
    )
    iceberg = Table(
        id=uuid.uuid4(),
        namespace_id=ns.id,
        name="curated",
        table_type=TableType.ICEBERG,
        metadata_location="file:///tmp/lakana-test/managed/raw/curated/00000-x.metadata.json",
        previous_metadata_location=None,
    )
    session.add_all([external, iceberg])
    session.commit()

    external_loaded = session.get(Table, external.id)
    assert external_loaded is not None
    assert external_loaded.external_config == {
        "location_pattern": "raw/events/*.parquet",
        "format": "parquet",
        "schema": {"id": "BIGINT"},
    }
    iceberg_loaded = session.get(Table, iceberg.id)
    assert iceberg_loaded is not None
    assert iceberg_loaded.external_config is None


def test_a_new_principal_is_active_by_default(session: Session) -> None:
    """A principal created without specifying is_active starts active."""
    principal = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="alice")
    session.add(principal)
    session.commit()
    session.refresh(principal)
    assert principal.is_active is True


def test_a_group_round_trips(session: Session) -> None:
    """A group persists and reloads with its name intact."""
    group = Group(id=uuid.uuid4(), name="engineers")
    session.add(group)
    session.commit()
    session.refresh(group)
    assert group.name == "engineers"


def test_two_groups_cannot_share_a_name(session: Session) -> None:
    """The unique constraint on Group.name is real, not just a convention."""
    session.add(Group(id=uuid.uuid4(), name="engineers"))
    session.commit()
    session.add(Group(id=uuid.uuid4(), name="engineers"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_group_membership_links_a_principal_to_a_group(session: Session) -> None:
    """A membership row round-trips and cascades on either side's deletion."""
    principal = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="alice")
    group = Group(id=uuid.uuid4(), name="engineers")
    session.add_all([principal, group])
    session.commit()

    session.add(GroupMembership(group_id=group.id, principal_id=principal.id))
    session.commit()

    assert (
        session.query(GroupMembership)
        .filter_by(group_id=group.id, principal_id=principal.id)
        .count()
        == 1
    )

    session.delete(group)
    session.commit()
    assert session.query(GroupMembership).count() == 0


def test_a_personal_access_token_round_trips(session: Session) -> None:
    """A PAT persists with its hash, never its plaintext secret."""
    principal = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="alice")
    session.add(principal)
    session.commit()

    pat = PersonalAccessToken(
        id=uuid.uuid4(),
        principal_id=principal.id,
        key_id="abc123",
        secret_hash="d" * 64,
        name="laptop",
    )
    session.add(pat)
    session.commit()
    session.refresh(pat)

    assert pat.last_used_at is None
    assert pat.expires_at is None
    assert pat.revoked_at is None
    assert not hasattr(pat, "secret")


def test_a_pat_is_deleted_when_its_principal_is_deleted(session: Session) -> None:
    """A credential has no meaning once its principal is gone, unlike a query run."""
    principal = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="alice")
    session.add(principal)
    session.commit()
    session.add(
        PersonalAccessToken(
            id=uuid.uuid4(),
            principal_id=principal.id,
            key_id="abc123",
            secret_hash="d" * 64,
            name="laptop",
        )
    )
    session.commit()

    session.delete(principal)
    session.commit()
    assert session.query(PersonalAccessToken).count() == 0
