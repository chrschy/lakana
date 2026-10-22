"""Schema compatibility between a write's result and its target."""

from __future__ import annotations

import pyarrow as pa
import pytest
from pyiceberg.schema import Schema
from pyiceberg.types import (
    BinaryType,
    BooleanType,
    DateType,
    DecimalType,
    DoubleType,
    FloatType,
    IcebergType,
    IntegerType,
    LongType,
    NestedField,
    StringType,
    TimestampType,
    TimestamptzType,
    TimeType,
    UUIDType,
)

from lakana_core.catalog.json_schema import ColumnSpec
from lakana_core.catalog.schema_compat import (
    ColumnType,
    RenamedColumn,
    RetypedColumn,
    SchemaDiff,
    SchemaMismatchError,
    arrow_to_iceberg_schema,
    arrow_to_iceberg_type,
    diff_schemas,
    is_promotion,
    plan_schema,
    view_columns,
)
from lakana_core.errors import ValidationError


def _schema(*columns: tuple[str, IcebergType, bool]) -> Schema:
    """Build a schema from `(name, type, required)` triples, ids `1..n`.

    Parameters
    ----------
    *columns
        The columns, in order.

    Returns
    -------
    Schema
        The schema, with `schema_id` 0.
    """
    return Schema(
        *(
            NestedField(field_id, name, field_type, required=required)
            for field_id, (name, field_type, required) in enumerate(columns, start=1)
        )
    )


@pytest.mark.parametrize(
    ("arrow_type", "expected"),
    [
        (pa.int8(), IntegerType()),
        (pa.int16(), IntegerType()),
        (pa.int32(), IntegerType()),
        (pa.uint8(), IntegerType()),
        (pa.uint16(), IntegerType()),
        (pa.int64(), LongType()),
        (pa.uint32(), LongType()),
        (pa.float32(), FloatType()),
        (pa.float64(), DoubleType()),
        (pa.bool_(), BooleanType()),
        (pa.string(), StringType()),
        (pa.large_string(), StringType()),
        (pa.string_view(), StringType()),
        (pa.binary(), BinaryType()),
        (pa.large_binary(), BinaryType()),
        (pa.decimal128(38, 0), DecimalType(38, 0)),
        (pa.date32(), DateType()),
        (pa.time64("us"), TimeType()),
        (pa.timestamp("us"), TimestampType()),
        (pa.timestamp("us", tz="UTC"), TimestamptzType()),
        (pa.timestamp("us", tz="+00:00"), TimestamptzType()),
        (pa.uuid(), UUIDType()),
    ],
)
def test_arrow_types_a_table_can_store_map_to_iceberg(
    arrow_type: pa.DataType, expected: IcebergType
) -> None:
    """Every type DuckDB commonly produces maps to its Iceberg type."""
    assert arrow_to_iceberg_type(arrow_type, "c") == expected


@pytest.mark.parametrize(
    "arrow_type",
    [
        pa.uint64(),
        pa.timestamp("ns"),
        pa.timestamp("ms"),
        pa.timestamp("us", tz="Europe/Berlin"),
        pa.list_(pa.int32()),
        pa.struct([pa.field("a", pa.int32())]),
        pa.month_day_nano_interval(),
    ],
)
def test_arrow_types_a_table_cannot_store_fail_naming_the_column(arrow_type: pa.DataType) -> None:
    """An unstorable type fails with the column's name and a CAST hint, not a crash."""
    with pytest.raises(ValidationError, match=r"'amount'.*CAST"):
        arrow_to_iceberg_type(arrow_type, "amount")


def test_an_untyped_null_column_fails_naming_the_cast_to_write() -> None:
    """`SELECT NULL AS x` (Arrow `null` on DuckDB 2) names the CAST that gives it a type."""
    with pytest.raises(ValidationError) as caught:
        arrow_to_iceberg_type(pa.null(), "x")

    assert str(caught.value) == (
        "Column 'x' is NULL without a type, which a Lakana table can't store; "
        "write `CAST(NULL AS <type>) AS x`, for example `CAST(NULL AS VARCHAR)`."
    )


def test_an_untyped_null_column_hint_quotes_a_name_that_needs_it() -> None:
    """The suggested SQL stays valid when the column's name needs quoting."""
    with pytest.raises(ValidationError, match=r'AS "unit price"`'):
        arrow_to_iceberg_type(pa.null(), "unit price")


def test_view_columns_reports_an_untyped_null_column_as_null() -> None:
    """A view over `SELECT NULL AS x` is described, not refused: its column type reads `null`."""
    schema = pa.schema([pa.field("x", pa.null(), nullable=True)])

    assert view_columns(schema) == [ColumnSpec(name="x", type="null", nullable=True)]


def test_view_columns_describes_a_storable_type_by_its_iceberg_name() -> None:
    """A view column a table could also store reports its Iceberg type name."""
    schema = pa.schema([pa.field("id", pa.int64(), nullable=False)])

    assert view_columns(schema) == [ColumnSpec(name="id", type="long", nullable=False)]


def test_view_columns_reports_an_unstorable_type_by_its_arrow_spelling() -> None:
    """A view may output a type no table can store; it is described, not refused."""
    schema = pa.schema([pa.field("tags", pa.list_(pa.int32()), nullable=True)])

    columns = view_columns(schema)

    assert columns == [ColumnSpec(name="tags", type=str(pa.list_(pa.int32())), nullable=True)]


def test_arrow_to_iceberg_schema_numbers_fields_and_keeps_nullability() -> None:
    """Field ids run `1..n`; a non-nullable Arrow field becomes required."""
    schema = arrow_to_iceberg_schema(
        pa.schema([pa.field("id", pa.int64(), nullable=False), pa.field("r", pa.string())])
    )

    assert [(f.field_id, f.name, f.required) for f in schema.fields] == [
        (1, "id", True),
        (2, "r", False),
    ]


def test_arrow_to_iceberg_schema_refuses_a_repeated_column_name() -> None:
    """`SELECT a, a FROM t` can't become a table."""
    with pytest.raises(ValidationError, match="more than once"):
        arrow_to_iceberg_schema(pa.schema([pa.field("a", pa.int64()), pa.field("a", pa.int64())]))


def test_arrow_to_iceberg_schema_refuses_empty_result() -> None:
    """A result with no columns can't define a schema."""
    with pytest.raises(ValidationError, match="no columns"):
        arrow_to_iceberg_schema(pa.schema([]))


@pytest.mark.parametrize(
    ("source", "target", "expected"),
    [
        (IntegerType(), LongType(), True),
        (FloatType(), DoubleType(), True),
        (DecimalType(10, 2), DecimalType(20, 2), True),
        (DecimalType(10, 2), DecimalType(20, 3), False),
        (DecimalType(20, 2), DecimalType(10, 2), False),
        (LongType(), IntegerType(), False),
        (StringType(), LongType(), False),
        (LongType(), LongType(), False),
    ],
)
def test_is_promotion(source: IcebergType, target: IcebergType, expected: bool) -> None:
    """Only the Iceberg-safe widenings the spec names are promotions."""
    assert is_promotion(source, target) is expected


def test_identical_schemas_have_an_empty_diff_and_no_new_schema() -> None:
    """The common case: nothing changes."""
    table = _schema(("id", LongType(), False), ("r", StringType(), False))
    result = _schema(("id", LongType(), False), ("r", StringType(), False))

    plan = plan_schema(
        table, result, evolve=False, last_column_id=2, next_schema_id=1, table_name="gold.t"
    )

    assert plan.schema is None
    assert plan.diff.is_empty()


def test_a_result_type_that_promotes_into_the_table_is_compatible() -> None:
    """Result `int` into a `long` column is cast at write time, not a schema change."""
    table = _schema(("id", LongType(), False))
    result = _schema(("id", IntegerType(), False))

    assert diff_schemas(table, result).is_empty()


def test_a_nullable_result_feeding_a_required_column_is_compatible() -> None:
    """DuckDB marks every result column nullable; nulls are checked per batch."""
    table = _schema(("id", LongType(), True))
    result = _schema(("id", LongType(), False))

    plan = plan_schema(
        table, result, evolve=False, last_column_id=1, next_schema_id=1, table_name="gold.t"
    )

    assert plan.schema is None


def test_an_extra_nullable_result_column_is_added_without_evolve() -> None:
    """Spec: extra result columns are compatible if nullable, and are added."""
    table = _schema(("id", LongType(), False))
    result = _schema(("id", LongType(), False), ("region", StringType(), False))

    plan = plan_schema(
        table, result, evolve=False, last_column_id=4, next_schema_id=3, table_name="gold.t"
    )

    assert plan.schema is not None
    assert plan.schema.schema_id == 3
    assert [(f.field_id, f.name, f.required) for f in plan.schema.fields] == [
        (1, "id", False),
        (5, "region", False),
    ]
    assert plan.diff == SchemaDiff(added=(ColumnType("region", "string"),))


def test_a_required_extra_column_is_refused_without_evolve() -> None:
    """Arrow data can carry a non-nullable extra column; that needs `evolve_schema`."""
    table = _schema(("id", LongType(), False))
    result = _schema(("id", LongType(), False), ("region", StringType(), True))

    with pytest.raises(SchemaMismatchError, match="added region"):
        plan_schema(
            table, result, evolve=False, last_column_id=1, next_schema_id=1, table_name="gold.t"
        )


def test_a_dropped_column_fails_with_a_readable_diff() -> None:
    """Without `evolve_schema` a missing column fails before any write, naming it."""
    table = _schema(("id", LongType(), False), ("region", StringType(), False))
    result = _schema(("id", LongType(), False))

    with pytest.raises(SchemaMismatchError) as caught:
        plan_schema(
            table, result, evolve=False, last_column_id=2, next_schema_id=1, table_name="gold.t"
        )

    assert "gold.t" in str(caught.value)
    assert "dropped region (string)" in str(caught.value)
    assert "--evolve-schema" in str(caught.value)
    assert caught.value.diff.dropped == (ColumnType("region", "string"),)


def test_a_dropped_and_an_added_column_of_the_same_type_look_renamed() -> None:
    """The diff pairs them as renamed-looking, so the message says what probably happened."""
    table = _schema(("id", LongType(), False), ("region", StringType(), False))
    result = _schema(("id", LongType(), False), ("area", StringType(), False))

    diff = diff_schemas(table, result)

    assert diff.renamed == (RenamedColumn("region", "area", "string"),)
    assert diff.added == ()
    assert diff.dropped == ()
    assert "renamed-looking region -> area (string)" in diff.describe()


def test_a_narrowed_or_retyped_column_fails_without_evolve() -> None:
    """Table `int`, result `long` needs the table to widen; that is a schema change."""
    table = _schema(("id", IntegerType(), False), ("r", StringType(), False))
    result = _schema(("id", LongType(), False), ("r", LongType(), False))

    diff = diff_schemas(table, result)

    assert diff.retyped == (
        RetypedColumn("id", "int", "long"),
        RetypedColumn("r", "string", "long"),
    )
    with pytest.raises(SchemaMismatchError, match="retyped id: int -> long"):
        plan_schema(
            table, result, evolve=False, last_column_id=2, next_schema_id=1, table_name="gold.t"
        )


def test_evolve_widens_a_column_in_place_and_retypes_an_incompatible_one_under_a_new_id() -> None:
    """A safe widening keeps its field id; anything else gets a fresh one."""
    table = _schema(("id", IntegerType(), True), ("r", StringType(), False))
    result = _schema(("id", LongType(), False), ("r", LongType(), False))

    plan = plan_schema(
        table, result, evolve=True, last_column_id=7, next_schema_id=2, table_name="gold.t"
    )

    assert plan.schema is not None
    assert [(f.field_id, f.name, f.field_type, f.required) for f in plan.schema.fields] == [
        (1, "id", LongType(), True),
        (8, "r", LongType(), False),
    ]


def test_evolve_adopts_the_result_order_and_drops_missing_columns() -> None:
    """With `evolve_schema` the table takes the result schema in the same commit."""
    table = _schema(("id", LongType(), False), ("gone", StringType(), False))
    result = _schema(("new", DoubleType(), False), ("id", LongType(), False))

    plan = plan_schema(
        table, result, evolve=True, last_column_id=2, next_schema_id=1, table_name="gold.t"
    )

    assert plan.schema is not None
    assert [(f.field_id, f.name) for f in plan.schema.fields] == [(3, "new"), (1, "id")]
    assert plan.diff.to_json() == {
        "added": [{"name": "new", "type": "double"}],
        "dropped": [{"name": "gone", "type": "string"}],
        "renamed": [],
        "retyped": [],
    }
