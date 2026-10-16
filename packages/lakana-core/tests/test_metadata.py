"""Tests for Iceberg metadata construction and IO."""

import uuid
from pathlib import Path

import pytest
from pyiceberg.io.pyarrow import PyArrowFileIO
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.table.update import (
    AddPartitionSpecUpdate,
    AssertTableUUID,
    RemovePropertiesUpdate,
    SetDefaultSpecUpdate,
    SetPropertiesUpdate,
)
from pyiceberg.transforms import IdentityTransform
from pyiceberg.types import LongType, NestedField, StringType

from lakana_core.catalog.metadata import (
    apply_updates,
    build_table_metadata,
    check_requirements,
    next_metadata_location,
    parse_metadata_version,
    read_metadata,
    write_metadata,
)
from lakana_core.errors import ConflictError, LakanaError
from lakana_core.errors import ValidationError as LakanaValidationError


def _schema() -> Schema:
    """Build a two-column schema.

    Returns
    -------
    Schema
        A schema with a required id and an optional name.
    """
    return Schema(
        NestedField(1, "id", LongType(), required=True),
        NestedField(2, "name", StringType(), required=False),
    )


def test_version_of_a_missing_location_is_minus_one() -> None:
    """No previous metadata means the next version is 0."""
    assert parse_metadata_version(None) == -1


def test_version_is_parsed_from_the_file_name() -> None:
    """The version is the numeric prefix of the metadata file name."""
    loc = "file:///w/metadata/00007-2a1f0a1e-0e0a-4a5b-9b6c-1d2e3f4a5b6c.metadata.json"
    assert parse_metadata_version(loc) == 7


def test_a_non_conforming_name_is_minus_one() -> None:
    """An unparseable name must not be guessed at."""
    assert parse_metadata_version("file:///w/metadata/whatever.json") == -1


def test_a_non_uuid_second_field_is_minus_one() -> None:
    """The pattern shape alone is not enough; the UUID must parse.

    pyiceberg's `TABLE_METADATA_FILE_NAME_REGEX` only requires the second
    field to be 36 characters of word-characters and hyphens in the right
    positions — not that it is valid hex. This field has the right shape
    (8-4-4-4-12) but is not hexadecimal, so the regex matches and
    `uuid.UUID` must be the thing that rejects it.
    """
    loc = "file:///w/metadata/00007-gggggggg-gggg-gggg-gggg-gggggggggggg.metadata.json"
    assert parse_metadata_version(loc) == -1


def test_the_first_location_is_version_zero(tmp_path: Path) -> None:
    """A brand-new table starts at 00000."""
    loc = next_metadata_location(
        table_location=f"file://{tmp_path}/t", current_location=None, properties={}
    )
    assert loc.split("/")[-1].startswith("00000-")


def test_the_next_location_increments_the_version(tmp_path: Path) -> None:
    """Each commit advances the version by one."""
    first = next_metadata_location(
        table_location=f"file://{tmp_path}/t", current_location=None, properties={}
    )
    second = next_metadata_location(
        table_location=f"file://{tmp_path}/t", current_location=first, properties={}
    )
    assert second.split("/")[-1].startswith("00001-")


def test_two_writers_at_the_same_version_get_different_files(tmp_path: Path) -> None:
    """Confirm this is what makes write-then-CAS safe.

    Two writers racing on the same table compute the same next version, but
    the file name also carries a fresh UUID, so neither can overwrite the
    other's metadata. The loser of the compare-and-swap leaves an orphaned
    file — recoverable garbage — rather than a corrupted one.
    """
    kwargs = {
        "table_location": f"file://{tmp_path}/t",
        "current_location": None,
        "properties": {},
    }
    assert next_metadata_location(**kwargs) != next_metadata_location(**kwargs)


def test_metadata_round_trips_through_storage(tmp_path: Path) -> None:
    """What is written is what is read back."""
    location = f"file://{tmp_path}/t"
    io = PyArrowFileIO()
    md = build_table_metadata(
        schema=_schema(),
        location=location,
        partition_spec=None,
        sort_order=None,
        properties={},
    )
    target = next_metadata_location(table_location=location, current_location=None, properties={})

    written = write_metadata(io, md, target)
    back = read_metadata(io, written)

    assert written == target
    assert back.table_uuid == md.table_uuid
    assert back.schemas[0] == md.schemas[0]


def test_read_metadata_raises_lakana_error_on_corrupt_document(tmp_path: Path) -> None:
    """A corrupt metadata file must not leak pyiceberg's or pydantic's own type.

    pyiceberg's deserialiser wraps pydantic's validation failure in its own
    `ValidationError`; this is not the caller's fault (unlike a bad update
    from a client), so it becomes `LakanaError` rather than
    `lakana_core.errors.ValidationError`.
    """
    from pydantic import ValidationError as PydanticValidationError
    from pyiceberg.exceptions import ValidationError as IcebergValidationError

    location = f"file://{tmp_path}/t/metadata/00000-x.metadata.json"
    io = PyArrowFileIO()
    with io.new_output(location).create() as f:
        f.write(b'{"format-version": 2, this is not valid json')

    with pytest.raises(LakanaError) as exc_info:
        read_metadata(io, location)

    # Strict identity, not just "an instance of LakanaError": ValidationError is
    # itself a LakanaError subclass, so pytest.raises(LakanaError) alone would not
    # catch a regression to the wrong branch of the hierarchy.
    assert type(exc_info.value) is LakanaError
    assert not isinstance(exc_info.value, IcebergValidationError)
    assert not isinstance(exc_info.value, PydanticValidationError)


def test_apply_updates_returns_a_new_metadata(tmp_path: Path) -> None:
    """Updates are applied functionally, leaving the base untouched."""
    md = build_table_metadata(
        schema=_schema(),
        location=f"file://{tmp_path}/t",
        partition_spec=None,
        sort_order=None,
        properties={},
    )
    updated = apply_updates(md, [SetPropertiesUpdate(updates={"owner": "chris"})])

    assert updated.properties.get("owner") == "chris"
    assert "owner" not in md.properties


def test_apply_updates_raises_lakana_validation_not_pyiceberg(tmp_path: Path) -> None:
    """A structurally invalid update must not escape as a pyiceberg exception.

    `update_table_metadata` validates the resulting partition spec against
    the schema once every update in the batch has been applied. Making a
    partition spec referencing a column id that does not exist the default
    spec triggers that check; pyiceberg raises its own `ValidationError` for
    it. `lakana-server` cannot import pyiceberg (architecture Constraint 2), so
    it could not catch that exception even if it wanted to. Core translates.
    Updates are client-supplied via the REST catalog's commit endpoint, so
    this is a real, not theoretical, path.
    """
    from pyiceberg.exceptions import ValidationError as IcebergValidationError

    md = build_table_metadata(
        schema=_schema(),
        location=f"file://{tmp_path}/t",
        partition_spec=None,
        sort_order=None,
        properties={},
    )
    bogus_spec = PartitionSpec(
        PartitionField(source_id=99, field_id=1000, transform=IdentityTransform(), name="bogus"),
        spec_id=1,
    )

    with pytest.raises(LakanaValidationError) as exc_info:
        apply_updates(
            md,
            [AddPartitionSpecUpdate(spec=bogus_spec), SetDefaultSpecUpdate(spec_id=-1)],
        )

    assert not isinstance(exc_info.value, IcebergValidationError)
    assert exc_info.value.__cause__ is not None


def test_apply_updates_translates_plain_value_error_too(tmp_path: Path) -> None:
    """A malformed update that fails before schema validation is also translated.

    `SetDefaultSpecUpdate` referencing a spec id that was never added raises a
    plain `ValueError` from pyiceberg, distinct from its `ValidationError`.
    Both are client-triggerable through the REST catalog's commit endpoint,
    so both must become `lakana_core.errors.ValidationError`.
    """
    md = build_table_metadata(
        schema=_schema(),
        location=f"file://{tmp_path}/t",
        partition_spec=None,
        sort_order=None,
        properties={},
    )

    with pytest.raises(LakanaValidationError) as exc_info:
        apply_updates(md, [SetDefaultSpecUpdate(spec_id=42)])

    assert exc_info.value.__cause__ is not None


def test_apply_updates_translates_a_key_error_too(tmp_path: Path) -> None:
    """Removing a property that was never set raises `KeyError`, not `ValueError`.

    pyiceberg's `RemovePropertiesUpdate` handler pops each removal off the
    properties dict without checking membership, so a client asking to
    remove an absent key raises `KeyError` — a `LookupError`, unrelated to
    `ValueError`. It is reachable from the REST catalog's commit endpoint by
    any non-pyiceberg client, and by two concurrent writers removing the
    same property, so it must be translated like the rest.
    """
    md = build_table_metadata(
        schema=_schema(),
        location=f"file://{tmp_path}/t",
        partition_spec=None,
        sort_order=None,
        properties={},
    )

    with pytest.raises(LakanaValidationError) as exc_info:
        apply_updates(md, [RemovePropertiesUpdate(removals=["never-set"])])

    assert exc_info.value.__cause__ is not None


def test_a_satisfied_requirement_passes(tmp_path: Path) -> None:
    """A matching requirement is not an error."""
    md = build_table_metadata(
        schema=_schema(),
        location=f"file://{tmp_path}/t",
        partition_spec=None,
        sort_order=None,
        properties={},
    )
    check_requirements(md, [AssertTableUUID(uuid=md.table_uuid)])


def test_a_violated_requirement_raises_lakana_conflict_not_pyiceberg(tmp_path: Path) -> None:
    """Pyiceberg's CommitFailedException must not escape core.

    `lakana-server` cannot import pyiceberg (architecture Constraint 2), so it
    could not catch that exception even if it wanted to. Core translates.
    """
    md = build_table_metadata(
        schema=_schema(),
        location=f"file://{tmp_path}/t",
        partition_spec=None,
        sort_order=None,
        properties={},
    )
    with pytest.raises(ConflictError):
        check_requirements(md, [AssertTableUUID(uuid=uuid.uuid4())])


def test_a_requirement_against_a_missing_table_raises_conflict() -> None:
    """Validating an existing-table requirement against None also translates."""
    with pytest.raises(ConflictError):
        check_requirements(None, [AssertTableUUID(uuid=uuid.uuid4())])


def test_no_pyiceberg_exception_escapes(tmp_path: Path) -> None:
    """Belt and braces: the translated error is not merely a subclass alias."""
    from pyiceberg.exceptions import CommitFailedException

    md = build_table_metadata(
        schema=_schema(),
        location=f"file://{tmp_path}/t",
        partition_spec=None,
        sort_order=None,
        properties={},
    )
    try:
        check_requirements(md, [AssertTableUUID(uuid=uuid.uuid4())])
    except ConflictError as exc:
        assert not isinstance(exc, CommitFailedException)
    else:
        pytest.fail("check_requirements did not raise")
