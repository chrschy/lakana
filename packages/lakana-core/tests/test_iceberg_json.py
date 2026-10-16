"""Tests for the JSON-to-pyiceberg-type boundary."""

import pytest
from pydantic import ValidationError as PydanticValidationError
from pyiceberg.partitioning import PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.table.sorting import SortOrder
from pyiceberg.table.update import AssertTableUUID, SetPropertiesUpdate

from lakana_core.iceberg_json import (
    parse_partition_spec,
    parse_schema,
    parse_sort_order,
    parse_table_requirements,
    parse_table_updates,
)


def test_parse_schema_accepts_a_realistic_wire_payload() -> None:
    """A two-field struct schema, as the Iceberg REST wire format sends it."""
    schema = parse_schema(
        {
            "type": "struct",
            "schema-id": 0,
            "fields": [
                {"id": 1, "name": "id", "type": "long", "required": True},
                {"id": 2, "name": "name", "type": "string", "required": False},
            ],
        }
    )
    assert isinstance(schema, Schema)
    assert [f.name for f in schema.fields] == ["id", "name"]


def test_parse_schema_rejects_a_type_mismatch() -> None:
    """A field of the wrong shape raises pydantic's ValidationError specifically.

    Not lakana_core's ValidationError, and not pyiceberg's — a third, distinct
    class of the same name. The IcebergRoute is what must catch this
    one; this test pins which one it actually is.
    """
    with pytest.raises(PydanticValidationError):
        parse_schema({"type": "struct", "fields": "not-a-list"})


def test_parse_partition_spec_accepts_an_unpartitioned_wire_payload() -> None:
    """The empty partition spec the wire format sends for unpartitioned tables."""
    spec = parse_partition_spec({"spec-id": 0, "fields": []})
    assert isinstance(spec, PartitionSpec)
    assert spec.spec_id == 0


def test_parse_sort_order_accepts_an_unsorted_wire_payload() -> None:
    """The empty sort order the wire format sends for unsorted tables."""
    order = parse_sort_order({"order-id": 0, "fields": []})
    assert isinstance(order, SortOrder)
    assert order.order_id == 0


def test_parse_table_updates_discriminates_on_action() -> None:
    """TableUpdate is a discriminated union; the right concrete type comes back."""
    updates = parse_table_updates([{"action": "set-properties", "updates": {"owner": "chris"}}])
    assert len(updates) == 1
    assert isinstance(updates[0], SetPropertiesUpdate)
    assert updates[0].updates == {"owner": "chris"}


def test_parse_table_updates_rejects_an_unknown_action() -> None:
    """An action the discriminator does not recognise is a validation error."""
    with pytest.raises(PydanticValidationError):
        parse_table_updates([{"action": "not-a-real-action"}])


def test_parse_table_requirements_discriminates_on_type() -> None:
    """TableRequirement is also a discriminated union."""
    requirements = parse_table_requirements(
        [{"type": "assert-table-uuid", "uuid": "11111111-1111-1111-1111-111111111111"}]
    )
    assert len(requirements) == 1
    assert isinstance(requirements[0], AssertTableUUID)


def test_parse_table_requirements_rejects_an_unknown_type() -> None:
    """Same discriminator failure mode as updates."""
    with pytest.raises(PydanticValidationError):
        parse_table_requirements([{"type": "not-a-real-requirement"}])


def test_empty_sequences_produce_empty_lists() -> None:
    """A commit with no requirements or updates is valid, not an error."""
    assert parse_table_updates([]) == []
    assert parse_table_requirements([]) == []
