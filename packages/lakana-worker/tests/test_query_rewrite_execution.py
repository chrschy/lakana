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
