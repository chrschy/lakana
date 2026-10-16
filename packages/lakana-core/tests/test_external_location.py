"""Tests for `lakana_core.catalog.external_location`."""

import unicodedata
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from core_test_helpers import _config
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.catalog.external_location import (
    check_claim_free,
    claims_overlap,
    parse_external_location,
)
from lakana_core.catalog.service import CatalogService
from lakana_core.errors import ValidationError
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import Environment, Namespace, Table, TableType


def test_parses_claim_and_glob() -> None:
    """A well-formed pattern splits into its directory claim and basename glob."""
    loc = parse_external_location("raw/customers/*.parquet")
    assert (loc.claim, loc.glob) == ("raw/customers", "*.parquet")


@pytest.mark.parametrize(
    "pattern",
    [
        "managed/gold/customers/*.parquet",
        "_artifacts/queries/x/*.arrow",
        "landing/*.parquet",
        "raw/*.parquet",
        "/raw/a/*.parquet",
        "raw/../managed/x/*.parquet",
        "raw/./a/*.parquet",
        "raw//a/*.parquet",
        "raw/a/**/*.parquet",
        "raw/*/x.parquet",
        "raw/a/b*/x.parquet",
    ],
)
def test_refuses_patterns_outside_the_confined_shape(pattern: str) -> None:
    """Anything outside `raw/<dir…>/<basename-glob>` is refused."""
    with pytest.raises(ValidationError):
        parse_external_location(pattern)


@pytest.mark.parametrize(
    ("pattern", "message"),
    [
        (
            "/raw/a/*.parquet",
            "Location pattern '/raw/a/*.parquet' must not start with '/'.",
        ),
        (
            "raw/a/**/*.parquet",
            "Location pattern 'raw/a/**/*.parquet' must not contain '**'.",
        ),
        (
            "raw/x.parquet",
            "Location pattern 'raw/x.parquet' must have the shape 'raw/<dir...>/<glob>'.",
        ),
        (
            "raw/./a/*.parquet",
            "Location pattern 'raw/./a/*.parquet' has an invalid path component '.'.",
        ),
        (
            "gold/customers/*.parquet",
            "Location pattern 'gold/customers/*.parquet' must start with 'raw/'.",
        ),
    ],
)
def test_refuses_patterns_with_exact_messages(pattern: str, message: str) -> None:
    """Pin each check's exact error text, not just its exception type.

    Several of these inputs would still raise `ValidationError` even if the
    specific check under test were disabled entirely — a later check in the
    sequence catches the same malformed input (e.g. a leading `/` also
    produces an empty `split("/")` component, which the invalid-component
    check would catch on its own). Only the exact message tells a mutant
    that disables or blanks one specific check apart from the real one.
    """
    with pytest.raises(ValidationError) as exc_info:
        parse_external_location(pattern)
    assert str(exc_info.value) == message


def test_wildcard_in_a_directory_component_message_is_exact() -> None:
    """Pin the wildcard-in-directory message's exact text, including its trailing sentence.

    A mutant that only pads or upper-cases the trailing "component may be a
    glob." literal still contains the original substring, so a partial
    (`match=`) check would not catch it — only an exact-equality check does.
    """
    with pytest.raises(ValidationError) as exc_info:
        parse_external_location("raw/*/x.parquet")
    assert str(exc_info.value) == (
        "Location pattern 'raw/*/x.parquet' may not have a wildcard in a "
        "directory component ('*'); only the final, basename component may be a glob."
    )


def test_matches_only_direct_children_of_the_claim() -> None:
    """The glob never reaches into subdirectories."""
    loc = parse_external_location("raw/customers/*.parquet")
    assert loc.matches("raw/customers/a.parquet")
    assert not loc.matches("raw/customers/sub/a.parquet")
    assert not loc.matches("raw/customers2/a.parquet")


def test_sibling_claim_with_shared_string_prefix_is_allowed() -> None:
    """`raw/cust` and `raw/customers` do not overlap — containment is by component."""
    assert not claims_overlap("raw/cust", "raw/customers")
    assert claims_overlap("raw/customers", "raw/customers/eu")
    assert claims_overlap("raw/customers", "raw/customers")


def test_claims_overlap_regardless_of_which_argument_is_longer() -> None:
    """Containment holds whichever claim is passed first — the shorter/longer sort must be real.

    A mutant that always treats the *first* argument as the shorter one
    (instead of actually comparing lengths) gets this backwards whenever
    the caller passes the longer claim first — `longer[:len(shorter)]` then
    compares slices of the wrong lengths and never matches.
    """
    assert claims_overlap("raw/customers/eu", "raw/customers")


def test_check_claim_free_excludes_the_named_table_from_its_own_conflict(
    catalog: CatalogService, principal: PrincipalRef, session: Session, tmp_path: Path
) -> None:
    """`exclude_table_id` lets a table re-check its own claim without self-conflicting."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    (tmp_path / "raw" / "customers").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), tmp_path / "raw" / "customers" / "a.parquet")
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["raw"]), name="customers")
    table = catalog.create_external_table(
        principal, ref, location_pattern="raw/customers/*.parquet", format="parquet"
    )
    ns = session.get(Namespace, table.namespace_id)
    assert ns is not None
    env_id = ns.environment_id

    with pytest.raises(ValidationError):
        check_claim_free(session, env_id, "raw/customers")

    check_claim_free(session, env_id, "raw/customers", exclude_table_id=table.id)


def test_claims_overlap_ignores_case_and_unicode_normalization_form() -> None:
    """A case- or normalization-form difference must not defeat the overlap check.

    Local filesystems are commonly case-insensitive (and macOS's is also
    normalization-insensitive), so two claims that a real filesystem would
    treat as the same directory must be detected as overlapping even when
    their string forms differ only by case or by Unicode composition.
    """
    assert claims_overlap("raw/Customers", "raw/customers")
    nfd = unicodedata.normalize("NFD", "raw/café")
    nfc = unicodedata.normalize("NFC", "raw/café")
    assert nfd != nfc  # sanity: genuinely different byte sequences
    assert claims_overlap(nfd, nfc)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Ś", "ſ́"),  # ruff: ignore[ambiguous-unicode-character-string] -- U+015A LATIN CAPITAL S WITH ACUTE vs. U+017F LATIN SMALL LONG S + U+0301 COMBINING ACUTE ACCENT
        ("ΐ", "Ϊ́"),
    ],
)
def test_claims_overlap_uses_unicode_canonical_caseless_matching(a: str, b: str) -> None:
    """A pair that folds differently under plain NFC+casefold must still overlap.

    `_normalize_component` must apply Unicode's own canonical caseless
    matching algorithm — NFD, casefold, NFD again — not a single
    NFC-then-casefold pass. The first pair is a precomposed capital S with
    acute versus a long s with a combining acute accent, which fold to the
    same string only under the two-pass NFD form; the second is a
    precomposed Greek iota with dialytika and tonos versus a capital iota
    with dialytika plus a combining tonos — the same pair of edge cases the
    Unicode standard's own caseless-matching test data calls out.
    """
    assert claims_overlap(f"raw/{a}", f"raw/{b}")


def test_parse_external_location_refuses_a_claim_longer_than_the_column(
    tmp_path: Path,
) -> None:
    """A claim over 1024 characters is refused: `tables.external_claim` is `String(1024)`."""
    long_pattern = "raw/" + ("x" * 1022) + "/*.parquet"
    assert len(long_pattern.rsplit("/", 1)[0]) > 1024
    with pytest.raises(ValidationError) as exc_info:
        parse_external_location(long_pattern)
    assert str(exc_info.value) == (
        f"Location pattern {long_pattern!r} has a claim longer than 1024 characters."
    )


def test_parse_external_location_allows_a_claim_of_exactly_the_column_width() -> None:
    """The `>` boundary allows a claim of exactly 1024 characters, the column's own width.

    A mutant relaxing this to `>=` would wrongly refuse a claim that fits
    exactly, one character short of where refusal is actually warranted.
    """
    pattern = f"raw/{'x' * 1020}/*.parquet"
    claim = pattern.rsplit("/", 1)[0]
    assert len(claim) == 1024
    loc = parse_external_location(pattern)
    assert loc.claim == claim


def test_check_claim_free_fails_closed_on_a_legacy_row_with_a_null_claim(
    catalog: CatalogService, principal: PrincipalRef, session: Session, tmp_path: Path
) -> None:
    """A older row with `external_claim=None` still blocks an overlapping new claim.

    Reproduces the round-1 gap directly: a row inserted before the
    `external_claim` backfill (or one the backfill could not parse) must
    not be treated as claiming nothing just because the column is `NULL` —
    `check_claim_free` re-parses its stored `location_pattern` and, if it
    parses, still compares it for overlap.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    ns = catalog.load_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    session.add(
        Table(
            id=uuid.uuid4(),
            namespace_id=ns.id,
            name="legacy",
            table_type=TableType.EXTERNAL,
            external_config={
                "location_pattern": "raw/customers/*.parquet",
                "format": "parquet",
                "schema": {},
            },
            external_claim=None,
        )
    )
    session.commit()

    with pytest.raises(ValidationError):
        check_claim_free(session, ns.environment_id, "raw/customers")


def test_check_claim_free_ignores_a_legacy_row_whose_pattern_does_not_parse(
    catalog: CatalogService, principal: PrincipalRef, session: Session, tmp_path: Path
) -> None:
    """A legacy `NULL`-claim row whose stored pattern still doesn't parse claims nothing.

    Such a row can never be queried (the worker refuses any `EXTERNAL` row
    without a matching `external_claim`), so it is safe to treat it as
    claiming nothing here rather than blocking an unrelated new claim
    forever.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    ns = catalog.load_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    session.add(
        Table(
            id=uuid.uuid4(),
            namespace_id=ns.id,
            name="legacy",
            table_type=TableType.EXTERNAL,
            external_config={
                "location_pattern": "gold/customers/*.parquet",
                "format": "parquet",
                "schema": {},
            },
            external_claim=None,
        )
    )
    session.commit()

    check_claim_free(session, ns.environment_id, "raw/customers")


def test_check_claim_free_ignores_a_legacy_row_with_no_external_config_at_all(
    catalog: CatalogService, principal: PrincipalRef, session: Session, tmp_path: Path
) -> None:
    """An `EXTERNAL` row with `external_config=None` (a corrupted or older row) claims nothing.

    Mirrors `lakana_worker.pipeline`'s own handling of this state: such a
    row can never be queried either way, so `check_claim_free` skips it
    rather than raising a `KeyError`/`AttributeError` on the missing
    config.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    ns = catalog.load_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    session.add(
        Table(
            id=uuid.uuid4(),
            namespace_id=ns.id,
            name="broken",
            table_type=TableType.EXTERNAL,
            external_config=None,
            external_claim=None,
        )
    )
    session.commit()

    check_claim_free(session, ns.environment_id, "raw/customers")


def test_check_claim_free_conflict_message_is_generic(
    catalog: CatalogService, principal: PrincipalRef, session: Session, tmp_path: Path
) -> None:
    """The conflict message names neither the conflicting table nor its claim.

    Naming either would let an unprivileged caller learn about a table and
    a directory they have no grant to see, just by probing claims (final
    review M1).
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    (tmp_path / "raw" / "customers").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), tmp_path / "raw" / "customers" / "a.parquet")
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["raw"]), name="customers")
    catalog.create_external_table(
        principal, ref, location_pattern="raw/customers/*.parquet", format="parquet"
    )
    ns = catalog.load_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))

    with pytest.raises(ValidationError) as exc_info:
        check_claim_free(session, ns.environment_id, "raw/customers")
    assert str(exc_info.value) == "That directory is already claimed by another table."
    assert "customers" not in str(exc_info.value)


def test_check_claim_free_trusts_the_stored_claim_over_a_reparsed_one(
    catalog: CatalogService, principal: PrincipalRef, session: Session, tmp_path: Path
) -> None:
    """A resolved `external_claim` is compared directly, never silently reparsed.

    Constructs a deliberately inconsistent row — a stored `external_claim`
    that disagrees with what its own `location_pattern` would parse to — to
    prove the column value wins. A mutant selecting a literal `None` in
    place of `Table.external_claim` would always take the reparse branch
    instead, comparing against the pattern-derived claim rather than the
    real, stored one.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    ns = catalog.load_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    session.add(
        Table(
            id=uuid.uuid4(),
            namespace_id=ns.id,
            name="inconsistent",
            table_type=TableType.EXTERNAL,
            external_claim="raw/stored",
            external_config={
                "location_pattern": "raw/reparsed/*.parquet",
                "format": "parquet",
                "schema": {},
            },
        )
    )
    session.commit()

    with pytest.raises(ValidationError):
        check_claim_free(session, ns.environment_id, "raw/stored")
    check_claim_free(session, ns.environment_id, "raw/reparsed")


def test_check_claim_free_only_compares_claims_within_the_same_environment(
    catalog: CatalogService, principal: PrincipalRef, session: Session, tmp_path: Path
) -> None:
    """An identical claim in a different environment is never a conflict.

    A mutant dropping the `Namespace.environment_id == environment_id`
    filter would compare against every environment's external tables,
    wrongly refusing a claim that lives safely in a completely separate
    one.
    """
    tmp_a = tmp_path / "a"
    tmp_b = tmp_path / "b"
    tmp_a.mkdir()
    tmp_b.mkdir()
    catalog.create_environment(principal, name="env-a", storage=_config(tmp_a))
    catalog.create_environment(principal, name="env-b", storage=_config(tmp_b))
    catalog.create_namespace(principal, "env-a", NamespacePath.from_parts(["raw"]))
    catalog.create_namespace(principal, "env-b", NamespacePath.from_parts(["raw"]))
    (tmp_a / "raw" / "customers").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), tmp_a / "raw" / "customers" / "a.parquet")
    ref_a = TableRef(
        environment="env-a", namespace=NamespacePath.from_parts(["raw"]), name="customers"
    )
    catalog.create_external_table(
        principal, ref_a, location_pattern="raw/customers/*.parquet", format="parquet"
    )
    ns_b = catalog.load_namespace(principal, "env-b", NamespacePath.from_parts(["raw"]))

    check_claim_free(session, ns_b.environment_id, "raw/customers")


def test_check_claim_free_ignores_a_non_external_row_even_with_a_stray_claim(
    catalog: CatalogService, principal: PrincipalRef, session: Session, tmp_path: Path
) -> None:
    """`table_type == EXTERNAL` is enforced; a non-EXTERNAL row is never compared.

    Defense in depth: nothing at the database layer stops an `ICEBERG` row
    from carrying an `external_claim` value, even though application code
    never sets one — a mutant dropping the `table_type` filter would still
    compare it and wrongly report a conflict.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    ns = catalog.load_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    session.add(
        Table(
            id=uuid.uuid4(),
            namespace_id=ns.id,
            name="iceberg_tbl",
            table_type=TableType.ICEBERG,
            external_claim="raw/customers",
        )
    )
    session.commit()

    check_claim_free(session, ns.environment_id, "raw/customers")


def test_check_claim_free_detects_a_self_conflict_with_only_one_namespace_in_the_environment(
    session: Session,
) -> None:
    """A conflict is found even when the environment has exactly one namespace.

    An environment created via `CatalogService.create_environment` always
    seeds three default namespaces (bronze/silver/gold), so a join-condition
    bug that only finds a match because *some other* namespace happens to
    coexist in the environment would go unnoticed through that path.
    Constructing the rows directly isolates the join condition itself: with
    only one namespace total, a join requiring inequality between a table's
    namespace and itself would find nothing at all.
    """
    env_id = uuid.uuid4()
    session.add(Environment(id=env_id, name="solo", storage_root="file:///dev/null"))
    ns_id = uuid.uuid4()
    session.add(Namespace(id=ns_id, environment_id=env_id, path="raw"))
    session.add(
        Table(
            id=uuid.uuid4(),
            namespace_id=ns_id,
            name="customers",
            table_type=TableType.EXTERNAL,
            external_claim="raw/customers",
            external_config={
                "location_pattern": "raw/customers/*.parquet",
                "format": "parquet",
                "schema": {},
            },
        )
    )
    session.commit()

    with pytest.raises(ValidationError, match="already claimed by another table"):
        check_claim_free(session, env_id, "raw/customers")


def test_check_claim_free_exclude_only_skips_the_named_table_not_every_table(
    catalog: CatalogService, principal: PrincipalRef, session: Session, tmp_path: Path
) -> None:
    """Excluding one table's id must not silently exempt every other table too.

    Reproduces two related bugs at once: replacing the `and` in
    `exclude_table_id is not None and table_id == exclude_table_id` with an
    `or` makes the guard true — and thus skips the row — the instant *any*
    `exclude_table_id` is given, regardless of whether that row is the one
    named; replacing the following `continue` with `break` stops checking
    altogether the moment the named table is found, silently ignoring every
    row after it. Two distinct external tables with non-overlapping claims,
    the excluded one inserted (and so examined) first, isolate both bugs:
    excluding the first table must still surface a conflict against the
    second, un-excluded one.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    (tmp_path / "raw" / "a").mkdir(parents=True)
    (tmp_path / "raw" / "b").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), tmp_path / "raw" / "a" / "x.parquet")
    pq.write_table(pa.table({"id": [1]}), tmp_path / "raw" / "b" / "x.parquet")
    ref_a = TableRef(environment="dev", namespace=NamespacePath.from_parts(["raw"]), name="a")
    ref_b = TableRef(environment="dev", namespace=NamespacePath.from_parts(["raw"]), name="b")
    table_a = catalog.create_external_table(
        principal, ref_a, location_pattern="raw/a/*.parquet", format="parquet"
    )
    catalog.create_external_table(
        principal, ref_b, location_pattern="raw/b/*.parquet", format="parquet"
    )
    ns = session.get(Namespace, table_a.namespace_id)
    assert ns is not None

    with pytest.raises(ValidationError):
        check_claim_free(session, ns.environment_id, "raw/b", exclude_table_id=table_a.id)


def test_check_claim_free_checks_every_row_after_a_legacy_non_string_pattern(
    catalog: CatalogService, principal: PrincipalRef, session: Session, tmp_path: Path
) -> None:
    """A legacy row with a non-string (or missing) pattern must not short-circuit the scan.

    The `continue` on such a row must move on to the *next* row, not stop
    the scan entirely (as a `break` mutant would) — a genuinely conflicting
    table inserted after it must still be caught. `created_at` is pinned
    explicitly on both rows, rather than left to each `Table`'s own
    `datetime.now(UTC)` default, so `check_claim_free`'s `ORDER BY
    Table.created_at` deterministically scans `broken` before
    `conflicting` — two defaults evaluated microseconds apart in the same
    flush could otherwise tie or land either way, which would let this
    test pass against a `break` mutant purely by accident of scan order.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    ns = catalog.load_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    first = datetime.now(UTC)
    session.add(
        Table(
            id=uuid.uuid4(),
            namespace_id=ns.id,
            name="broken",
            table_type=TableType.EXTERNAL,
            external_config={"format": "parquet", "schema": {}},
            external_claim=None,
            created_at=first,
        )
    )
    session.add(
        Table(
            id=uuid.uuid4(),
            namespace_id=ns.id,
            name="conflicting",
            table_type=TableType.EXTERNAL,
            external_config={
                "location_pattern": "raw/customers/*.parquet",
                "format": "parquet",
                "schema": {},
            },
            external_claim="raw/customers",
            created_at=first + timedelta(seconds=1),
        )
    )
    session.commit()

    with pytest.raises(ValidationError):
        check_claim_free(session, ns.environment_id, "raw/customers")


def test_check_claim_free_checks_every_row_after_a_legacy_row_that_fails_to_parse(
    catalog: CatalogService, principal: PrincipalRef, session: Session, tmp_path: Path
) -> None:
    """A legacy row whose stored pattern raises on reparse must not short-circuit the scan.

    The `except ValidationError: continue` must move on to the next row,
    not stop the scan entirely (as a `break` mutant would) — a genuinely
    conflicting table inserted after it must still be caught. `created_at`
    is pinned explicitly on both rows, rather than left to each `Table`'s
    own `datetime.now(UTC)` default, so `check_claim_free`'s `ORDER BY
    Table.created_at` deterministically scans `broken` before
    `conflicting` — two defaults evaluated microseconds apart in the same
    flush could otherwise tie or land either way, which would let this
    test pass against a `break` mutant purely by accident of scan order.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    ns = catalog.load_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    first = datetime.now(UTC)
    session.add(
        Table(
            id=uuid.uuid4(),
            namespace_id=ns.id,
            name="broken",
            table_type=TableType.EXTERNAL,
            external_config={
                "location_pattern": "gold/customers/*.parquet",
                "format": "parquet",
                "schema": {},
            },
            external_claim=None,
            created_at=first,
        )
    )
    session.add(
        Table(
            id=uuid.uuid4(),
            namespace_id=ns.id,
            name="conflicting",
            table_type=TableType.EXTERNAL,
            external_config={
                "location_pattern": "raw/customers/*.parquet",
                "format": "parquet",
                "schema": {},
            },
            external_claim="raw/customers",
            created_at=first + timedelta(seconds=1),
        )
    )
    session.commit()

    with pytest.raises(ValidationError):
        check_claim_free(session, ns.environment_id, "raw/customers")
