"""Which policy column a rename touched, judged from the schemas in a table's metadata."""

from __future__ import annotations

from pyiceberg.partitioning import PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.table.metadata import TableMetadata, TableMetadataV2
from pyiceberg.table.sorting import SortOrder
from pyiceberg.types import LongType, NestedField, StringType

from lakana_core.policy.renames import (
    PolicyColumns,
    policy_column_renamed_message,
    renamed_policy_column,
)


def _schema(schema_id: int, *columns: tuple[int, str]) -> Schema:
    """Build a schema of optional string columns after an `id` column.

    Parameters
    ----------
    schema_id
        The schema's id.
    *columns
        Each further column's field id and name.

    Returns
    -------
    Schema
        The schema.
    """
    fields = [NestedField(1, "id", LongType(), required=False)]
    fields += [NestedField(i, n, StringType(), required=False) for i, n in columns]
    return Schema(*fields, schema_id=schema_id)


def _metadata(current: int, *schemas: Schema) -> TableMetadata:
    """Build the metadata of a table with these schemas.

    Parameters
    ----------
    current
        The id of the current schema.
    *schemas
        The schemas, in the order the metadata lists them.

    Returns
    -------
    TableMetadata
        The metadata.
    """
    return TableMetadataV2(
        location="file:///table",
        last_column_id=20,
        schemas=list(schemas),
        current_schema_id=current,
        partition_specs=[PartitionSpec()],
        sort_orders=[SortOrder()],
    )


def _policy(*columns: str, schema_id: int | None) -> PolicyColumns:
    """Build a policy that uses these columns.

    Parameters
    ----------
    *columns
        The columns, as the policy spells them.
    schema_id
        The schema recorded with the policy.

    Returns
    -------
    PolicyColumns
        The policy.
    """
    return PolicyColumns(columns=frozenset(columns), schema_id=schema_id)


def test_a_table_that_never_changed_touches_no_policy() -> None:
    """One schema has nothing to compare."""
    metadata = _metadata(0, _schema(0, (2, "ssn")))
    assert renamed_policy_column(metadata, [_policy("ssn", schema_id=0)]) is None


def test_a_rename_of_a_policy_column_is_found_by_the_old_name() -> None:
    """`ssn` was renamed; nothing took its name."""
    metadata = _metadata(1, _schema(0, (2, "ssn")), _schema(1, (2, "ssn_old")))
    assert renamed_policy_column(metadata, [_policy("ssn", schema_id=0)]) == "ssn"


def test_a_rename_onto_a_policy_column_is_found_by_the_new_name() -> None:
    """`email` became `mail`, a column the policy names (written for a table that had it)."""
    metadata = _metadata(1, _schema(0, (2, "email")), _schema(1, (2, "mail")))
    assert renamed_policy_column(metadata, [_policy("mail", schema_id=0)]) == "mail"


def test_a_rename_of_another_column_touches_nothing() -> None:
    """The policy's column keeps its field id and its name."""
    metadata = _metadata(1, _schema(0, (2, "ssn"), (3, "email")), _schema(1, (2, "ssn"), (3, "e")))
    assert renamed_policy_column(metadata, [_policy("ssn", schema_id=0)]) is None


def test_a_name_that_passed_to_another_field_id_is_found_across_a_gap() -> None:
    """Dropped in one schema and added in the next: no step renames it, the name changed owner."""
    metadata = _metadata(2, _schema(0, (2, "ssn")), _schema(1), _schema(2, (9, "ssn")))
    assert renamed_policy_column(metadata, [_policy("ssn", schema_id=0)]) == "ssn"


def test_names_are_compared_without_regard_to_case() -> None:
    """The database matches column names case-insensitively, so does the rule."""
    metadata = _metadata(1, _schema(0, (2, "SSN")), _schema(1, (2, "ssn_old")))
    assert renamed_policy_column(metadata, [_policy("ssn", schema_id=0)]) == "ssn"


def test_a_rename_before_the_recorded_schema_is_not_the_policys() -> None:
    """Only the schemas from the recorded id on count."""
    metadata = _metadata(1, _schema(0, (2, "old")), _schema(1, (2, "ssn")))
    assert renamed_policy_column(metadata, [_policy("ssn", schema_id=1)]) is None


def test_a_policy_without_a_recorded_schema_starts_at_the_oldest_one() -> None:
    """The same history touches a policy of an earlier release."""
    metadata = _metadata(1, _schema(0, (2, "old")), _schema(1, (2, "ssn")))
    assert renamed_policy_column(metadata, [_policy("ssn", schema_id=None)]) == "ssn"


def test_each_policy_is_judged_from_its_own_schema_and_the_first_name_in_order_is_given() -> None:
    """Two affected policies: the name is the first in sorted order."""
    metadata = _metadata(1, _schema(0, (2, "b"), (3, "a")), _schema(1, (2, "b2"), (3, "a2")))
    policies = [_policy("b", schema_id=0), _policy("a", schema_id=0), _policy("id", schema_id=1)]
    assert renamed_policy_column(metadata, policies) == "a"


def test_no_policy_touches_nothing() -> None:
    """With no policy there is no name to touch."""
    metadata = _metadata(1, _schema(0, (2, "ssn")), _schema(1, (2, "ssn_old")))
    assert renamed_policy_column(metadata, []) is None


def test_the_message_names_the_table_and_the_column_and_no_data() -> None:
    """The text says what happened and what to do next."""
    assert policy_column_renamed_message("ssn", "gold.staff") == (
        "`gold.staff` has a row filter or column mask on column `ssn`, and a column was renamed "
        "since the policy was created, so the policy may now cover a different column. Nothing "
        "is shown. An administrator must review the table's row filters and column masks, "
        "revoke each one that names a renamed column, and create it again for the column it is "
        "meant for."
    )
