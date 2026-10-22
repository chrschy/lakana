"""Policy-rewritten queries executed against a real DuckDB connection.

These tests belong to the worker, the only package that may import ``duckdb``. They prove that a
query `lakana_core.query_rewrite` rewrote actually runs and actually masks and filters rows, which
the tests of lakana-core cannot do: they assert on the rewritten SQL text, and the CI gate
`scripts/tests/test_package_test_imports.py` keeps them from importing DuckDB or the worker.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import sqlglot.expressions as exp
from pyiceberg.types import DecimalType, DoubleType, IcebergType, IntegerType, LongType, StringType
from worker_test_helpers import local_prefixes

from lakana_core.errors import ValidationError
from lakana_core.policy.cast_types import arrow_cast_type, iceberg_cast_type
from lakana_core.query_rewrite import (
    ReadField,
    RewrittenQuery,
    TableSource,
    ViewDefinition,
    check_blacklist,
    expand_views,
    rewrite_query,
)
from lakana_worker import query_child
from lakana_worker.executor import execute_query


def _fetch(rewritten: RewrittenQuery) -> list[tuple[object, ...]]:
    """Run a rewritten query on a fresh connection the way the query process does.

    Parameters
    ----------
    rewritten
        The rewrite.

    Returns
    -------
    list of tuple
        The rows.
    """
    connection = duckdb.connect()
    query_child.set_variables(connection, rewritten.parameters)
    return connection.execute(rewritten.sql).fetchall()


def _parquet(*urls: str) -> TableSource:
    """Build a parquet `TableSource` for the given URLs.

    Parameters
    ----------
    *urls
        The resolved URLs the table reads.

    Returns
    -------
    TableSource
        A parquet source over `urls`, with no declared columns (none are
        needed while there is at least one file).
    """
    return TableSource(urls=list(urls), file_format="parquet")


def test_json_execute_serialized_sql_would_otherwise_read_another_files_rows(
    tmp_path: Path,
) -> None:
    """Reproduces why `json_execute_serialized_sql` must be blacklisted.

    `check_blacklist` inspects the *submitted* SQL's own AST for a
    blacklisted function call by name — it never parses into a string
    *argument*. `json_execute_serialized_sql(<json>)` takes a serialized
    query plan (`json_serialize_sql`'s own output) as that string argument
    and runs it as real SQL, so a query naming only
    `json_execute_serialized_sql` in its own text — nothing that looks like
    `read_parquet` to an AST walk — can still read an arbitrary file once
    executed. This proves the underlying mechanism is real, against a raw
    DuckDB connection; blacklisting the function by name is what actually
    closes it, verified by the blacklist tests in lakana-core's
    `test_query_rewrite.py`.
    """
    secret = tmp_path / "secret.parquet"
    pq.write_table(pa.table({"ssn": ["111-22-3333"]}), secret)
    con = duckdb.connect(":memory:")
    serialize_row = con.execute(
        "SELECT json_serialize_sql(?)", [f"SELECT ssn FROM read_parquet('{secret}')"]
    ).fetchone()
    assert serialize_row is not None
    serialized = serialize_row[0]

    rows = con.execute("SELECT * FROM json_execute_serialized_sql(?)", [serialized]).fetchall()

    assert rows == [("111-22-3333",)]
    with pytest.raises(ValidationError, match="json_execute_serialized_sql"):
        check_blacklist("SELECT * FROM json_execute_serialized_sql(?)")


def test_a_column_mask_survives_execution_against_real_duckdb(tmp_path: Path) -> None:
    """A masked column's real value never reaches a fetched row, not merely the SQL text."""
    probe = tmp_path / "customers.parquet"
    pq.write_table(pa.table({"id": [1, 2], "ssn": ["111-22-3333", "444-55-6666"]}), probe)
    source = TableSource(
        urls=[str(probe)],
        file_format="parquet",
        columns={"id": "BIGINT", "ssn": "VARCHAR"},
        column_masks={"ssn": "NULL"},
    )

    rewritten = rewrite_query("SELECT * FROM gold.customers", {"gold.customers": source})
    rows = _fetch(rewritten)

    assert rows == [(1, None), (2, None)]
    assert "111-22-3333" not in str(rows)
    assert "444-55-6666" not in str(rows)


def test_a_row_filter_and_column_mask_both_take_effect_together(tmp_path: Path) -> None:
    """A table with both a row filter and a column mask enforces both at once."""
    probe = tmp_path / "customers.parquet"
    pq.write_table(
        pa.table(
            {
                "id": [1, 2, 3],
                "region": ["EU", "US", "EU"],
                "ssn": ["111-22-3333", "222-33-4444", "333-44-5555"],
            }
        ),
        probe,
    )
    source = TableSource(
        urls=[str(probe)],
        file_format="parquet",
        columns={"id": "BIGINT", "region": "VARCHAR", "ssn": "VARCHAR"},
        row_filters=["region = 'EU'"],
        column_masks={"ssn": "NULL"},
    )

    rewritten = rewrite_query("SELECT * FROM gold.customers", {"gold.customers": source})
    rows = _fetch(rewritten)

    assert sorted(rows) == [(1, "EU", None), (3, "EU", None)]


def test_rewrite_keeps_two_sanitize_colliding_tables_apart_through_duckdb(
    tmp_path: Path,
) -> None:
    """The issue's exact reproduction returns both tables' real rows, not one table twice."""
    first = tmp_path / "first.parquet"
    second = tmp_path / "second.parquet"
    pq.write_table(pa.table({"v": [1]}), first)
    pq.write_table(pa.table({"v": [2]}), second)

    rewritten = rewrite_query(
        "SELECT v FROM a.b_c UNION ALL SELECT v FROM a_b.c",
        sources={"a.b_c": _parquet(str(first)), "a_b.c": _parquet(str(second))},
    )

    rows = _fetch(rewritten)
    assert sorted(rows) == [(1,), (2,)]


def _orders(tmp_path: Path, name: str = "orders") -> Path:
    """Write a small orders file.

    Parameters
    ----------
    tmp_path
        Directory to write into.
    name
        File stem.

    Returns
    -------
    pathlib.Path
        The parquet file, with the columns `id`, `region` and `note`.
    """
    path = tmp_path / f"{name}.parquet"
    pq.write_table(
        pa.table({"id": [1, 2, 3], "region": ["EU", "US", "EU"], "note": ["a", "b", "c"]}), path
    )
    return path


def _run(sql: str, sources: dict[str, TableSource]) -> list[tuple[object, ...]]:
    """Rewrite `sql` and execute it on a fresh DuckDB connection.

    Parameters
    ----------
    sql
        The query as the caller wrote it.
    sources
        The tables it reads.

    Returns
    -------
    list of tuple
        The fetched rows, sorted.
    """
    rewritten = rewrite_query(sql, sources)
    rows = _fetch(rewritten)
    return sorted(rows, key=repr)


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT orders.id FROM gold.orders", [1, 2, 3]),
        ("SELECT gold.orders.id FROM gold.orders", [1, 2, 3]),
        ("SELECT o.id FROM gold.orders o", [1, 2, 3]),
        ("SELECT o.id FROM gold.orders AS o", [1, 2, 3]),
        ('SELECT "orders".id FROM gold.orders', [1, 2, 3]),
        ("SELECT ORDERS.id FROM gold.orders", [1, 2, 3]),
        ("SELECT id FROM gold.orders WHERE gold.orders.region = 'US'", [2]),
    ],
)
def test_a_column_qualified_with_the_table_name_or_alias_runs(
    tmp_path: Path, sql: str, expected: list[int]
) -> None:
    """The caller's own qualifier resolves; it never meets Lakana's internal read name."""
    source = _parquet(str(_orders(tmp_path)))

    rows = _run(sql, {"gold.orders": source})

    assert [row[0] for row in rows] == expected


def test_a_catalog_qualified_column_runs(tmp_path: Path) -> None:
    """A three-part table name keeps all its parts out of the column qualifier."""
    source = _parquet(str(_orders(tmp_path)))

    rows = _run("SELECT gold.sales.orders.id FROM gold.sales.orders", {"gold.sales.orders": source})

    assert rows == [(1,), (2,), (3,)]


def test_a_qualified_column_under_a_filter_and_a_mask(tmp_path: Path) -> None:
    """A masked column reached as `orders.note` is still masked; the filter still applies."""
    source = TableSource(
        urls=[str(_orders(tmp_path))],
        file_format="parquet",
        columns={"id": "BIGINT", "region": "VARCHAR", "note": "VARCHAR"},
        row_filters=["region = 'EU'"],
        column_masks={"note": "NULL"},
    )

    rows = _run(
        "SELECT orders.id, orders.note, gold.orders.note FROM gold.orders", {"gold.orders": source}
    )

    assert rows == [(1, None, None), (3, None, None)]


def test_a_qualified_column_the_read_does_not_list_is_refused_without_the_internal_name(
    tmp_path: Path,
) -> None:
    """A column left out of the declared list fails, and the message names no `__lakana_` read."""
    source = TableSource(
        urls=[str(_orders(tmp_path))],
        file_format="parquet",
        columns={"id": "BIGINT", "region": "VARCHAR"},
        declared_columns_only=True,
    )

    with pytest.raises(duckdb.BinderException) as raised:
        _run("SELECT orders.note FROM gold.orders", {"gold.orders": source})

    # The query echo further down is cut by the query child before a caller sees it.
    first_line = str(raised.value).splitlines()[0]
    assert "__lakana_" not in first_line
    assert '"note"' in first_line


def test_a_join_of_two_tables_with_one_column_name_resolves_each_qualifier(
    tmp_path: Path,
) -> None:
    """`a.id` and `b.id` each reach their own table."""
    first = tmp_path / "first.parquet"
    second = tmp_path / "second.parquet"
    pq.write_table(pa.table({"id": [1, 2], "x": ["p", "q"]}), first)
    pq.write_table(pa.table({"id": [2, 3], "y": ["r", "s"]}), second)
    sources = {"gold.a": _parquet(str(first)), "silver.b": _parquet(str(second))}

    rows = _run("SELECT a.id, b.id, x, y FROM gold.a JOIN silver.b ON a.id = b.id", sources)
    qualified = _run(
        "SELECT gold.a.id, silver.b.id FROM gold.a JOIN silver.b ON gold.a.id = silver.b.id",
        sources,
    )

    assert rows == [(2, 2, "q", "r")]
    assert qualified == [(2, 2)]


def test_two_tables_of_one_name_in_different_schemas_still_run_with_aliases(
    tmp_path: Path,
) -> None:
    """Giving the tables aliases keeps working when both are called `orders`."""
    first = _orders(tmp_path, "first")
    second = _orders(tmp_path, "second")
    sources = {"gold.orders": _parquet(str(first)), "silver.orders": _parquet(str(second))}

    rows = _run("SELECT g.id FROM gold.orders g JOIN silver.orders s ON g.id = s.id", sources)

    assert rows == [(1,), (2,), (3,)]


def _two_orders(tmp_path: Path) -> dict[str, TableSource]:
    """Build two tables both called `orders`, in `gold` and `silver`.

    Parameters
    ----------
    tmp_path
        Directory to write into.

    Returns
    -------
    dict of str to TableSource
        The sources; the gold one has the ids 1 to 3, the silver one 2 to 4.
    """
    first = tmp_path / "gold.parquet"
    second = tmp_path / "silver.parquet"
    pq.write_table(pa.table({"id": [1, 2, 3], "g": ["a", "b", "c"]}), first)
    pq.write_table(pa.table({"id": [2, 3, 4], "s": ["x", "y", "z"]}), second)
    return {"gold.orders": _parquet(str(first)), "silver.orders": _parquet(str(second))}


def test_two_unaliased_tables_of_one_name_keep_working_without_qualifiers(tmp_path: Path) -> None:
    """The join worked before columns could be qualified, and it still does."""
    rows = _run(
        "SELECT g, s FROM gold.orders JOIN silver.orders ON g = g OR s = s WHERE g = 'b'",
        _two_orders(tmp_path),
    )

    assert rows == [("b", "x"), ("b", "y"), ("b", "z")]


def test_two_tables_of_one_name_fail_loudly_when_a_column_is_qualified(tmp_path: Path) -> None:
    """Neither `orders.id` nor `gold.orders.id` may bind to one of the two: the database refuses."""
    sources = _two_orders(tmp_path)

    for sql in (
        "SELECT orders.id FROM gold.orders JOIN silver.orders ON true",
        "SELECT gold.orders.id FROM gold.orders JOIN silver.orders ON true",
    ):
        with pytest.raises(duckdb.BinderException):
            _run(sql, sources)


def _gold_and_silver(tmp_path: Path) -> dict[str, TableSource]:
    """Build `gold.orders` (ids 1, 2, 3) and `silver.orders` (ids 3, 4) with a `region`.

    Parameters
    ----------
    tmp_path
        Directory to write into.

    Returns
    -------
    dict of str to TableSource
        The sources.
    """
    first = tmp_path / "gold.parquet"
    second = tmp_path / "silver.parquet"
    pq.write_table(pa.table({"id": [1, 2, 3], "region": ["EU", "EU", "US"]}), first)
    pq.write_table(pa.table({"id": [3, 4], "region": ["US", "EU"]}), second)
    return {"gold.orders": _parquet(str(first)), "silver.orders": _parquet(str(second))}


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            "SELECT id FROM gold.orders WHERE EXISTS "
            "(SELECT 1 FROM silver.orders WHERE silver.orders.id = gold.orders.id)",
            [(3,)],
        ),
        (
            "SELECT id FROM gold.orders WHERE NOT EXISTS "
            "(SELECT 1 FROM silver.orders WHERE silver.orders.id = gold.orders.id)",
            [(1,), (2,)],
        ),
        (
            "SELECT id FROM gold.orders WHERE id IN "
            "(SELECT id FROM silver.orders WHERE silver.orders.region = gold.orders.region)",
            [(3,)],
        ),
        (
            "SELECT id, (SELECT count(*) FROM silver.orders "
            "WHERE silver.orders.id = gold.orders.id) AS n FROM gold.orders",
            [(1, 0), (2, 0), (3, 1)],
        ),
    ],
)
def test_a_correlated_subquery_over_two_tables_of_one_name_is_right_or_refused(
    tmp_path: Path, sql: str, expected: list[tuple[object, ...]]
) -> None:
    """The same bare name in an inner and an outer scope never makes both sides one table."""
    try:
        rows = _run(sql, _gold_and_silver(tmp_path))
    except duckdb.BinderException:
        return

    assert rows == expected


def test_a_correlated_subquery_over_an_inner_cte_named_like_the_table_is_right_or_refused(
    tmp_path: Path,
) -> None:
    """An inner CTE called `orders` is another source with that name."""
    sql = (
        "SELECT id FROM gold.orders WHERE EXISTS "
        "(WITH orders AS (SELECT 3 AS id) SELECT 1 FROM orders WHERE orders.id = gold.orders.id)"
    )
    try:
        rows = _run(sql, _gold_and_silver(tmp_path))
    except duckdb.BinderException:
        return

    assert rows == [(3,)]


def test_a_derived_table_named_like_the_table_never_gets_the_tables_rows(tmp_path: Path) -> None:
    """`orders` is both a derived table and a read: no column may be bound to the wrong one."""
    sql = (
        "SELECT id FROM gold.orders WHERE EXISTS "
        "(SELECT 1 FROM (SELECT 3 AS id) AS orders WHERE orders.id = gold.orders.id)"
    )
    try:
        rows = _run(sql, _gold_and_silver(tmp_path))
    except duckdb.BinderException:
        return

    assert rows == [(3,)]


def test_a_cte_named_like_a_table_beside_that_table_keeps_working(tmp_path: Path) -> None:
    """Worked before columns could be qualified: `orders.note` is the CTE's."""
    sources = {"gold.orders": _parquet(str(_orders(tmp_path)))}

    rows = _run(
        "WITH orders AS (SELECT 'cte' AS note) SELECT orders.note FROM orders, gold.orders",
        sources,
    )

    assert rows == [("cte",)] * 3


def test_an_explicit_alias_named_like_another_table_keeps_working(tmp_path: Path) -> None:
    """Worked before: `orders.x` is the silver table aliased `orders`."""
    sources = _two_orders(tmp_path)

    rows = _run("SELECT orders.s FROM silver.orders AS orders JOIN gold.orders ON true", sources)

    assert len(rows) == 9


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM gold.orders WHERE EXISTS "
        "(SELECT 1 FROM (SELECT {'id': 1} AS orders) d WHERE gold.orders.id = 3)",
        "WITH x AS (SELECT {'id': 1} AS orders) "
        "SELECT id FROM gold.orders WHERE EXISTS (SELECT 1 FROM x WHERE gold.orders.id = 3)",
        "WITH x AS (SELECT gold.orders.id AS i FROM (SELECT 1) t) "
        "SELECT id FROM gold.orders, x WHERE x.i = id",
    ],
)
def test_a_full_name_in_a_nested_scope_never_binds_to_a_struct_column_of_that_scope(
    tmp_path: Path, sql: str
) -> None:
    """`orders.id` would read the field `id` of a struct column `orders`; the full name may not."""
    try:
        rows = _run(sql, _gold_and_silver(tmp_path))
    except duckdb.BinderException:
        return

    assert rows == [(3,)]


def test_a_full_view_name_in_a_nested_scope_stays_as_written(tmp_path: Path) -> None:
    """The view path follows the same rule: the cut happens only in the view's own scope."""
    expanded = expand_views(
        "SELECT id FROM gold.v WHERE EXISTS "
        "(SELECT 1 FROM (SELECT {'id': 1} AS v) d WHERE gold.v.id = 3)",
        lambda dotted: (
            ViewDefinition(name=dotted, sql="SELECT 3 AS id") if dotted == "gold.v" else None
        ),
    )

    with pytest.raises(duckdb.BinderException):
        duckdb.connect().execute(expanded.sql)


def _fid(name: str, arrow_type: pa.DataType, field_id: int) -> pa.Field:
    """Build an Arrow field that carries an Iceberg field id.

    Parameters
    ----------
    name
        The column's name in the file.
    arrow_type
        Its type in the file.
    field_id
        Its Iceberg field id.

    Returns
    -------
    pa.Field
        The field, with ``PARQUET:field_id`` metadata.
    """
    return pa.field(name, arrow_type, metadata={b"PARQUET:field_id": str(field_id).encode()})


def _evolved_files(tmp_path: Path) -> tuple[str, str]:
    """Write a file before and a file after five schema changes, as an Iceberg writer does.

    Between them: `id` int to long, `name` renamed `full_name`, `price` float to double,
    `old_col` dropped and added again (another field id, another type), `added` added.

    Parameters
    ----------
    tmp_path
        Where the files go.

    Returns
    -------
    tuple of str
        The older file and the newer one.
    """
    old, new = tmp_path / "old.parquet", tmp_path / "new.parquet"
    pq.write_table(
        pa.table(
            {
                "id": pa.array([1, 2], pa.int32()),
                "name": ["a", "b"],
                "price": pa.array([1.5, 2.5], pa.float32()),
                "old_col": ["x-old", "y-old"],
            },
            schema=pa.schema(
                [
                    _fid("id", pa.int32(), 1),
                    _fid("name", pa.string(), 2),
                    _fid("price", pa.float32(), 3),
                    _fid("old_col", pa.string(), 4),
                ]
            ),
        ),
        old,
    )
    pq.write_table(
        pa.table(
            {
                "id": pa.array([3, 99], pa.int64()),
                "full_name": ["c", "d"],
                "price": pa.array([3.5, 4.5], pa.float64()),
                "old_col": pa.array([50, 60], pa.int32()),
                "added": ["n1", "n2"],
            },
            schema=pa.schema(
                [
                    _fid("id", pa.int64(), 1),
                    _fid("full_name", pa.string(), 2),
                    _fid("price", pa.float64(), 3),
                    _fid("old_col", pa.int32(), 5),
                    _fid("added", pa.string(), 6),
                ]
            ),
        ),
        new,
    )
    return str(old), str(new)


_EVOLVED_FIELDS = (
    ReadField(1, "id", "BIGINT"),
    ReadField(2, "full_name", "VARCHAR"),
    ReadField(3, "price", "DOUBLE"),
    ReadField(5, "old_col", "INTEGER"),
    ReadField(6, "added", "VARCHAR"),
)
_EVOLVED_COLUMNS = {
    "id": "long",
    "full_name": "string",
    "price": "double",
    "old_col": "int",
    "added": "string",
}
_EVOLVED_ROWS = [
    (1, "a", 1.5, None, None),
    (2, "b", 2.5, None, None),
    (3, "c", 3.5, 50, "n1"),
    (99, "d", 4.5, 60, "n2"),
]
"""The table as its current schema says: renamed data under the new name, the dropped column's
old values gone, the added columns NULL in the older file."""


@pytest.mark.parametrize("policy", ["none", "row_filter", "column_mask"])
@pytest.mark.parametrize("newest_first", [False, True])
def test_a_read_by_field_id_gives_the_same_rows_in_either_file_order(
    tmp_path: Path, newest_first: bool, policy: str
) -> None:
    """Without the map the two files fail with "schema mismatch in glob" in either order.

    Parameters
    ----------
    tmp_path
        Where the files go.
    newest_first
        The order the catalog lists the files in.
    policy
        No policy, a row filter on the re-added column, or a mask on the renamed one.
    """
    old, new = _evolved_files(tmp_path)
    source = TableSource(
        urls=[new, old] if newest_first else [old, new],
        file_format="parquet",
        columns=_EVOLVED_COLUMNS,
        row_filters=("old_col IS NULL OR old_col > 55",) if policy == "row_filter" else (),
        column_masks={"full_name": "NULL"} if policy == "column_mask" else {},
        field_ids=_EVOLVED_FIELDS,
    )

    rows = sorted(
        _fetch(rewrite_query("SELECT * FROM gold.t", {"gold.t": source})), key=lambda r: r[0]
    )

    expected = _EVOLVED_ROWS
    if policy == "row_filter":
        expected = [row for row in expected if row[3] is None or row[3] > 55]
    if policy == "column_mask":
        expected = [(row[0], None, *row[2:]) for row in expected]
    assert rows == expected


def test_a_read_of_two_thousand_files_and_a_thousand_columns_by_field_id(tmp_path: Path) -> None:
    """The file list and the map grow with the table and still go through the query process."""
    fields = [_fid(f"c{i}", pa.int64(), i) for i in range(1, 1001)]
    path = tmp_path / "wide.parquet"
    pq.write_table(
        pa.table({f.name: pa.array([1, 2], pa.int64()) for f in fields}, schema=pa.schema(fields)),
        path,
    )
    source = TableSource(
        urls=[str(path)] * 2000,
        file_format="parquet",
        columns={f"c{i}": "long" for i in range(1, 1001)},
        field_ids=tuple(ReadField(i, f"c{i}", "BIGINT") for i in range(1, 1001)),
    )
    rewritten = rewrite_query(
        "SELECT count(*) AS n, sum(c500) AS s, count(c1000) AS k FROM gold.wide",
        {"gold.wide": source},
    )

    result = execute_query(
        rewritten.sql, rewritten.parameters, read_prefixes=local_prefixes(rewritten.parameters)
    )

    assert result.to_pylist() == [{"n": 4000, "s": 6000, "k": 4000}]


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM gold.t WHERE CAST(secret AS INTEGER) = 1",
        "SELECT id, CAST(secret AS INTEGER) AS n FROM gold.t",
    ],
)
def test_a_users_cast_never_reaches_a_row_the_filter_hides(tmp_path: Path, query: str) -> None:
    """A value in a hidden row is neither converted nor quoted in an error.

    The row filter runs before the user's predicates and select list.

    Parameters
    ----------
    tmp_path
        Where the file goes.
    query
        A query that casts the hidden column in its predicate or select list.
    """
    path = tmp_path / "rows.parquet"
    rows = 30_000
    pq.write_table(
        pa.table(
            {
                "id": list(range(rows)),
                "dept": ["a" if i % 2 else "b" for i in range(rows)],
                "secret": ["HIDDEN-not-a-number" if i % 2 else "1" for i in range(rows)],
            }
        ),
        path,
        row_group_size=1000,
    )
    source = TableSource(
        urls=[str(path)],
        file_format="parquet",
        columns={"id": "long", "dept": "string", "secret": "string"},
        row_filters=("dept = 'b'",),
    )

    rewritten = rewrite_query(query, {"gold.t": source})

    rows_out = _fetch(rewritten)
    assert len(rows_out) == rows // 2
    assert "HIDDEN" not in repr(rows_out)


def _target(field_type: IcebergType) -> exp.DataType:
    """Give the cast target of an Iceberg type that has one.

    Parameters
    ----------
    field_type
        The Iceberg type.

    Returns
    -------
    sqlglot.expressions.DataType
        The structured type.
    """
    target = iceberg_cast_type(field_type)
    assert target is not None
    return target


def _typed_file(tmp_path: Path) -> str:
    """Write a parquet file with a BIGINT, DOUBLE, DECIMAL, VARCHAR and INTEGER column.

    Parameters
    ----------
    tmp_path
        Where the file goes.

    Returns
    -------
    str
        The file's path.
    """
    probe = tmp_path / "typed.parquet"
    pq.write_table(
        pa.table(
            {
                "big": pa.array([5], pa.int64()),
                "dbl": pa.array([1.5], pa.float64()),
                "dec": pa.array([Decimal("12.34")], pa.decimal128(10, 2)),
                "txt": pa.array(["secret"], pa.string()),
                "small": pa.array([7], pa.int32()),
            }
        ),
        probe,
    )
    return str(probe)


_TYPEOF = (
    "SELECT typeof(big), typeof(dbl), typeof(dec), typeof(txt), typeof(small), "
    "big, dbl, dec, txt, small FROM gold.typed"
)
_TYPED_COLUMNS = {
    "big": "BIGINT",
    "dbl": "DOUBLE",
    "dec": "DECIMAL(10, 2)",
    "txt": "VARCHAR",
    "small": "INTEGER",
}
_TYPED_MASKS = {"big": "0", "dbl": "0", "dec": "0", "txt": "'***'", "small": "NULL"}


def test_a_type_checked_mask_keeps_the_columns_own_type_for_the_reader(tmp_path: Path) -> None:
    """A type-checked mask is cast: `0` on BIGINT, DOUBLE and DECIMAL stays that type."""
    source = TableSource(
        urls=[_typed_file(tmp_path)],
        file_format="parquet",
        columns=_TYPED_COLUMNS,
        column_masks=_TYPED_MASKS,
        mask_types={
            "big": _target(LongType()),
            "dbl": _target(DoubleType()),
            "dec": _target(DecimalType(10, 2)),
            "txt": _target(StringType()),
            "small": _target(IntegerType()),
        },
        typed_masks=frozenset(_TYPED_MASKS),
    )

    row = _fetch(rewrite_query(_TYPEOF, {"gold.typed": source}))[0]

    assert row[:5] == ("BIGINT", "DOUBLE", "DECIMAL(10,2)", "VARCHAR", "INTEGER")
    assert row[5:] == (0, 0.0, Decimal("0.00"), "***", None)


def test_a_mask_of_an_earlier_release_reads_as_it_always_did(tmp_path: Path) -> None:
    """A mask that was never type-checked is not cast: `0` is an INTEGER, as before."""
    source = TableSource(
        urls=[_typed_file(tmp_path)],
        file_format="parquet",
        columns=_TYPED_COLUMNS,
        column_masks=_TYPED_MASKS,
        mask_types={
            "big": _target(LongType()),
            "dbl": _target(DoubleType()),
        },
    )

    rewritten = rewrite_query(_TYPEOF, {"gold.typed": source})

    assert "CAST(" not in rewritten.sql.split("FROM READ_PARQUET")[0]
    row = _fetch(rewritten)[0]
    assert row[0] == "INTEGER"
    assert row[1] == "INTEGER"


def test_a_masked_read_of_an_external_table_of_common_types_works(tmp_path: Path) -> None:
    """The stored Arrow type names of timestamps, dates, decimals, structs and tz columns."""
    path = tmp_path / "raw.parquet"
    table = pa.table(
        {
            "ts": pa.array([datetime(2026, 1, 1)], pa.timestamp("us")),
            "d": pa.array([date(2026, 1, 1)], pa.date32()),
            "dec": pa.array([Decimal("1.50")], pa.decimal128(10, 2)),
            "ls": pa.array(["x"], pa.large_string()),
            "st": pa.array([{"a": 1}], pa.struct([("a", pa.int64())])),
            "tz": pa.array([datetime(2026, 1, 1, tzinfo=UTC)], pa.timestamp("us", tz="UTC")),
        }
    )
    pq.write_table(table, path)
    stored = {field.name: str(field.type) for field in table.schema}
    names = list(stored)
    source = TableSource(
        urls=[str(path)],
        file_format="parquet",
        columns=stored,
        column_masks=dict.fromkeys(names, "NULL"),
        mask_types={
            name: target for name in names if (target := arrow_cast_type(stored[name])) is not None
        },
        typed_masks=frozenset(names),
    )

    rewritten = rewrite_query("SELECT * FROM gold.raw", {"gold.raw": source})

    assert _fetch(rewritten) == [(None,) * len(names)]
    assert "STRUCT" not in rewritten.sql


def test_a_crafted_struct_field_name_cannot_reach_the_cast(tmp_path: Path) -> None:
    """The stored type of a struct column is never rendered: no cast, and none of its text."""
    path = tmp_path / "raw.parquet"
    crafted = "a DECIMAL(10, (SELECT 42)) , b"
    table = pa.table({"st": pa.array([{crafted: 1}], pa.struct([(crafted, pa.int64())]))})
    pq.write_table(table, path)
    stored = {"st": str(table.schema.field("st").type)}
    assert "SELECT 42" in stored["st"]
    source = TableSource(
        urls=[str(path)],
        file_format="parquet",
        columns=stored,
        column_masks={"st": "NULL"},
        mask_types={},
        typed_masks=frozenset({"st"}),
    )

    rewritten = rewrite_query("SELECT * FROM gold.raw", {"gold.raw": source})

    assert "SELECT 42" not in rewritten.sql
    assert _fetch(rewritten) == [(None,)]


def test_a_type_checked_mask_that_reads_a_widened_column_shows_null_for_a_value_that_no_longer_fits(
    tmp_path: Path,
) -> None:
    """`a` is INTEGER and its mask reads `b`, widened from INTEGER to BIGINT after the check."""
    path = tmp_path / "widened.parquet"
    pq.write_table(
        pa.table(
            {
                "a": pa.array([1, 2], pa.int32()),
                "b": pa.array([7, 3_000_000_000], pa.int64()),
            }
        ),
        path,
    )
    source = TableSource(
        urls=[str(path)],
        file_format="parquet",
        columns={"a": "INTEGER", "b": "BIGINT"},
        column_masks={"a": "b"},
        mask_types={"a": _target(IntegerType())},
        typed_masks=frozenset({"a"}),
    )

    rewritten = rewrite_query("SELECT typeof(a), a FROM gold.w ORDER BY b", {"gold.w": source})

    assert _fetch(rewritten) == [("INTEGER", 7), ("INTEGER", None)]


def _timestamp_file(tmp_path: Path, unit: str) -> str:
    """Write a Parquet file with one `ts` column at a precision, as pyarrow writes it.

    Parameters
    ----------
    tmp_path
        Where the file goes.
    unit
        The Arrow unit: `s`, `ms`, `us` or `ns`.

    Returns
    -------
    str
        The file's path.
    """
    path = tmp_path / f"ts_{unit}.parquet"
    pq.write_table(pa.table({"ts": pa.array([datetime(2026, 1, 1)], pa.timestamp(unit))}), path)
    return str(path)


@pytest.mark.parametrize("unit", ["s", "ms", "us"])
def test_a_parquet_timestamp_up_to_micro_takes_a_filter_and_a_typed_mask(
    tmp_path: Path, unit: str
) -> None:
    """DuckDB reads these as TIMESTAMP: the filter runs and the constant mask keeps the type."""
    stored = str(pq.read_schema(_timestamp_file(tmp_path, unit)).field("ts").type)
    target = arrow_cast_type(stored)
    assert target is not None
    for literal in ("2025-01-01 00:00:00", "3000-01-01 00:00:00", "0100-01-01 00:00:00"):
        source = TableSource(
            urls=[_timestamp_file(tmp_path, unit)],
            file_format="parquet",
            columns={"ts": stored},
            row_filters=(f"ts > TIMESTAMP '{literal}'",),
        )
        rows = _fetch(rewrite_query("SELECT typeof(ts) FROM gold.t", {"gold.t": source}))
        assert rows in ([("TIMESTAMP",)], [])
    masked = TableSource(
        urls=[_timestamp_file(tmp_path, unit)],
        file_format="parquet",
        columns={"ts": stored},
        column_masks={"ts": "TIMESTAMP '1970-01-01 00:00:00'"},
        mask_types={"ts": target},
        typed_masks=frozenset({"ts"}),
    )
    row = _fetch(rewrite_query("SELECT typeof(ts), ts FROM gold.t", {"gold.t": masked}))[0]
    assert row == ("TIMESTAMP", datetime(1970, 1, 1))


def test_a_nanosecond_parquet_timestamp_takes_a_literal_only_inside_its_range(
    tmp_path: Path,
) -> None:
    """The edges of the range the check allows: the last inside works, the first outside fails."""
    path = _timestamp_file(tmp_path, "ns")
    stored = str(pq.read_schema(path).field("ts").type)
    assert stored == "timestamp[ns]"

    def run(literal: str) -> list[tuple[object, ...]]:
        source = TableSource(
            urls=[path],
            file_format="parquet",
            columns={"ts": stored},
            row_filters=(f"ts < {literal}",),
        )
        return _fetch(rewrite_query("SELECT typeof(ts) FROM gold.t", {"gold.t": source}))

    assert run("TIMESTAMP '2262-04-11 23:47:16.854775'") == [("TIMESTAMP_NS",)]
    assert run("TIMESTAMP '1677-09-21 00:12:43.145225'") == []
    assert run("DATE '2262-04-11'") == [("TIMESTAMP_NS",)]
    for outside in ("TIMESTAMP '2262-04-11 23:47:16.854776'", "TIMESTAMP '3000-01-01'"):
        with pytest.raises(duckdb.ConversionException):
            run(outside)


def test_a_mask_on_a_nanosecond_parquet_timestamp_is_read_as_written(tmp_path: Path) -> None:
    """`NULL` and the column itself are used as stored: no cast, the column keeps its type."""
    path = _timestamp_file(tmp_path, "ns")
    for mask, expected in (
        ("NULL", ('"NULL"', None)),
        ("ts", ("TIMESTAMP_NS", datetime(2026, 1, 1))),
    ):
        source = TableSource(
            urls=[path],
            file_format="parquet",
            columns={"ts": "timestamp[ns]"},
            column_masks={"ts": mask},
            mask_types={},
        )
        rewritten = rewrite_query(
            "SELECT typeof(ts), ts FROM gold.t WHERE ts > TIMESTAMP '2020-01-01' OR ts IS NULL",
            {"gold.t": source},
        )
        assert "CAST" not in rewritten.sql.split(" FROM READ_PARQUET")[0]
        assert _fetch(rewritten) == [expected]


@pytest.mark.parametrize(
    ("sql", "rows"),
    [
        ("VALUES (1, 'a'), (2, 'b')", [{"col0": 1, "col1": "a"}, {"col0": 2, "col1": "b"}]),
        ("VALUES (2), (1), (3) ORDER BY 1 DESC LIMIT 2", [{"col0": 3}, {"col0": 2}]),
    ],
)
def test_a_query_that_is_only_a_values_list_returns_its_rows(
    sql: str, rows: list[dict[str, object]]
) -> None:
    """`lakana sql "VALUES ..."` takes the query path and returns what DuckDB itself returns.

    Parameters
    ----------
    sql
        A bare `VALUES` list.
    rows
        The rows, with the column names DuckDB gives a bare `VALUES`.
    """
    check_blacklist(sql)
    rewritten = rewrite_query(sql, {})

    result = execute_query(
        rewritten.sql, rewritten.parameters, read_prefixes=local_prefixes(rewritten.parameters)
    )

    assert result.to_pylist() == rows
    assert duckdb.connect().execute(sql).to_arrow_table().to_pylist() == rows


def test_a_column_mask_holds_for_a_read_inside_a_bare_values_list(tmp_path: Path) -> None:
    """A masked column read through a subquery in `VALUES` shows the mask, never the value."""
    probe = tmp_path / "customers.parquet"
    pq.write_table(pa.table({"id": [1, 2], "ssn": ["111-22-3333", "444-55-6666"]}), probe)
    source = TableSource(
        urls=[str(probe)],
        file_format="parquet",
        columns={"id": "BIGINT", "ssn": "VARCHAR"},
        column_masks={"ssn": "'***'"},
    )
    sql = "VALUES ((SELECT max(ssn) FROM gold.customers), (SELECT count(*) FROM gold.customers))"
    check_blacklist(sql)

    rows = _fetch(rewrite_query(sql, {"gold.customers": source}))

    assert rows == [("***", 2)]
