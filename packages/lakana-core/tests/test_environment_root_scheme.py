"""Tests for refusing a storage root in another scheme than the environment's storage (#260).

fsspec reads ``local://`` and ``local:`` as the working directory and ``memory://`` as the
process's memory, and a ``file`` root on an ``s3`` environment would quietly use the local disk:
a root reaches the storage of its own scheme, whatever scheme the environment was given.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, get_args

import pytest
from sqlalchemy.orm import Session

from lakana_core.artifacts.sweep import sweep_artifacts
from lakana_core.auth import PrincipalRef
from lakana_core.catalog import CatalogService
from lakana_core.catalog.location_sweep import sweep_table_locations
from lakana_core.config import StorageConfig
from lakana_core.errors import ValidationError
from lakana_core.models import Environment
from lakana_core.projects.code_sweep import sweep_code_archives
from lakana_core.storage.resolver import (
    ROOT_SCHEME_EXAMPLES,
    StorageResolver,
    check_root_scheme,
)

Scheme = Literal["file", "s3", "abfs"]
"""The type of `StorageConfig.scheme`."""

SCHEMES = get_args(StorageConfig.model_fields["scheme"].annotation)
"""Every scheme an environment's storage can have."""

MISMATCHES: list[tuple[Scheme, str]] = [
    ("file", "local://"),
    ("file", "local:///"),
    ("file", "local:///data"),
    ("file", "LOCAL:///data"),
    ("file", "asynclocal:///data"),
    ("file", "memory://"),
    ("file", "memory:///data"),
    ("file", "simplecache::file:///data"),
    ("file", "s3://bucket/lake"),
    ("file", "s3a://bucket/lake"),
    ("file", "abfs://container/lake"),
    ("file", "gs://bucket/lake"),
    ("file", "gcs://bucket/lake"),
    ("file", "az://container/lake"),
    ("file", "hdfs://host/lake"),
    ("file", "https://host/lake"),
    ("s3", "file:///data/lake"),
    ("s3", "local:///data"),
    ("s3", "memory://"),
    ("s3", "abfs://container/lake"),
    ("s3", "abfss://container/lake"),
    ("s3", "gs://bucket/lake"),
    ("s3", "oss://bucket/lake"),
    ("abfs", "s3://bucket/lake"),
    ("abfs", "file:///data/lake"),
    ("abfs", "az://container/lake"),
    ("abfs", "memory://"),
]
"""A root in another scheme than the environment's, each spelling of the hazard."""


@pytest.mark.parametrize(("scheme", "root"), MISMATCHES)
def test_a_root_in_another_scheme_is_refused_naming_both_and_a_root_to_use(
    catalog: CatalogService, principal: PrincipalRef, scheme: Scheme, root: str
) -> None:
    """The refusal names the root, its scheme, the storage's, and a root in the storage's scheme."""
    with pytest.raises(ValidationError) as refused:
        catalog.create_environment(
            principal, name="dev", storage=StorageConfig(scheme=scheme, root=root, options={})
        )
    message = str(refused.value)
    assert repr(root) in message
    assert repr(root.partition("://")[0]) in message
    assert f"storage scheme is {scheme!r}" in message
    assert repr(ROOT_SCHEME_EXAMPLES[scheme]) in message
    assert catalog.list_environments(principal) == []


@pytest.mark.parametrize(
    ("scheme", "root", "canonical"),
    [
        ("s3", "s3a://bucket/lake", "s3://bucket/lake"),
        ("s3", "s3n://bucket/lake", "s3://bucket/lake"),
        ("s3", "S3://bucket/lake", "s3://bucket/lake"),
        ("abfs", "abfss://container/lake", "abfs://container/lake"),
        ("abfs", "ABFS://container/lake", "abfs://container/lake"),
        ("file", "FILE:///data/lake", "file:///data/lake"),
    ],
)
def test_an_alias_of_the_storage_scheme_is_its_scheme_and_gets_the_spelling_to_use(
    catalog: CatalogService, principal: PrincipalRef, scheme: Scheme, root: str, canonical: str
) -> None:
    """``s3a``, ``s3n``, ``abfss`` and another case name the storage's scheme: only the spelling."""
    with pytest.raises(ValidationError) as refused:
        catalog.create_environment(
            principal, name="dev", storage=StorageConfig(scheme=scheme, root=root, options={})
        )
    message = str(refused.value)
    assert repr(canonical) in message
    assert "storage scheme" not in message


@pytest.mark.parametrize(
    ("scheme", "root"),
    [("s3", "s3://bucket/lake"), ("s3", "s3://bucket"), ("abfs", "abfs://container/lake")],
)
def test_an_object_store_root_in_its_own_scheme_is_accepted(
    catalog: CatalogService, principal: PrincipalRef, scheme: Scheme, root: str
) -> None:
    """Creating an environment touches no storage, so no bucket is needed."""
    catalog.create_environment(
        principal, name="dev", storage=StorageConfig(scheme=scheme, root=root, options={})
    )
    assert catalog.get_environment(principal, "dev").storage_root == root


def test_a_file_root_in_its_own_scheme_is_accepted(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The ordinary case."""
    root = f"file://{tmp_path}"
    catalog.create_environment(
        principal, name="dev", storage=StorageConfig(scheme="file", root=root, options={})
    )
    assert catalog.get_environment(principal, "dev").storage_root == root


@pytest.mark.parametrize("scheme", SCHEMES)
def test_every_storage_scheme_has_an_example_root_it_accepts(scheme: str) -> None:
    """The rule is total: each scheme the storage can have names a root of its own scheme."""
    check_root_scheme(ROOT_SCHEME_EXAMPLES[scheme], scheme)
    assert set(ROOT_SCHEME_EXAMPLES) == set(SCHEMES)


@pytest.mark.parametrize("root", ["/data/lake", "data", "local:", "file:"])
def test_a_root_without_a_scheme_is_left_to_the_spelling_check(root: str) -> None:
    """`check_storage_root` refuses a root without ``scheme://``; this check has none to compare."""
    check_root_scheme(root, "file")


def _existing_environment(session: Session, name: str, root: str) -> None:
    """Insert an environment as an older release accepted it, root in another scheme.

    Parameters
    ----------
    session
        Database session; commits.
    name
        Its name.
    root
        Its storage root.
    """
    session.add(Environment(id=uuid.uuid4(), name=name, storage_root=root, storage_options={}))
    session.commit()


@pytest.mark.parametrize("root", ["local://", "local:///data", "memory://", "gs://bucket/lake"])
def test_an_existing_root_in_an_unsupported_scheme_fails_closed(
    session: Session,
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root: str,
) -> None:
    """Every storage call refuses it, the sweep skips it, and nothing lands in the working dir."""
    monkeypatch.chdir(tmp_path)
    _existing_environment(session, "old", root)

    with pytest.raises(ValidationError, match="Unsupported storage scheme"):
        StorageResolver(session).for_environment("old")
    with pytest.raises(ValidationError, match="Unsupported storage scheme"):
        catalog.find_orphaned_locations(principal, "old")
    with pytest.raises(ValidationError, match="Unsupported storage scheme"):
        catalog.delete_orphaned_locations(principal, "old")
    report = sweep_table_locations(
        session, StorageResolver(session), now=datetime.now(UTC), grace_hours=24
    )

    resolver = StorageResolver(session)
    artifacts = sweep_artifacts(session, resolver, now=datetime.now(UTC), upload_ttl_seconds=60)
    code = sweep_code_archives(session, resolver, now=datetime.now(UTC))

    assert (report.failed_environments, artifacts.failed_environments) == (1, 1)
    assert code.failed_roots == 1
    assert list(tmp_path.iterdir()) == []
    assert catalog.get_environment(principal, "old").storage_root == root


def test_a_deleted_environment_in_an_unsupported_scheme_is_never_purged(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Its storage cannot be reached, so the sweep skips it and the row and its name stay."""
    _existing_environment(session, "old", "local:///data")
    deleted = catalog.delete_environment(principal, "old", purge=True)
    assert deleted.purge_after is not None

    report = sweep_table_locations(
        session,
        StorageResolver(session),
        now=deleted.purge_after + timedelta(days=2),
        grace_hours=24,
    )

    assert (report.failed_environments, report.environments_purged) == (1, 0)
    assert session.query(Environment).filter_by(name="old").count() == 1
