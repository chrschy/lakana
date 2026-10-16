"""Policy-rewritten queries executed against a real DuckDB connection.

These tests belong to the worker, the only package that may import ``duckdb``. They prove that a
query `lakana_core.query_rewrite` rewrote actually runs and actually masks and filters rows, which
the tests of lakana-core cannot do: they assert on the rewritten SQL text, and the CI gate
`scripts/tests/test_package_test_imports.py` keeps them from importing DuckDB or the worker.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from lakana_core.errors import ValidationError
from lakana_core.query_rewrite import TableSource, check_blacklist, rewrite_query


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
    rows = duckdb.connect().execute(rewritten.sql, rewritten.parameters).fetchall()

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
    rows = duckdb.connect().execute(rewritten.sql, rewritten.parameters).fetchall()

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

    rows = duckdb.connect().execute(rewritten.sql, rewritten.parameters).fetchall()
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
    rows = duckdb.connect().execute(rewritten.sql, rewritten.parameters).fetchall()
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
    except (duckdb.BinderException, ValidationError):
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
