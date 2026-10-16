"""Parsing and confinement of `EXTERNAL` table location patterns.

An `EXTERNAL` table's `location_pattern` used to be an unconstrained glob,
matched with `fnmatch` against whatever the storage backend listed under its
directory — a pattern could name any file under the environment's storage
root, including another table's own managed data. This module gives every
raw file exactly one owner: a `location_pattern` must be shaped
`raw/<dir…>/<basename-glob>`, where the directory part (the **claim**) is
exclusive to one table in its environment, and the glob matches only direct
children of that directory, never a subdirectory.
"""

from __future__ import annotations

import fnmatch
import unicodedata
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.errors import ValidationError
from lakana_core.models import Namespace, Table, TableType
from lakana_core.storage.resolver import RAW_PREFIX

_WILDCARD_CHARACTERS = frozenset("*?[")
"""Glob metacharacters `fnmatch` treats specially, refused in a directory component."""

_MAX_CLAIM_LENGTH = 1024
"""`tables.external_claim` is `String(1024)`; a longer claim could never be stored."""


@dataclass(frozen=True)
class ExternalLocation:
    """A parsed, confined `location_pattern`.

    Parameters
    ----------
    claim
        The directory this table exclusively owns, such as
        ``"raw/customers"``. No trailing slash.
    glob
        The basename-only pattern matched within `claim`, such as
        ``"*.parquet"``.
    """

    claim: str
    glob: str

    def matches(self, relative_path: str) -> bool:
        """Check whether a storage-root-relative path belongs to this location.

        Parameters
        ----------
        relative_path
            A path as `Storage.list` returns it, relative to the
            environment's storage root.

        Returns
        -------
        bool
            `True` iff `relative_path`'s parent directory equals `claim`
            exactly and its basename matches `glob`. A file in a
            subdirectory of `claim` never matches, however permissive
            `glob` is, and however the storage backend's `list` behaves —
            recursive or not — this is the authoritative filter.
        """
        directory, _, basename = relative_path.rpartition("/")
        return directory == self.claim and fnmatch.fnmatch(basename, self.glob)


def parse_external_location(pattern: str) -> ExternalLocation:
    """Parse and validate a `location_pattern` into its claim and glob.

    Parameters
    ----------
    pattern
        The caller-supplied pattern, such as ``"raw/customers/*.parquet"``.

    Returns
    -------
    ExternalLocation
        The pattern's directory claim and basename glob.

    Raises
    ------
    ValidationError
        If `pattern` starts with ``/``; contains ``**``; has fewer than
        three ``/``-separated components; has a first component other than
        `RAW_PREFIX`; has an empty, ``.``, or ``..`` component; has a
        wildcard character (``*``, ``?``, ``[``) in a directory component
        (every component except the last, the glob); or the resulting
        claim is longer than 1024 characters, `tables.external_claim`'s
        column width.
    """
    if pattern.startswith("/"):
        raise ValidationError(f"Location pattern {pattern!r} must not start with '/'.")
    if "**" in pattern:
        raise ValidationError(f"Location pattern {pattern!r} must not contain '**'.")
    components = pattern.split("/")
    if len(components) < 3:
        raise ValidationError(
            f"Location pattern {pattern!r} must have the shape '{RAW_PREFIX}/<dir...>/<glob>'."
        )
    for component in components:
        if component in ("", ".", ".."):
            raise ValidationError(
                f"Location pattern {pattern!r} has an invalid path component {component!r}."
            )
    if components[0] != RAW_PREFIX:
        raise ValidationError(f"Location pattern {pattern!r} must start with '{RAW_PREFIX}/'.")
    for component in components[1:-1]:
        if _WILDCARD_CHARACTERS & set(component):
            raise ValidationError(
                f"Location pattern {pattern!r} may not have a wildcard in a "
                f"directory component ({component!r}); only the final, basename "
                "component may be a glob."
            )
    claim = "/".join(components[:-1])
    if len(claim) > _MAX_CLAIM_LENGTH:
        raise ValidationError(
            f"Location pattern {pattern!r} has a claim longer than {_MAX_CLAIM_LENGTH} characters."
        )
    return ExternalLocation(claim=claim, glob=components[-1])


def _normalize_component(component: str) -> str:
    """Fold a path component for case- and normalization-insensitive comparison.

    Applies Unicode's own canonical caseless matching algorithm (Unicode
    Standard Annex #15): NFD, then casefold, then NFD again — not a single
    NFC-then-casefold pass. A single
    NFC-then-casefold pass gets most pairs right but disagrees with the
    canonical algorithm on a handful of characters whose casefolding
    mapping produces a sequence that itself needs re-decomposing — for
    example, a precomposed capital S with acute versus a long s plus a
    combining acute accent fold to visually identical but byte-different
    strings under NFC-then-casefold, and only compare equal once the
    result is normalized to NFD again; see this module's tests for the
    exact character pairs.

    Parameters
    ----------
    component
        A single ``/``-separated piece of a claim.

    Returns
    -------
    str
        `component`, canonically case-folded, so that components differing
        only by letter case or by composed/decomposed accent form compare
        equal — matching how a case-insensitive local filesystem (and, for
        normalization form, macOS's) actually treats directory names.
    """
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", component).casefold())


def claims_overlap(a: str, b: str) -> bool:
    """Check whether two claims conflict.

    Two claims conflict when they are equal, or when one is a
    path-component prefix of the other — never on a shared string prefix
    that stops mid-component, so ``"raw/cust"`` and ``"raw/customers"`` do
    not overlap. Comparison is case- and Unicode-normalization-insensitive
    (see `_normalize_component`), since two claims a real filesystem would
    treat as the same directory must still be caught as overlapping.

    Parameters
    ----------
    a
        The first claim.
    b
        The second claim.

    Returns
    -------
    bool
        `True` if `a` and `b` overlap.
    """
    a_parts = [_normalize_component(part) for part in a.split("/")]
    b_parts = [_normalize_component(part) for part in b.split("/")]
    shorter, longer = (a_parts, b_parts) if len(a_parts) <= len(b_parts) else (b_parts, a_parts)
    return longer[: len(shorter)] == shorter


def check_claim_free(
    session: Session,
    environment_id: uuid.UUID,
    claim: str,
    exclude_table_id: uuid.UUID | None = None,
) -> None:
    """Raise if `claim` conflicts with an existing external table's claim.

    Parameters
    ----------
    session
        Database session.
    environment_id
        The environment to check within — claims are only exclusive within
        one environment.
    claim
        The claim a new (or renamed) external table would hold.
    exclude_table_id
        A table id to ignore, for re-checking a table's own existing claim.
        `None` by default.

    Raises
    ------
    ValidationError
        If `claim` overlaps an existing external table's claim in this
        environment. The message is deliberately generic — it names
        neither the conflicting table nor its claim: naming either would let an unprivileged caller
        learn
        about a table and a directory they have no grant to see, just by
        probing claims. An `EXTERNAL` row whose `external_claim` column is
        `NULL` — a table created before this column was backfilled, or one
        the backfill could not parse — is not treated as claiming nothing:
        its stored `location_pattern` is re-parsed here, and if that
        parses, the resulting claim is compared for overlap too (fail
        closed). A row whose pattern does not parse either can never be
        queried (`lakana_worker.pipeline` refuses any `EXTERNAL` row without
        a matching `external_claim`), so it is skipped rather than
        blocking an unrelated claim forever.

    Notes
    -----
    Rows are scanned in `created_at` order. The overlap decision itself
    does not depend on scan order — every candidate row is checked exactly
    once, and the result ("some row conflicts" or not) is the same either
    way — but a deterministic order is what lets a test pin a specific row
    (a legacy row with a `NULL`/unparseable claim, or an explicitly
    excluded table) to be scanned *before* the row that must still be
    caught after it, so a mutation that turns the loop's `continue` into a
    `break` is reliably killed rather than depending on incidental
    (backend- and insertion-order-dependent) row order.
    """
    rows = session.execute(
        select(Table.id, Table.external_claim, Table.external_config)
        .join(Namespace, Table.namespace_id == Namespace.id)
        .where(
            Namespace.environment_id == environment_id,
            Table.table_type == TableType.EXTERNAL,
        )
        .order_by(Table.created_at)
    ).all()
    for table_id, existing_claim, external_config in rows:
        if exclude_table_id is not None and table_id == exclude_table_id:
            continue
        if existing_claim is None:
            pattern = (external_config or {}).get("location_pattern")
            if not isinstance(pattern, str):
                continue
            try:
                existing_claim = parse_external_location(pattern).claim
            except ValidationError:
                continue
        if claims_overlap(claim, existing_claim):
            raise ValidationError("That directory is already claimed by another table.")
