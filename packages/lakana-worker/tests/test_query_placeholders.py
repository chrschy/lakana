"""User SQL must never reach the rewriter's own bound parameters.

`lakana_core.query_rewrite.rewrite_query` hands DuckDB each table's resolved
file list as a bound parameter — on S3, presigned URLs of the raw files. A
placeholder in the user's own SQL would bind to that list and return it as
an ordinary value, and anyone holding those URLs can read the raw files
directly, past every row filter and column mask. These tests drive the real
rewriter and the real executor, the level where that leak happened.
"""

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from lakana_core.errors import ValidationError
from lakana_core.query_rewrite import TableSource, check_blacklist, rewrite_query
from lakana_worker.executor import execute_query

_PLACEHOLDER_QUERIES = [
    pytest.param("SELECT $1 AS leak, ssn FROM bronze.customers", id="positional-dollar"),
    pytest.param("SELECT $ 1 AS leak, ssn FROM bronze.customers", id="positional-dollar-space"),
    pytest.param("SELECT ? AS leak, ssn FROM bronze.customers", id="question-mark"),
    pytest.param("SELECT $name AS leak, ssn FROM bronze.customers", id="named"),
    pytest.param("SELECT $_ AS leak, ssn FROM bronze.customers", id="named-underscore"),
    pytest.param("SELECT ssn FROM bronze.customers WHERE id > $1", id="where"),
    pytest.param("SELECT len($1) AS n, ssn FROM bronze.customers", id="function-argument"),
    pytest.param("SELECT CAST($1 AS VARCHAR) AS leak FROM bronze.customers", id="cast"),
    pytest.param("SELECT ssn FROM bronze.customers WHERE ssn IN (?, ?)", id="in-list"),
    pytest.param("SELECT ssn FROM bronze.customers LIMIT ?", id="limit"),
    pytest.param("SELECT ssn FROM bronze.customers ORDER BY $1", id="order-by"),
    pytest.param("SELECT (SELECT $1) AS leak, ssn FROM bronze.customers", id="scalar-subquery"),
    pytest.param(
        "SELECT * FROM (SELECT $1 AS leak, ssn FROM bronze.customers) AS s",
        id="derived-table",
    ),
    pytest.param(
        "WITH c AS (SELECT $1 AS leak) SELECT c.leak, b.ssn FROM c, bronze.customers AS b",
        id="cte",
    ),
    pytest.param(
        "SELECT ssn FROM bronze.customers UNION ALL SELECT $1::VARCHAR",
        id="union-branch",
    ),
    pytest.param(
        "SELECT b.ssn FROM bronze.customers AS b JOIN bronze.customers AS c ON c.id = $1",
        id="join-condition",
    ),
]


@pytest.fixture
def customers(tmp_path: Path) -> TableSource:
    """Build a masked, row-filtered `bronze.customers` backed by one local file.

    Parameters
    ----------
    tmp_path
        pytest's per-test temporary directory.

    Returns
    -------
    TableSource
        The table's resolved source, with a row filter and an `ssn` mask.
    """
    path = tmp_path / "c.parquet"
    pq.write_table(pa.table({"id": [1, 2, 3], "ssn": ["111", "222", "333"]}), path)
    return TableSource(
        urls=[str(path)],
        file_format="parquet",
        columns={"id": "BIGINT", "ssn": "VARCHAR"},
        row_filters=("id > 1",),
        column_masks={"ssn": "'***'"},
    )


@pytest.mark.parametrize("sql", _PLACEHOLDER_QUERIES)
def test_a_placeholder_is_refused_before_it_can_bind_to_the_source_urls(
    sql: str, customers: TableSource
) -> None:
    """Every placeholder form fails validation, and no error names a file.

    Parameters
    ----------
    sql
        A query carrying a placeholder somewhere in its tree.
    customers
        The masked, row-filtered source fixture.
    """
    url = customers.urls[0]
    with pytest.raises(ValidationError, match="placeholders are not allowed") as blacklisted:
        check_blacklist(sql)
    assert url not in str(blacklisted.value)
    assert "c.parquet" not in str(blacklisted.value)

    with pytest.raises(ValidationError, match="placeholders are not allowed") as rewritten:
        rewrite_query(sql, {"bronze.customers": customers})
    assert url not in str(rewritten.value)
    assert "c.parquet" not in str(rewritten.value)


def test_the_reported_leak_no_longer_returns_the_source_urls(customers: TableSource) -> None:
    """The reproduced query never reaches DuckDB at all.

    Before the fix this returned `[{'leak': [<file url>], 'ssn': '***'}]`:
    the mask and the filter both held, and the raw file's URL came back
    beside them.

    Parameters
    ----------
    customers
        The masked, row-filtered source fixture.
    """
    sql = "SELECT $1 AS leak, ssn FROM bronze.customers"
    with pytest.raises(ValidationError, match="placeholders are not allowed"):
        rewrite_query(sql, {"bronze.customers": customers})

    safe = rewrite_query("SELECT ssn FROM bronze.customers", {"bronze.customers": customers})
    result = execute_query(safe.sql, safe.parameters, restrict_local_filesystem=False)
    assert customers.urls[0] not in str(result.to_pylist())


def test_the_rewritten_query_binds_urls_under_unguessable_names(
    customers: TableSource,
) -> None:
    """The URL lists bind by per-rewrite random names, never by position.

    Parameters
    ----------
    customers
        The masked, row-filtered source fixture.
    """
    sql = "SELECT id, ssn FROM bronze.customers"
    first = rewrite_query(sql, {"bronze.customers": customers})
    second = rewrite_query(sql, {"bronze.customers": customers})

    assert "?" not in first.sql
    assert list(first.parameters.values()) == [list(customers.urls)]
    assert set(first.parameters).isdisjoint(second.parameters)
    for name in first.parameters:
        assert f"${name}" in first.sql
    result = execute_query(first.sql, first.parameters, restrict_local_filesystem=False)
    assert result.to_pylist() == [{"id": 2, "ssn": "***"}, {"id": 3, "ssn": "***"}]


@pytest.mark.parametrize("smuggled", ["$1", "?"])
def test_a_positional_placeholder_that_slipped_past_validation_still_cannot_bind(
    smuggled: str, customers: TableSource
) -> None:
    """Defence in depth: a positional reference cannot address named URL lists.

    Stands in for a future parser differential that let a positional
    placeholder survive into the rewritten SQL: DuckDB refuses to mix it
    with the rewriter's named parameters rather than binding it.

    Parameters
    ----------
    smuggled
        The positional placeholder spliced into the rewritten SQL.
    customers
        The masked, row-filtered source fixture.
    """
    rewritten = rewrite_query("SELECT ssn FROM bronze.customers", {"bronze.customers": customers})
    sql = rewritten.sql.replace("SELECT ssn FROM", f"SELECT {smuggled} AS leak, ssn FROM", 1)
    assert smuggled in sql

    with pytest.raises(ValidationError) as excinfo:
        execute_query(sql, rewritten.parameters, restrict_local_filesystem=False)
    assert customers.urls[0] not in str(excinfo.value)
    assert "c.parquet" not in str(excinfo.value)


@pytest.mark.parametrize(
    "sql",
    [
        "PREPARE p AS SELECT $1",
        "EXECUTE p(1)",
        "DEALLOCATE p",
    ],
)
def test_prepared_statement_syntax_is_refused(sql: str) -> None:
    """User SQL cannot open its own prepared statement to bind parameters with.

    Parameters
    ----------
    sql
        A prepared-statement command.
    """
    with pytest.raises(ValidationError):
        check_blacklist(sql)
