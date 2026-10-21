"""A new environment's root may not lie in another environment's managed or unrecorded files.

Environments may share a root, and one may be nested elsewhere in another's root (prod at a
bucket's root, dev under ``dev/``). A root at or inside another environment's ``managed/``,
``raw/``, ``artifacts/`` or ``_artifacts/`` is refused when the environment is created: there
the other environment keeps files of its own, and the clean-up could then only keep both
environments' leftovers (`lakana_core.catalog.locations.root_clash`).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.catalog import CatalogService
from lakana_core.catalog.locations import root_clash
from lakana_core.config import StorageConfig
from lakana_core.errors import ValidationError
from lakana_core.models import Environment

ZONES = ["managed", "raw", "artifacts", "_artifacts"]


def _create(catalog: CatalogService, principal: PrincipalRef, name: str, root: str) -> None:
    """Create an environment on a root.

    Parameters
    ----------
    catalog
        The catalog service.
    principal
        The acting principal.
    name
        The environment's name.
    root
        Its storage root; the storage scheme is the root's own.
    """
    scheme = "s3" if root.startswith("s3://") else "file"
    catalog.create_environment(
        principal, name=name, storage=StorageConfig(scheme=scheme, root=root, options={})
    )


def _refusal(catalog: CatalogService, principal: PrincipalRef, name: str, root: str) -> str:
    """Return the message `create_environment` refuses a root with.

    Parameters
    ----------
    catalog
        The catalog service.
    principal
        The acting principal.
    name
        The new environment's name.
    root
        Its storage root.

    Returns
    -------
    str
        The message.
    """
    with pytest.raises(ValidationError) as refused:
        _create(catalog, principal, name, root)
    assert name not in [e.name for e in catalog.list_environments(principal)]
    return str(refused.value)


def _stored(session: Session, name: str, root: str) -> None:
    """Insert an environment row with a root as an older release may have stored it.

    Parameters
    ----------
    session
        Database session.
    name
        The environment's name.
    root
        Its storage root, written in any spelling.
    """
    session.add(
        Environment(
            id=uuid.uuid4(), name=name, storage_root=root, storage_options={}, properties={}
        )
    )
    session.commit()


@pytest.mark.parametrize("zone", ZONES)
@pytest.mark.parametrize("below", ["", "/dev", "/a/b"])
def test_a_root_at_or_inside_another_environments_directory_is_refused(
    catalog: CatalogService, principal: PrincipalRef, zone: str, below: str
) -> None:
    """At ``managed/`` or one of the unrecorded directories, or anywhere inside them."""
    _create(catalog, principal, "prod", "s3://lake")
    message = _refusal(catalog, principal, "dev", f"s3://lake/{zone}{below}")
    assert "'prod'" in message
    assert f"{zone}/" in message
    assert "'s3://lake/dev'" in message


@pytest.mark.parametrize("zone", ZONES)
def test_a_root_that_would_hold_another_environment_in_its_directory_is_refused(
    catalog: CatalogService, principal: PrincipalRef, zone: str
) -> None:
    """The same layout made the other way round: the existing root lies in the new one's."""
    _create(catalog, principal, "dev", f"s3://lake/{zone}/dev")
    message = _refusal(catalog, principal, "prod", "s3://lake")
    assert "'dev'" in message
    assert f"{zone}/" in message
    assert "'s3://lake/prod'" in message


def test_the_message_names_the_cause_and_a_root_of_its_own(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    """It names the other environment, its root, the directory and a root to use instead."""
    _create(catalog, principal, "prod", "s3://lake/")
    message = _refusal(catalog, principal, "dev", "s3://lake/managed/dev")
    assert message == (
        "The storage root 's3://lake/managed/dev' lies inside managed/ of environment 'prod' "
        "(storage root 's3://lake/'), where that environment keeps files of its own; the "
        "clean-up could not tell the two environments' files apart. Give the environment a root "
        "of its own, such as 's3://lake/dev', or share the root of 'prod' exactly."
    )


def test_the_suggestion_is_left_out_when_it_would_be_refused_too(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A name that is itself one of the directories gets the general advice instead."""
    _create(catalog, principal, "prod", "s3://lake")
    message = _refusal(catalog, principal, "raw", "s3://lake/raw/x")
    assert "such as" not in message
    assert "outside the managed/, raw/, artifacts/ and _artifacts/ directories" in message


@pytest.mark.parametrize(
    "stored",
    [
        "S3://lake",
        "s3a://lake",
        "s3://Lake",
        "s3://lake//",
        "s3://l%61ke",
        "s3://lake/./x/..",
    ],
)
def test_another_spelling_of_the_other_root_is_seen_through(
    session: Session, catalog: CatalogService, principal: PrincipalRef, stored: str
) -> None:
    """A root an older release stored in another spelling is compared under every reading."""
    _stored(session, "prod", stored)
    assert "'prod'" in _refusal(catalog, principal, "dev", "s3://lake/managed/dev")


def test_another_case_of_the_directory_is_refused(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A store or disk that ignores case reads ``MANAGED/`` as ``managed/``."""
    _create(catalog, principal, "prod", "s3://lake")
    assert "'prod'" in _refusal(catalog, principal, "dev", "s3://lake/MANAGED/dev")


def test_a_symbolic_link_into_another_environments_directory_is_refused(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """On a local disk the root is also read through its links."""
    (tmp_path / "prod" / "raw").mkdir(parents=True)
    (tmp_path / "link").symlink_to(tmp_path / "prod" / "raw")
    _create(catalog, principal, "prod", f"file://{tmp_path}/prod")
    assert "'prod'" in _refusal(catalog, principal, "dev", f"file://{tmp_path}/link/dev")


def test_a_deleted_environment_still_counts(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Its files stay until they are purged, and it can be restored."""
    _create(catalog, principal, "prod", "s3://lake")
    catalog.delete_environment(principal, "prod")
    message = _refusal(catalog, principal, "dev", "s3://lake/artifacts/dev")
    assert "'prod' (deleted)" in message


@pytest.mark.parametrize(
    "root",
    [
        "s3://lake",
        "s3://lake/",
        "s3://lake/dev",
        "s3://lake/managed-dev",
        "s3://lake/rawdata",
        "s3://lake/teams/raw",
        "s3://other/managed/dev",
        "file:///lake/managed/dev",
    ],
)
def test_a_shared_root_or_one_elsewhere_is_accepted(
    catalog: CatalogService, principal: PrincipalRef, root: str
) -> None:
    """The same root exactly, a sibling of the directories, or another bucket or scheme."""
    _create(catalog, principal, "prod", "s3://lake")
    _create(catalog, principal, "dev", root)
    assert catalog.get_environment(principal, "dev").storage_root == root


def test_a_root_that_holds_another_one_elsewhere_is_accepted(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Prod at the bucket's root around an existing dev under ``dev/``."""
    _create(catalog, principal, "dev", "s3://lake/dev")
    _create(catalog, principal, "prod", "s3://lake")
    assert catalog.get_environment(principal, "prod").storage_root == "s3://lake"


def test_an_unreadable_root_of_an_older_release_does_not_block_a_create(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A relative ``file`` root cannot be compared; the clean-up keeps its files instead."""
    _stored(session, "legacy", "file://data/lake")
    _create(catalog, principal, "dev", "file:///data/lake/managed/dev")
    assert catalog.get_environment(principal, "dev").name == "dev"


def test_an_earlier_environment_at_the_root_of_the_disk_is_compared_too(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """``file:///`` (refused for a new environment now) has its ``managed/`` at ``/managed``."""
    _stored(session, "prod", "file:///")
    assert "'prod'" in _refusal(catalog, principal, "dev", "file:///managed/dev")
    _create(catalog, principal, "dev", "file:///dev")


def test_a_name_that_makes_no_valid_root_gets_the_general_advice(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    """``<root>/<name>`` is only suggested when it is a root Lakana would accept."""
    _create(catalog, principal, "prod", "s3://lake")
    message = _refusal(catalog, principal, "dev[1]", "s3://lake/managed/x")
    assert "such as" not in message
    assert "a root of its own, outside" in message


def test_a_root_that_cannot_be_read_has_no_clash(session: Session) -> None:
    """`check_storage_root` refuses it first; on its own it compares with nothing."""
    _stored(session, "prod", "s3://lake")
    assert root_clash(session, "file://relative/managed") is None
    assert root_clash(session, "s3://lake/raw/x") is not None
