"""An artifact version as a lineage source and a lineage node."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pytest
from core_test_helpers import ActionAllowList, _bootstrap_principal, _prepared_catalog
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.artifacts.store import current_version, version_row
from lakana_core.auth import Action, AllowAllAuthorization
from lakana_core.catalog.lineage_graph import LineageService
from lakana_core.catalog.service import CatalogService
from lakana_core.catalog.writes import MAX_SOURCE_VERSIONS, check_sources, parse_source_versions
from lakana_core.errors import PermissionDenied, ValidationError
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import ArtifactVersion, LineageEdge, Table, TableType
from lakana_core.storage.resolver import StorageResolver

_GOLD = NamespacePath.from_parts(["gold"])
_MODEL = TableRef("dev", _GOLD, "model")
_SCORES = TableRef("dev", _GOLD, "scores")
_ROWS = pa.table({"id": pa.array([1], pa.int64())})


def _catalog(
    session: Session, tmp_path: Path, versions: tuple[int, ...] = (1, 2)
) -> CatalogService:
    """Build a catalog with a table `gold.scores` and an artifact `gold.model`.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        The environment's storage.
    versions
        Version numbers to insert for the artifact; the last is deleted when
        negative (``-3`` inserts version 3 as deleted).

    Returns
    -------
    CatalogService
        The catalog, allow-all.
    """
    catalog = _prepared_catalog(
        session, tmp_path, AllowAllAuthorization(), namespaces=[_GOLD], tables=[_MODEL, _SCORES]
    )
    row = session.query(Table).filter(Table.name == "model").one()
    row.table_type = TableType.ARTIFACT
    row.metadata_location = None
    now = datetime(2026, 10, 4, tzinfo=UTC)
    for number in versions:
        session.add(
            ArtifactVersion(
                id=uuid.uuid4(),
                artifact_id=row.id,
                version=abs(number),
                manifest_sha256="a" * 64,
                manifest_json={},
                size_bytes=1,
                file_count=1,
                is_directory=False,
                created_at=now,
                deleted_at=now if number < 0 else None,
            )
        )
    session.commit()
    return catalog


def test_current_version_is_the_highest_live_one(session: Session, tmp_path: Path) -> None:
    _catalog(session, tmp_path, versions=(1, 2, -3))
    artifact_id = session.query(Table).filter(Table.name == "model").one().id
    assert current_version(session, artifact_id) == 2
    deleted = version_row(session, artifact_id, 3)
    assert deleted is not None
    assert deleted.deleted_at is not None
    assert version_row(session, artifact_id, 9) is None
    assert current_version(session, uuid.uuid4()) is None


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([], {}),
        (["3:models.churn"], {"models.churn": 3}),
        (["1:a.b", "12:c.d:e"], {"a.b": 1, "c.d:e": 12}),
    ],
)
def test_source_versions_parse(values: list[str], expected: dict[str, int]) -> None:
    assert parse_source_versions(values) == expected


_HOSTILE = [
    ["models.churn"],
    ["x:models.churn"],
    ["-1:a.b"],
    ["+1:a.b"],
    [" 1:a.b"],
    ["1 :a.b"],
    ["1\n:a.b"],
    ["1.0:a.b"],
    ["1e3:a.b"],
    ["0x1:a.b"],
    ["1_0:a.b"],
    ["\uff11:a.b"],
    ["\u0661:a.b"],
    ["3:"],
    [":a.b"],
    [":"],
    [""],
    [f"{'9' * 5000}:a.b"],
    [f"{'1' * 5000}:a.b"],
    ["1:ab"],
    ["1:a."],
    ["1:.b"],
    ["1:a..b"],
    ["1:a/b.c"],
    ["1:a. .c"],
    ["1:a.\x00b"],
    ["1:a.b", "2:a.b"],
    ["1:a.b", "1:a.b"],
    # A name echoed by an error message must be printable and bounded.
    ["1:a.b\nc.d"],
    ["1:a.b\x07"],
    ["1: a.b"],
    ["1:a.b "],
    ["1:a.b\u202e"],
    ["1:a.b\u200b"],
    ["1:a.b\u00a0"],
    ["1:a.b\u2028c"],
    ["1:a.b\ud800"],
    ["1:a.b\ue000"],
    ["1:a.b\u0378"],
    [f"1:{'a' * 5000}.b"],
    [f"1:a.{'b' * 100_000}"],
    [f"1:{'a' * 1024}.{'b' * 256}"],
    [1],
    [None],
    [b"1:a.b"],
]
_VERSION_HINT = (
    " The version is a whole number from 1 to 2147483647, written without leading zeros."
)
_BAD_NUMBERS = ["0:a.b", "03:a.b", "00:a.b", "2147483648:a.b", "99999999999:a.b", "0:models.churn"]


@pytest.mark.parametrize("values", _HOSTILE, ids=lambda v: repr(v)[:40])
def test_a_malformed_source_version_is_refused(values: list[Any]) -> None:
    with pytest.raises(ValidationError) as caught:
        parse_source_versions(values)
    assert str(caught.value) == (
        "source_version must be '<version>:<namespace.name>', for example '3:models.churn_model'."
    )


@pytest.mark.parametrize("value", _BAD_NUMBERS)
def test_a_bad_version_number_says_what_a_version_is(value: str) -> None:
    with pytest.raises(ValidationError) as caught:
        parse_source_versions([value])
    text = str(caught.value)
    assert text == (
        "source_version must be '<version>:<namespace.name>', for example "
        "'3:models.churn_model'." + _VERSION_HINT
    )
    assert value.split(":")[0] not in text


def test_the_longest_legal_name_is_accepted() -> None:
    name = f"{'a' * 1024}.{'b' * 255}"
    assert parse_source_versions([f"1:{name}"])[name] == 1


def test_the_largest_version_and_a_bounded_number_of_values_are_accepted() -> None:
    assert parse_source_versions(["2147483647:a.b"]) == {"a.b": 2147483647}
    many = [f"1:a.n{i}" for i in range(MAX_SOURCE_VERSIONS)]
    assert len(parse_source_versions(many)) == MAX_SOURCE_VERSIONS
    with pytest.raises(ValidationError) as caught:
        parse_source_versions([*many, "1:a.extra"])
    assert str(caught.value) == (
        f"source_version may be given at most {MAX_SOURCE_VERSIONS} times; "
        "name only the artifacts the task read."
    )


def test_names_are_compared_as_resolve_lineage_sources_compares_them() -> None:
    """Names are exact (case-sensitive), so `A.b` and `a.b` are two tables, not a duplicate."""
    assert parse_source_versions(["1:a.b", "2:A.b"]) == {"a.b": 1, "A.b": 2}


def test_an_unreadable_artifact_and_a_missing_version_are_refused_alike(
    session: Session, tmp_path: Path
) -> None:
    _catalog(session, tmp_path)
    p = _bootstrap_principal(session)
    authz = ActionAllowList(*[a for a in Action if a is not Action.READ_TABLE])
    catalog = CatalogService(session, StorageResolver(session), authz)
    messages = set()
    for version in (1, 7):  # one exists, one does not
        with pytest.raises(PermissionDenied) as caught:
            catalog.resolve_lineage_sources(
                p, "dev", ["gold.model"], versions={"gold.model": version}
            )
        messages.add(str(caught.value))
        with pytest.raises(PermissionDenied) as written:
            catalog.replace(
                p, _SCORES, _ROWS, sources=["gold.model"], source_versions={"gold.model": version}
            )
        messages.add(str(written.value))
        with pytest.raises(PermissionDenied) as appended:
            catalog.append(
                p, _SCORES, _ROWS, sources=["gold.model"], source_versions={"gold.model": version}
            )
        messages.add(str(appended.value))
    assert len(messages) == 1
    assert "version" not in messages.pop()
    assert session.query(LineageEdge).count() == 0


def test_an_artifact_in_the_sources_resolves_in_first_seen_order_once(
    session: Session, tmp_path: Path
) -> None:
    catalog = _catalog(session, tmp_path)
    p = _bootstrap_principal(session)
    sources = catalog.resolve_lineage_sources(
        p, "dev", ["gold.model", "gold.model"], versions={"gold.model": 2}
    )
    assert [(s.name, s.snapshot_id) for s in sources] == [("gold.model", 2)]


def test_an_artifact_source_resolves_to_its_current_or_the_named_version(
    session: Session, tmp_path: Path
) -> None:
    catalog = _catalog(session, tmp_path)
    p = _bootstrap_principal(session)
    (current,) = catalog.resolve_lineage_sources(p, "dev", ["gold.model"])
    assert (current.name, current.snapshot_id) == ("gold.model", 2)
    (named,) = catalog.resolve_lineage_sources(p, "dev", ["gold.model"], versions={"gold.model": 1})
    assert named.snapshot_id == 1
    with pytest.raises(ValidationError, match=r"gold\.model has no version 7\."):
        catalog.resolve_lineage_sources(p, "dev", ["gold.model"], versions={"gold.model": 7})
    for versions in ({"gold.scores": 1}, {"gold.other": 1}):
        with pytest.raises(ValidationError, match="is not an artifact among the sources"):
            catalog.resolve_lineage_sources(
                p, "dev", ["gold.model", "gold.scores"], versions=versions
            )


def test_an_empty_artifact_has_no_snapshot_and_a_missing_version_is_refused(
    session: Session, tmp_path: Path
) -> None:
    catalog = _catalog(session, tmp_path, versions=(-1,))
    p = _bootstrap_principal(session)
    (source,) = catalog.resolve_lineage_sources(p, "dev", ["gold.model"])
    assert source.snapshot_id is None
    with pytest.raises(ValidationError, match=r"gold\.model has no version 2\."):
        catalog.resolve_lineage_sources(p, "dev", ["gold.model"], versions={"gold.model": 2})


def test_a_source_version_deleted_since_it_was_read_is_still_recorded(
    session: Session, tmp_path: Path
) -> None:
    """A consumer read version 1, which was pruned while it worked: its lineage names it."""
    catalog = _catalog(session, tmp_path, versions=(-1, 2))
    p = _bootstrap_principal(session)
    (named,) = catalog.resolve_lineage_sources(p, "dev", ["gold.model"], versions={"gold.model": 1})
    assert (named.name, named.snapshot_id) == ("gold.model", 1)


def test_a_table_write_from_a_source_version_deleted_since_is_recorded(
    session: Session, tmp_path: Path
) -> None:
    catalog = _catalog(session, tmp_path, versions=(-1, 2))
    p = _bootstrap_principal(session)
    catalog.replace(p, _SCORES, _ROWS, sources=["gold.model"], source_versions={"gold.model": 1})
    more = pa.table(
        {"id": pa.array([2], pa.int64())},
        schema=_ROWS.schema.set(0, _ROWS.schema.field(0).with_nullable(False)),
    )
    catalog.append(p, _SCORES, more, sources=["gold.model"], source_versions={"gold.model": 1})
    edges = session.scalars(select(LineageEdge)).all()
    assert [(e.source_name, e.source_snapshot_id) for e in edges] == [("gold.model", 1)] * 2


def test_a_table_written_from_an_artifact_records_the_version_it_read(
    session: Session, tmp_path: Path
) -> None:
    """A consumer's snapshot has an edge from the artifact version it read."""
    catalog = _catalog(session, tmp_path)
    p = _bootstrap_principal(session)
    result = catalog.replace(
        p, _SCORES, _ROWS, sources=["gold.model"], source_versions={"gold.model": 1}
    )
    edge = session.scalars(select(LineageEdge)).one()
    assert (edge.source_name, edge.source_snapshot_id, edge.target_snapshot_id) == (
        "gold.model",
        1,
        result.snapshot_id,
    )
    graph = LineageService(session, AllowAllAuthorization()).lineage(p, _SCORES)
    assert {(node.name, node.kind) for node in graph.nodes} == {
        ("gold.scores", "TABLE"),
        ("gold.model", "ARTIFACT"),
    }
    assert [(link.source_snapshot_id, link.target_snapshot_id) for link in graph.edges] == [
        (1, result.snapshot_id)
    ]


def test_a_version_for_an_unreadable_table_is_a_permission_error(
    session: Session, tmp_path: Path
) -> None:
    """The caller learns nothing about whether `gold.scores` is a table or an artifact."""
    _catalog(session, tmp_path)
    p = _bootstrap_principal(session)
    authz = ActionAllowList(*[a for a in Action if a is not Action.READ_TABLE])
    catalog = CatalogService(session, StorageResolver(session), authz)
    texts = set()
    for name in ("gold.scores", "gold.model"):
        with pytest.raises(PermissionDenied) as caught:
            catalog.resolve_lineage_sources(p, "dev", [name], versions={name: 1})
        texts.add(str(caught.value).replace(name, "<name>"))
    assert len(texts) == 1
    assert "not an artifact" not in texts.pop()


def test_can_read_table_asks_the_read_check_without_refusing(
    session: Session, tmp_path: Path
) -> None:
    _catalog(session, tmp_path)
    p = _bootstrap_principal(session)
    allowed = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    assert allowed.can_read_table(p, _MODEL) and allowed.can_read_table(p, _SCORES)
    authz = ActionAllowList(*[a for a in Action if a is not Action.READ_TABLE])
    denied = CatalogService(session, StorageResolver(session), authz)
    assert not denied.can_read_table(p, _MODEL)
    assert not denied.can_read_table(p, TableRef("dev", _GOLD, "nothing"))


def test_sources_are_bounded_in_count_and_length_without_echoing_a_name() -> None:
    """`check_sources` is the one rule for the `source` parameter."""
    check_sources(["gold.sales"] * MAX_SOURCE_VERSIONS)
    check_sources(["g" * 1279 + "x"])
    with pytest.raises(ValidationError, match="at most 64 times"):
        check_sources(["gold.sales"] * (MAX_SOURCE_VERSIONS + 1))
    with pytest.raises(ValidationError) as caught:
        check_sources(["x" * 100_000])
    assert len(str(caught.value)) < 300
    with pytest.raises(ValidationError):
        check_sources(cast("list[str]", [7]))
