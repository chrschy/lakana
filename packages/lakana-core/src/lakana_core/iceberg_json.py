"""The boundary where Iceberg REST JSON becomes a pyiceberg type.

`lakana-server` may not import `pyiceberg` at all (the `server-goes-through-core`
import-linter contract) — but the Iceberg REST router receives JSON bodies
that are only meaningful once turned into pyiceberg's own types, since
`CatalogService` takes those types directly. This module is the one place
that conversion happens, so the import boundary is real rather than
aspirational.

Every function here raises `pydantic.ValidationError` on malformed input —
not `lakana_core.errors.ValidationError`, and not `pyiceberg.exceptions.ValidationError`,
which are two different classes of the same name. This module does not
translate that exception: its caller is always the Iceberg REST router,
which is what knows the response shape a parse failure should produce.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import TypeAdapter
from pyiceberg.partitioning import PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.table.sorting import SortOrder
from pyiceberg.table.update import TableRequirement, TableUpdate

_TABLE_UPDATES: TypeAdapter[list[TableUpdate]] = TypeAdapter(list[TableUpdate])
_TABLE_REQUIREMENTS: TypeAdapter[list[TableRequirement]] = TypeAdapter(list[TableRequirement])


def parse_schema(data: Mapping[str, Any]) -> Schema:
    """Parse an Iceberg REST wire-format schema.

    Parameters
    ----------
    data
        The `schema` field of a `CreateTableRequest`, as decoded JSON.

    Returns
    -------
    Schema
        The parsed schema.

    Raises
    ------
    pydantic.ValidationError
        If `data` does not match the Iceberg schema shape.
    """
    return Schema.model_validate(data)


def parse_partition_spec(data: Mapping[str, Any]) -> PartitionSpec:
    """Parse an Iceberg REST wire-format partition spec.

    Parameters
    ----------
    data
        The `partition-spec` field of a `CreateTableRequest`, as decoded JSON.

    Returns
    -------
    PartitionSpec
        The parsed partition spec.

    Raises
    ------
    pydantic.ValidationError
        If `data` does not match the Iceberg partition spec shape.
    """
    return PartitionSpec.model_validate(data)


def parse_sort_order(data: Mapping[str, Any]) -> SortOrder:
    """Parse an Iceberg REST wire-format sort order.

    Parameters
    ----------
    data
        The `write-order` field of a `CreateTableRequest`, as decoded JSON.

    Returns
    -------
    SortOrder
        The parsed sort order.

    Raises
    ------
    pydantic.ValidationError
        If `data` does not match the Iceberg sort order shape.
    """
    return SortOrder.model_validate(data)


def parse_table_updates(data: Sequence[Mapping[str, Any]]) -> list[TableUpdate]:
    """Parse the `updates` array of a `CommitTableRequest`.

    `TableUpdate` is a discriminated union (on the `action` field) rather
    than a single pydantic model, so it is parsed with `TypeAdapter` instead
    of `.model_validate()`.

    Parameters
    ----------
    data
        The `updates` field of a `CommitTableRequest`, as decoded JSON.

    Returns
    -------
    list of TableUpdate
        The parsed updates, in the order given.

    Raises
    ------
    pydantic.ValidationError
        If any element does not match a known update shape, including an
        unrecognised `action`.
    """
    return _TABLE_UPDATES.validate_python(list(data))


def parse_table_requirements(data: Sequence[Mapping[str, Any]]) -> list[TableRequirement]:
    """Parse the `requirements` array of a `CommitTableRequest`.

    `TableRequirement` is a discriminated union (on the `type` field), parsed
    the same way as `parse_table_updates`.

    Parameters
    ----------
    data
        The `requirements` field of a `CommitTableRequest`, as decoded JSON.

    Returns
    -------
    list of TableRequirement
        The parsed requirements, in the order given.

    Raises
    ------
    pydantic.ValidationError
        If any element does not match a known requirement shape, including
        an unrecognised `type`.
    """
    return _TABLE_REQUIREMENTS.validate_python(list(data))
