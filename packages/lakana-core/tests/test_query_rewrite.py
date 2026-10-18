"""Tests for SQL query rewriting and the AST blacklist."""

import re

import pytest
import sqlglot
import sqlglot.expressions as exp

from lakana_core import query_rewrite
from lakana_core.errors import ValidationError
from lakana_core.query_rewrite import (
    ReadField,
    TableSource,
    ViewDefinition,
    _cte_alias,
    check_blacklist,
    expand_views,
    extract_table_refs,
    rewrite_query,
)

_READER_CALL = re.compile(r"READ_PARQUET\(GETVARIABLE\('v_\d+_files'\)\)")
"""A generated reader call, reading one of the variables the query process sets."""


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


def test_extract_table_refs_finds_every_qualified_table() -> None:
    """Every distinct qualified table name in the query is returned."""
    refs = extract_table_refs("SELECT a.id FROM bronze.a AS a JOIN gold.b AS b ON a.id = b.id")

    assert set(refs) == {"bronze.a", "gold.b"}


def test_extract_table_refs_ignores_a_name_the_query_defines_itself() -> None:
    """A `WITH` name is not a catalog table and must not be reported as one.

    `NamespacePath.from_parts([])` raises for an unqualified name, so
    treating a CTE name as a table reference failed the whole query.
    """
    refs = extract_table_refs("WITH x AS (SELECT id FROM raw.events) SELECT id FROM x")

    assert refs == ["raw.events"]


def test_extract_table_refs_reports_a_real_table_shadowed_by_an_inner_cte() -> None:
    """An inner-scope `WITH` name must not hide the real table of that name read outside it.

    The exclusion used to collect every CTE name anywhere in the statement
    and drop every same-named table node, which was an **authorization
    bypass**: the outer reference is a genuine read that DuckDB resolves
    scope-correctly, but it was never reported, so `pipeline.run_one` never
    authorized it.
    """
    sql = (
        "SELECT id FROM raw.events "
        'WHERE id IN (WITH "raw.events" AS (SELECT 1 AS id) SELECT id FROM "raw.events")'
    )

    assert extract_table_refs(sql) == ["raw.events"]


def test_extract_table_refs_reports_an_outer_url_shadowed_by_an_inner_cte() -> None:
    """The exploit shape: a quoted URL DuckDB reads over HTTP, shadowed by a same-named CTE.

    DuckDB reads `FROM "http://…/x.csv"` as a real HTTP fetch, so dropping
    this reference was worker-side SSRF with no authorization call made at
    all. It is reported here; the pipeline then rejects it as a name no
    catalog table has.
    """
    sql = (
        'SELECT * FROM "http://evil/x.csv" WHERE 1 IN ('
        'WITH "http://evil/x.csv" AS (SELECT 1 AS z) SELECT z FROM "http://evil/x.csv")'
    )

    assert extract_table_refs(sql) == ["http://evil/x.csv"]


def test_extract_table_refs_reports_a_qualified_table_beside_a_same_named_cte() -> None:
    """A qualified name is never a `WITH` binding, even where the scope binds that bare name.

    sqlglot keys a scope's sources by bare name, so the binding wins the key
    and the real `db.x` would be dropped again — but DuckDB binds a `WITH`
    name as a single identifier and resolves `db.x` to the real table. Both
    references are read here, the binding under its own alias so that the
    query means one thing; DuckDB runs this exact statement.
    """
    refs = extract_table_refs("WITH x AS (SELECT 1 AS id) SELECT * FROM db.x JOIN x AS y ON TRUE")

    assert refs == ["db.x"]


def test_extract_table_refs_refuses_an_unaliased_qualified_table_beside_that_cte() -> None:
    """Dropping the alias makes `x` mean two things at once, and the query is refused.

    DuckDB refuses it too — `Ambiguous reference to table "x" (use: db.x or
    .x)` — so nothing runnable is lost by declining to guess which of the two
    a bare `x` names.
    """
    with pytest.raises(ValidationError, match="unambiguously"):
        extract_table_refs("WITH x AS (SELECT 1 AS id) SELECT * FROM db.x JOIN x ON TRUE")


def test_extract_table_refs_reports_a_url_shadowed_by_a_derived_table_alias() -> None:
    """A derived table's alias is not a `WITH` binding and must not hide a real read.

    `scope.sources` maps a derived table's alias to a `Scope` too, not only a
    `WITH` binding, so excluding every scope-valued source dropped the second,
    bare reference here — a genuine HTTP read DuckDB issues — while a
    *different* `FROM` entry merely happened to be aliased to the same name.
    Only a name a `WITH` clause actually bound is a binding, which is what
    `Scope.cte_sources` holds and a derived table's alias never enters.
    """
    sql = (
        'SELECT * FROM (SELECT 1 AS z) AS "http://evil.invalid/x.csv", "http://evil.invalid/x.csv"'
    )

    assert extract_table_refs(sql) == ["http://evil.invalid/x.csv"]


def test_extract_table_refs_refuses_a_table_alias_that_reuses_a_cte_name() -> None:
    """The proven exploit: a real read *aliased* to an unrelated `WITH` name.

    The classification used to ask `scope.sources` for the reference's
    `alias_or_name`, so aliasing a genuine external read to a CTE's name
    handed back that CTE's `Scope` and dropped the read from the reported
    references entirely — an **authorization bypass** with the file still
    read for real. Two sources of one scope may not share a name, so the
    query is refused outright now rather than classified at all.
    """
    sql = (
        "WITH c AS (SELECT 1 AS id) "
        'SELECT * FROM (SELECT val FROM c, "http://evil.invalid/x.csv" AS c) AS w'
    )

    with pytest.raises(ValidationError, match="unambiguously"):
        extract_table_refs(sql)


def test_rewrite_refuses_a_table_alias_that_reuses_a_cte_name() -> None:
    """The rewrite refuses the same ambiguity on its own, not only the extraction.

    `rewrite_query` classifies references a second time, so it makes the same
    check independently: neither entry point may rely on the other having
    been called first.
    """
    sql = 'WITH c AS (SELECT 1 AS id) SELECT * FROM (SELECT val FROM c, "raw.events" AS c) AS w'

    with pytest.raises(ValidationError, match="unambiguously"):
        rewrite_query(sql, sources={"raw.events": _parquet("a.parquet")})


def test_extract_table_refs_reports_both_tables_of_an_unqualified_column_join() -> None:
    """An unqualified column over two sources is ordinary SQL, not an ambiguity to refuse.

    sqlglot's `qualify` validates *columns* as well as tables by default, and
    no schema is passed here, so leaving that validation on would refuse this
    query — which DuckDB, holding the real schemas, runs without complaint.
    The reference check is about table and alias resolution only.
    """
    refs = extract_table_refs("SELECT id FROM bronze.a JOIN gold.b ON bronze.a.x = gold.b.y")

    assert refs == ["bronze.a", "gold.b"]


def test_extract_table_refs_reports_two_same_named_tables_in_different_namespaces() -> None:
    """Two namespaces may hold a table of the same name, and both reads are reported.

    Their bare names collide, which is not an ambiguity: DuckDB runs
    `FROM bronze.t, silver.t` and tells the two apart by namespace, so this
    must be classified rather than refused.
    """
    refs = extract_table_refs("SELECT * FROM bronze.t, silver.t")

    assert refs == ["bronze.t", "silver.t"]


def test_extract_table_refs_refuses_a_recursive_cte_rather_than_classifying_it() -> None:
    """What this test used to assert is precisely the bypass, so it is inverted.

    It formerly required `extract_table_refs` to return `[]` here, on the
    reading that a recursive `WITH` name is visible inside its own body and
    so is never a table. That is true of the *recursive* term and false of
    the *base* term, where DuckDB still resolves the name against a real
    relation — and `[]` meant no authorization call at all. Replacing the
    query's shape with a quoted path turned the same assertion into a file
    read nobody checked.

    The name is now refused outright, so the distinction never has to be
    drawn. Kept at this spot, inverted, so the old expectation cannot be
    reintroduced by someone reading the gap as an oversight.
    """
    sql = (
        "WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r WHERE n < 3) "
        "SELECT n FROM r"
    )

    with pytest.raises(ValidationError, match="Recursive CTEs"):
        extract_table_refs(sql)


def test_extract_table_refs_reports_a_url_aliased_to_a_cte_name() -> None:
    """An alias borrowed from a `WITH` name never makes a real read a `WITH` binding.

    Nothing here is ambiguous — the alias shadows the binding inside the
    derived table, exactly as DuckDB resolves it — so the query is classified
    rather than refused, and the aliased read is a real one that must be
    reported and authorized.
    """
    sql = (
        "WITH c AS (SELECT 1 AS id) "
        'SELECT * FROM (SELECT val FROM "http://evil.invalid/x.csv" AS c) AS w, c'
    )

    assert extract_table_refs(sql) == ["http://evil.invalid/x.csv"]


def test_rewrite_rewrites_a_name_shadowed_by_a_derived_table_alias() -> None:
    """The real read beside a same-named derived table is rewritten to its scoped files."""
    rewritten = rewrite_query(
        'SELECT * FROM (SELECT 1 AS z) AS "raw.events", "raw.events"',
        sources={"raw.events": _parquet("a.parquet")},
    )

    assert _cte_alias("raw.events") in rewritten.sql
    assert len(_READER_CALL.findall(rewritten.sql)) == 1
    assert list(rewritten.parameters.values()) == [["a.parquet"]]


def test_rewrite_rewrites_only_the_outer_occurrence_of_a_shadowed_name() -> None:
    """The real outer read is rewritten to its scoped files; the inner CTE is left alone."""
    rewritten = rewrite_query(
        "SELECT id FROM raw.events "
        'WHERE id IN (WITH "raw.events" AS (SELECT 1 AS id) SELECT id FROM "raw.events")',
        sources={"raw.events": _parquet("a.parquet")},
    )

    assert f"SELECT id FROM {_cte_alias('raw.events')} AS events WHERE" in rewritten.sql
    assert '"raw.events" AS (SELECT 1 AS id) SELECT id FROM "raw.events"' in rewritten.sql
    assert len(_READER_CALL.findall(rewritten.sql)) == 1
    assert list(rewritten.parameters.values()) == [["a.parquet"]]


def test_rewrite_replaces_a_reference_with_a_scoped_read() -> None:
    """A qualified table name becomes a CTE reading only the given URLs."""
    rewritten = rewrite_query(
        "SELECT id FROM bronze.weather",
        sources={"bronze.weather": _parquet("https://example/a.parquet")},
    )

    assert _READER_CALL.search(rewritten.sql)
    assert list(rewritten.parameters.values()) == [["https://example/a.parquet"]]
    assert "bronze.weather" not in rewritten.sql.replace(_cte_alias("bronze.weather"), "")


def test_rewrite_never_puts_a_resolved_url_in_the_sql() -> None:
    """The resolved URL is set as a variable, never rendered — the whole injection defence.

    A storage object key is attacker-controlled text (a raw-zone landing
    area accepts arbitrary writes by design), so a key containing a single
    quote would close an interpolated string literal and run the rest of the
    key as SQL inside another principal's authorized query.
    """
    evil = "a.parquet']) UNION ALL SELECT 42 AS id FROM (SELECT ['"

    rewritten = rewrite_query("SELECT id FROM raw.events", sources={"raw.events": _parquet(evil)})

    assert evil not in rewritten.sql
    assert rewritten.sql.count("'") == 2  # only the quotes of the variable's name
    assert "UNION" not in rewritten.sql
    assert list(rewritten.parameters.values()) == [[evil]]


def test_rewrite_keeps_the_original_table_alias() -> None:
    """Dropping the alias left the rest of the query unable to resolve `e.id`."""
    rewritten = rewrite_query(
        "SELECT e.id FROM raw.events AS e", sources={"raw.events": _parquet("a.parquet")}
    )

    assert f"{_cte_alias('raw.events')} AS e" in rewritten.sql
    assert "e.id" in rewritten.sql


def test_rewrite_aliases_an_unaliased_read_by_the_table_name() -> None:
    """`orders.id` must find the read, which is only known by its generated name otherwise."""
    rewritten = rewrite_query(
        "SELECT orders.id FROM gold.orders", sources={"gold.orders": _parquet("a.parquet")}
    )

    assert f"FROM {_cte_alias('gold.orders')} AS orders" in rewritten.sql
    assert rewritten.sql.endswith(
        "SELECT orders.id FROM " + _cte_alias("gold.orders") + " AS orders"
    )


def test_rewrite_keeps_the_quoting_of_the_table_name_it_uses_as_alias() -> None:
    """A table written with quotes keeps them, so `"Orders".id` still matches."""
    rewritten = rewrite_query(
        'SELECT "Orders".id FROM gold."Orders"', sources={"gold.Orders": _parquet("a.parquet")}
    )

    assert 'AS "Orders"' in rewritten.sql


def test_rewrite_cuts_the_schema_off_a_fully_qualified_column() -> None:
    """`gold.orders.id` becomes `orders.id`; other dotted columns stay as written."""
    rewritten = rewrite_query(
        "SELECT gold.orders.id, other.orders.id, orders.address.country FROM gold.orders",
        sources={"gold.orders": _parquet("a.parquet")},
    )

    assert "SELECT orders.id, other.orders.id, orders.address.country FROM" in rewritten.sql


def test_rewrite_cuts_the_catalog_and_schema_off_a_three_part_column() -> None:
    """A table with a catalog part is reduced to its name the same way."""
    rewritten = rewrite_query(
        "SELECT a.b.c.id FROM a.b.c", sources={"a.b.c": _parquet("a.parquet")}
    )

    assert "SELECT c.id FROM" in rewritten.sql


def test_rewrite_leaves_a_qualified_column_alone_when_the_table_has_an_alias() -> None:
    """With an alias, `gold.orders.id` is not valid for the database either and stays as typed."""
    rewritten = rewrite_query(
        "SELECT gold.orders.id FROM gold.orders AS o",
        sources={"gold.orders": _parquet("a.parquet")},
    )

    assert "SELECT gold.orders.id FROM" in rewritten.sql


def test_rewrite_keeps_the_internal_name_for_two_unaliased_tables_of_one_name() -> None:
    """Both would be called `orders`, which the database refuses, so neither gets the alias."""
    rewritten = rewrite_query(
        "SELECT 1 FROM gold.orders JOIN silver.orders ON true",
        sources={"gold.orders": _parquet("a.parquet"), "silver.orders": _parquet("b.parquet")},
    )

    assert " AS orders" not in rewritten.sql


def test_rewrite_leaves_qualifiers_alone_when_two_reads_share_a_name() -> None:
    """A clash fails in the database as it always did; nothing is bound to one of the two."""
    rewritten = rewrite_query(
        "SELECT orders.id, gold.orders.id FROM gold.orders JOIN silver.orders ON true",
        sources={"gold.orders": _parquet("a.parquet"), "silver.orders": _parquet("b.parquet")},
    )

    assert "SELECT orders.id, gold.orders.id FROM" in rewritten.sql


def test_rewrite_gives_no_alias_when_the_name_is_used_in_another_scope() -> None:
    """The outer read and the inner one are both `orders`: neither takes the name."""
    rewritten = rewrite_query(
        "SELECT id FROM gold.orders WHERE EXISTS "
        "(SELECT 1 FROM silver.orders WHERE silver.orders.id = gold.orders.id)",
        sources={"gold.orders": _parquet("a.parquet"), "silver.orders": _parquet("b.parquet")},
    )

    assert " AS orders" not in rewritten.sql
    assert "silver.orders.id = gold.orders.id" in rewritten.sql


@pytest.mark.parametrize(
    "sql",
    [
        "WITH orders AS (SELECT 1 AS note) SELECT orders.note FROM orders, gold.orders",
        "SELECT orders.x FROM silver.orders AS orders JOIN gold.orders ON true",
        "SELECT 1 FROM gold.orders WHERE EXISTS (SELECT 1 FROM (SELECT 3 AS id) AS orders)",
    ],
)
def test_rewrite_gives_no_alias_when_another_source_is_called_the_same(sql: str) -> None:
    """A CTE, an explicit alias or a derived table of that name stays the only one."""
    rewritten = rewrite_query(
        sql,
        sources={"gold.orders": _parquet("a.parquet"), "silver.orders": _parquet("b.parquet")},
    )

    assert _cte_alias("gold.orders") + " AS orders" not in rewritten.sql


def test_rewrite_cuts_the_schema_only_in_the_scope_of_the_read() -> None:
    """In a subquery or a CTE body the full name stays as written; the database refuses it."""
    rewritten = rewrite_query(
        "WITH x AS (SELECT gold.orders.id FROM raw.items) "
        "SELECT gold.orders.id, (SELECT gold.orders.id) FROM gold.orders "
        "WHERE id IN (SELECT gold.orders.id)",
        sources={"gold.orders": _parquet("a.parquet")},
    )

    assert rewritten.sql.count("gold.orders.id") == 3
    assert "SELECT orders.id," in rewritten.sql


def test_rewrite_leaves_a_qualifier_alone_in_a_sibling_branch() -> None:
    """The other branch of a UNION cannot see the read, so its column is not rebound."""
    rewritten = rewrite_query(
        "SELECT id FROM gold.orders UNION ALL SELECT gold.orders.id FROM raw.other",
        sources={"gold.orders": _parquet("a.parquet")},
    )

    assert "SELECT gold.orders.id FROM raw.other" in rewritten.sql


def test_rewrite_merges_its_ctes_ahead_of_the_querys_own() -> None:
    """A query's own CTE may read a rewritten table, so the generated CTE comes first."""
    rewritten = rewrite_query(
        "WITH x AS (SELECT id FROM raw.events) SELECT id FROM x",
        sources={"raw.events": _parquet("a.parquet")},
    )

    assert rewritten.sql.index(f"{_cte_alias('raw.events')} AS (") < rewritten.sql.index("x AS (")
    assert f"x AS (SELECT id FROM {_cte_alias('raw.events')} AS events)" in rewritten.sql
    assert list(rewritten.parameters.values()) == [["a.parquet"]]


def test_rewrite_emits_one_cte_for_a_table_referenced_twice() -> None:
    """A self-join names the same table twice; a duplicate CTE name is an error."""
    rewritten = rewrite_query(
        "SELECT a.id FROM gold.sales AS a JOIN gold.sales AS b ON a.id = b.id",
        sources={"gold.sales": _parquet("a.parquet")},
    )

    assert rewritten.sql.count(f"{_cte_alias('gold.sales')} AS (") == 1
    assert list(rewritten.parameters.values()) == [["a.parquet"]]


def test_rewrite_binds_one_parameter_per_cte_in_placeholder_order() -> None:
    """Each CTE reads its own variable, in the order the CTEs are generated."""
    rewritten = rewrite_query(
        "SELECT a.id FROM bronze.a AS a JOIN gold.b AS b ON a.id = b.id",
        sources={"bronze.a": _parquet("a.parquet"), "gold.b": _parquet("b.csv")},
    )

    first, second = rewritten.parameters.values()
    assert rewritten.sql.index(f"{_cte_alias('bronze.a')} AS (") < rewritten.sql.index(
        f"{_cte_alias('gold.b')} AS ("
    )
    assert first == ["a.parquet"]
    assert second == ["b.csv"]


def test_rewrite_reads_a_table_with_no_files_as_a_typed_empty_relation() -> None:
    """An Iceberg table before its first snapshot has no files; that is zero rows, not an error."""
    rewritten = rewrite_query(
        "SELECT id FROM gold.sales",
        sources={
            "gold.sales": TableSource(
                urls=[], file_format="parquet", columns={"id": "long", "region": "string"}
            )
        },
    )

    assert "READ_PARQUET" not in rewritten.sql
    assert 'CAST(NULL AS BIGINT) AS "id"' in rewritten.sql
    assert "WHERE FALSE" in rewritten.sql
    assert rewritten.parameters == {}


def test_rewrite_rejects_a_table_with_neither_files_nor_declared_columns() -> None:
    """Without a declared schema there is nothing to shape an empty relation from."""
    with pytest.raises(ValidationError, match="no declared columns"):
        rewrite_query(
            "SELECT id FROM gold.sales",
            sources={"gold.sales": TableSource(urls=[], file_format="parquet")},
        )


def test_rewrite_rejects_a_statement_that_is_not_a_query() -> None:
    """A DELETE naming a catalog table has nowhere to attach a CTE and no meaning here."""
    with pytest.raises(ValidationError, match="DELETE"):
        rewrite_query("DELETE FROM gold.sales", sources={"gold.sales": _parquet("a.parquet")})


@pytest.mark.parametrize(
    ("sql", "key"),
    [
        ("PIVOT gold.sales ON a USING sum(b)", "PIVOT"),
        ("SELECT 1 AS a; SELECT b FROM gold.sales", "BLOCK"),
    ],
)
def test_rewrite_rejects_a_catalog_table_under_a_statement_that_is_no_query(
    sql: str, key: str
) -> None:
    """Defence in depth: a top-level `PIVOT` or a `;`-separated block passes the walk.

    The statement has no scope to attach a CTE to, so the rewrite refuses it itself, naming
    what it parsed to.

    Parameters
    ----------
    sql
        A statement that is not an `exp.Query` and names a catalog table.
    key
        The statement kind the message names.
    """
    with pytest.raises(ValidationError, match=f"{key} may not reference a catalog table"):
        rewrite_query(sql, sources={"gold.sales": _parquet("a.parquet")})


def test_rewrite_leaves_a_reference_not_in_sources_untouched() -> None:
    """A table reference outside `sources` is not this call's concern and is left alone."""
    rewritten = rewrite_query(
        "SELECT a.id FROM bronze.a AS a JOIN gold.b AS b ON a.id = b.id",
        sources={"bronze.a": _parquet("https://example/a.parquet")},
    )

    assert _READER_CALL.search(rewritten.sql)
    assert "gold.b" in rewritten.sql


def test_rewrite_returns_the_original_sql_when_sources_matches_nothing() -> None:
    """When no table in the query is a key in `sources`, the SQL is returned unchanged."""
    sql = "SELECT id FROM bronze.weather"

    rewritten = rewrite_query(sql, sources={"gold.sales": _parquet("https://example/a.parquet")})

    assert rewritten.sql == sql
    assert rewritten.parameters == {}
    assert "WITH" not in rewritten.sql


def test_blacklist_rejects_a_direct_read_parquet_call() -> None:
    """A user cannot bypass the catalog by calling a file-reading function directly."""
    with pytest.raises(ValidationError, match="read_parquet"):
        check_blacklist("SELECT * FROM read_parquet('s3://other-bucket/x.parquet')")


def test_blacklist_rejects_a_direct_read_json_call() -> None:
    """read_json parses as exp.Anonymous; the blacklist must still catch it."""
    with pytest.raises(ValidationError, match="read_json"):
        check_blacklist("SELECT * FROM read_json('s3://other-bucket/x.json')")


def test_blacklist_rejects_a_direct_read_json_auto_call() -> None:
    """read_json_auto parses as exp.Anonymous; the blacklist must still catch it."""
    with pytest.raises(ValidationError, match="read_json_auto"):
        check_blacklist("SELECT * FROM read_json_auto('s3://other-bucket/x.json')")


@pytest.mark.parametrize(
    "function_name",
    [
        "read_parquet",
        "parquet_scan",
        "read_csv",
        "read_csv_auto",
        "read_json",
        "read_json_auto",
        "read_ndjson",
        "read_json_objects",
        "read_text",
        "read_blob",
        "glob",
        "query",
        "query_table",
        "sniff_csv",
        "parquet_metadata",
        "parquet_schema",
        "parquet_file_metadata",
        "parquet_kv_metadata",
        "parquet_full_metadata",
        "parquet_bloom_probe",
        "read_duckdb",
        "read_json_objects_auto",
        "read_ndjson_auto",
        "read_ndjson_objects",
        "json_execute_serialized_sql",
        "which_secret",
    ],
)
def test_blacklist_rejects_every_file_reading_function(function_name: str) -> None:
    """Every DuckDB file reader and its aliases are rejected, not just the canonical names.

    Parameters
    ----------
    function_name
        The reader function the query calls directly.
    """
    with pytest.raises(ValidationError, match=function_name):
        check_blacklist(f"SELECT * FROM {function_name}('/etc/passwd')")


def test_blacklist_rejects_a_quoted_function_name() -> None:
    """Quoted function names must be caught by the blacklist too.

    When a function name is written with quotes (e.g., `"read_text"(...)`),
    sqlglot represents it as an Identifier with quoted=True. The bare name
    extraction must handle this case to prevent bypasses.
    """
    with pytest.raises(ValidationError, match="read_text"):
        check_blacklist("SELECT * FROM \"read_text\"('/etc/passwd')")


def test_blacklist_allows_the_glob_operator() -> None:
    """`GLOB` as a predicate is not the `glob()` table function and stays allowed."""
    check_blacklist("SELECT id FROM bronze.weather WHERE name GLOB 'a*'")


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("ATTACH 'file:///etc/passwd' AS x", "ATTACH"),
        ("COPY bronze.weather TO '/tmp/out.csv'", "COPY"),
        ("INSTALL httpfs", "INSTALL"),
        ("LOAD httpfs", "LOAD"),
        ("CREATE SECRET s (TYPE S3, PROVIDER credential_chain)", "CREATE"),
        ("SET s3_access_key_id = 'AKIA'", "SET"),
        ("PRAGMA database_list", "PRAGMA"),
        ("DESCRIBE bronze.weather", "DESCRIBE"),
        ("CREATE TABLE x AS SELECT 1", "CREATE"),
        ("EXPLAIN SELECT 1", "Only a single query"),
        ("CALL dbgen(sf = 1)", "Only a single query"),
        ("SELECT 1; ATTACH 'file:///etc/passwd' AS x", "ATTACH"),
        ("SELECT 1; LOAD httpfs", "LOAD"),
        ("SELECT id FROM bronze.weather; SET s3_access_key_id = 'AKIA'", "Only a single query"),
    ],
)
def test_blacklist_rejects_anything_that_is_not_a_single_query(
    statement: str, message: str
) -> None:
    """Only a query may be submitted: every other statement shape is refused.

    Parameters
    ----------
    statement
        SQL that is not a single plain query.
    message
        Text the resulting error must name, so the rejection is legible.
    """
    with pytest.raises(ValidationError, match=message):
        check_blacklist(statement)


def test_blacklist_allows_ordinary_table_references() -> None:
    """A normal query against catalog tables is never rejected."""
    check_blacklist("SELECT id FROM bronze.weather WHERE temp_c > 0")


def test_blacklist_allows_a_function_call_that_is_not_blacklisted() -> None:
    """A function call outside the blacklist is inspected and let through, not rejected."""
    check_blacklist("SELECT COUNT(*) FROM bronze.weather")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT o.id, c.name FROM gold.orders AS o "
        "JOIN gold.customers AS c ON o.customer_id = c.id",
        "WITH recent AS (SELECT id FROM bronze.weather) SELECT id FROM recent",
        "SELECT id FROM bronze.a UNION SELECT id FROM bronze.b",
        "(SELECT id FROM bronze.weather)",
    ],
)
def test_blacklist_allows_every_query_shape(sql: str) -> None:
    """The structural rule admits every shape a legitimate read takes.

    Parameters
    ----------
    sql
        A query that must pass the check unchanged.
    """
    check_blacklist(sql)


_SECRET = '"/tmp/secret.csv"'
"""A quoted path DuckDB resolves to a real file by replacement scan.

Stands in for the file, URL, or catalog table a `WITH RECURSIVE` base term
reads while sqlglot believes the name is bound to the CTE being built.
"""

_RECURSIVE_BYPASS_SPELLINGS = {
    "union_all_base": (
        f"WITH RECURSIVE {_SECRET} AS ("
        f"SELECT val FROM {_SECRET} UNION ALL SELECT val FROM {_SECRET} WHERE FALSE"
        f") SELECT * FROM {_SECRET}"
    ),
    "union_distinct_base": (
        f"WITH RECURSIVE {_SECRET} AS ("
        f"SELECT val FROM {_SECRET} UNION SELECT val FROM {_SECRET} WHERE FALSE"
        f") SELECT * FROM {_SECRET}"
    ),
    "except_base": (
        f"WITH RECURSIVE {_SECRET} AS ("
        f"SELECT val FROM {_SECRET} EXCEPT SELECT val FROM {_SECRET} WHERE FALSE"
        f") SELECT * FROM {_SECRET}"
    ),
    "intersect_base": (
        f"WITH RECURSIVE {_SECRET} AS ("
        f"SELECT val FROM {_SECRET} INTERSECT SELECT val FROM {_SECRET} WHERE FALSE"
        f") SELECT * FROM {_SECRET}"
    ),
    "nested_set_operation_base": (
        f"WITH RECURSIVE {_SECRET} AS ("
        f"(SELECT val FROM {_SECRET} UNION ALL SELECT val FROM {_SECRET} WHERE FALSE) "
        f"UNION ALL SELECT val FROM {_SECRET} WHERE FALSE"
        f") SELECT * FROM {_SECRET}"
    ),
    "with_clause_inside_base": (
        f"WITH RECURSIVE {_SECRET} AS ("
        f"WITH leaked AS (SELECT val FROM {_SECRET}) SELECT val FROM leaked "
        f"UNION ALL SELECT val FROM {_SECRET} WHERE FALSE"
        f") SELECT * FROM {_SECRET}"
    ),
    "derived_table_inside_base": (
        f"WITH RECURSIVE {_SECRET} AS ("
        f"SELECT val FROM (SELECT val FROM {_SECRET}) AS d "
        f"UNION ALL SELECT val FROM {_SECRET} WHERE FALSE"
        f") SELECT * FROM {_SECRET}"
    ),
    "nested_in_a_subquery": (
        f"SELECT * FROM (WITH RECURSIVE {_SECRET} AS ("
        f"SELECT val FROM {_SECRET} UNION ALL SELECT val FROM {_SECRET} WHERE FALSE"
        f") SELECT * FROM {_SECRET}) AS q"
    ),
    "nested_in_an_in_predicate": (
        f"SELECT 1 WHERE 1 IN (WITH RECURSIVE {_SECRET} AS ("
        f"SELECT val FROM {_SECRET} UNION ALL SELECT val FROM {_SECRET} WHERE FALSE"
        f") SELECT * FROM {_SECRET})"
    ),
    "beside_a_legitimate_table": (
        f"WITH RECURSIVE {_SECRET} AS ("
        f"SELECT val FROM {_SECRET} UNION ALL SELECT val FROM {_SECRET} WHERE FALSE"
        f") SELECT * FROM {_SECRET}, bronze.weather"
    ),
}
"""Every reconstructed spelling of the base-term bypass, each rejected outright.

sqlglot's `optimizer/scope.py::_traverse_ctes` pre-seeds a recursive CTE's
own name into `Scope.cte_sources` *before* walking its body, and a set
operation inherits that binding into **both** branches. DuckDB only resolves
the name to the CTE in the recursive term; in the base term the same name
still resolves to a real relation — a file, an HTTP(S) URL, or a catalog
table. So every one of these read something real while `extract_table_refs`
reported nothing and no authorization call was made at all.
"""


@pytest.mark.parametrize("spelling", sorted(_RECURSIVE_BYPASS_SPELLINGS))
def test_blacklist_rejects_every_recursive_cte_bypass_spelling(spelling: str) -> None:
    """No spelling of the recursive-CTE base-term bypass survives the blacklist.

    Parameters
    ----------
    spelling
        Key into `_RECURSIVE_BYPASS_SPELLINGS` naming the shape under test.
    """
    with pytest.raises(ValidationError, match="Recursive CTEs"):
        check_blacklist(_RECURSIVE_BYPASS_SPELLINGS[spelling])


def test_blacklist_rejects_an_entirely_innocent_recursive_cte() -> None:
    """The feature is closed, not one malicious use of it.

    A counting query that touches no external relation at all is refused
    just the same: the rejection is structural, so there is no intent to
    judge and no second reading of DuckDB's binding rules to get wrong.
    """
    sql = (
        "WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r WHERE n < 3) "
        "SELECT n FROM r"
    )

    with pytest.raises(ValidationError, match="Recursive CTEs"):
        check_blacklist(sql)


def test_blacklist_allows_a_non_recursive_cte_named_like_a_recursive_one() -> None:
    """Only the `RECURSIVE` keyword is rejected, not CTEs or set operations.

    The flag read is `exp.With.args["recursive"]`, which is `True` only when
    the keyword was written; an ordinary `WITH` over a `UNION ALL` body is
    exactly the shape the bypass wore, and it must still run.
    """
    check_blacklist(
        "WITH recursive_totals AS (SELECT id FROM bronze.a UNION ALL SELECT id FROM bronze.b) "
        "SELECT id FROM recursive_totals"
    )


def test_extract_table_refs_rejects_a_recursive_cte() -> None:
    """Defence in depth: the classifier refuses what it cannot classify soundly.

    `check_blacklist` runs first in `lakana_worker.pipeline.run_one`, so this
    is unreachable through the real pipeline today. It is asserted anyway
    because the guarantee this function owes its caller is that every name
    it leaves out is genuinely not a table — and against a recursive CTE it
    cannot keep that promise.
    """
    with pytest.raises(ValidationError, match="Recursive CTEs"):
        extract_table_refs(_RECURSIVE_BYPASS_SPELLINGS["union_all_base"])


def test_rewrite_query_rejects_a_recursive_cte() -> None:
    """The rewriter refuses a recursive CTE independently of the other two checks."""
    with pytest.raises(ValidationError, match="Recursive CTEs"):
        rewrite_query(
            _RECURSIVE_BYPASS_SPELLINGS["union_all_base"],
            {"/tmp/secret.csv": _parquet("s3://bucket/part-0.parquet")},
        )


def test_a_table_with_no_policy_still_gets_a_bare_select_star() -> None:
    """No row_filters/column_masks means today's SELECT * FROM reader($name), byte for byte."""
    source = TableSource(urls=["s3://bucket/a.parquet"], file_format="parquet")

    rewritten = rewrite_query("SELECT * FROM gold.customers", {"gold.customers": source})

    assert re.search(r"SELECT \* FROM " + _READER_CALL.pattern, rewritten.sql)


def test_a_column_mask_replaces_the_column_in_the_generated_cte() -> None:
    """A masked column's real value never appears in the generated SQL's projection list."""
    source = TableSource(
        urls=["s3://bucket/a.parquet"],
        file_format="parquet",
        columns={"id": "BIGINT", "ssn": "VARCHAR"},
        column_masks={"ssn": "NULL"},
    )

    rewritten = rewrite_query("SELECT * FROM gold.customers", {"gold.customers": source})

    assert "NULL AS" in rewritten.sql
    assert '"ssn"' in rewritten.sql
    # The outer query's own SELECT * (now reading the masked CTE's output,
    # not the raw file) is untouched and expected; what must never appear
    # is the generated CTE's own body still being a blind SELECT * against
    # the reader.
    assert "* FROM READ_PARQUET" not in rewritten.sql


def test_a_row_filter_adds_a_where_clause_to_the_generated_cte() -> None:
    """A row filter's predicate is ANDed into the generated CTE's own WHERE clause."""
    source = TableSource(
        urls=["s3://bucket/a.parquet"],
        file_format="parquet",
        columns={"id": "BIGINT", "region": "VARCHAR"},
        row_filters=["region = 'EU'"],
    )

    rewritten = rewrite_query("SELECT * FROM gold.customers", {"gold.customers": source})

    assert "WHERE (region = 'EU')" in rewritten.sql


def test_a_generated_statement_that_does_not_parse_names_no_policy_text() -> None:
    """N3: a stored filter that stops parsing fails the rewrite without quoting it."""
    source = TableSource(
        urls=["s3://bucket/a.parquet"],
        file_format="parquet",
        columns={"id": "BIGINT"},
        row_filters=["SECRETWORD ((( +"],
    )

    with pytest.raises(ValidationError, match="Ask an administrator") as info:
        rewrite_query("SELECT * FROM gold.customers", {"gold.customers": source})
    assert "SECRETWORD" not in str(info.value)


def test_multiple_row_filters_are_anded_together() -> None:
    """Two applicable row filters both appear, ANDed."""
    source = TableSource(
        urls=["s3://bucket/a.parquet"],
        file_format="parquet",
        columns={"id": "BIGINT", "region": "VARCHAR"},
        row_filters=["region = 'EU'", "id > 0"],
    )

    rewritten = rewrite_query("SELECT * FROM gold.customers", {"gold.customers": source})

    assert "WHERE (region = 'EU') AND (id > 0)" in rewritten.sql


def test_a_masked_column_survives_a_join_through_a_nested_cte() -> None:
    """Masking survives nesting: the case that made the rewriter wrap every base-table reference."""
    customers = TableSource(
        urls=["s3://bucket/customers.parquet"],
        file_format="parquet",
        columns={"id": "BIGINT", "name": "VARCHAR", "ssn": "VARCHAR", "region": "VARCHAR"},
        column_masks={"ssn": "NULL"},
    )
    orders = TableSource(urls=["s3://bucket/orders.parquet"], file_format="parquet")
    sql = (
        "WITH enriched AS ("
        "  SELECT o.id, o.amount, c.name, c.ssn, c.region"
        "  FROM gold.orders o JOIN gold.customers c ON o.customer_id = c.id"
        ") SELECT * FROM enriched WHERE region = 'EU'"
    )

    rewritten = rewrite_query(sql, {"gold.orders": orders, "gold.customers": customers})

    assert "NULL AS" in rewritten.sql
    assert '"ssn"' in rewritten.sql
    # The outer query's own structure — the enriched CTE, the join, the
    # final WHERE — is untouched; only the customers CTE's own body changed.
    assert "WHERE region = 'EU'" in rewritten.sql


def test_a_policy_leaves_the_variables_unaffected() -> None:
    """A policy changes the generated CTE's body, never the variables set for it."""
    source = TableSource(
        urls=["s3://bucket/a.parquet"],
        file_format="parquet",
        columns={"id": "BIGINT", "ssn": "VARCHAR"},
        row_filters=["id > 0"],
        column_masks={"ssn": "NULL"},
    )

    rewritten = rewrite_query("SELECT * FROM gold.customers", {"gold.customers": source})

    assert list(rewritten.parameters.values()) == [["s3://bucket/a.parquet"]]
    assert rewritten.sql.count("GETVARIABLE") == 1
    assert "?" not in rewritten.sql


def test_a_column_mask_on_an_unknown_column_raises() -> None:
    """A mask keyed to a column absent from the schema fails closed, not silently unmasked.

    Without this check, a typo or case mismatch in a `ColumnMask.column`
    value (`ColumnMaskService.create` does no schema validation) would
    silently select the real, unmasked value with no diagnostic anywhere.
    """
    source = TableSource(
        urls=["s3://bucket/a.parquet"],
        file_format="parquet",
        columns={"id": "BIGINT", "ssn": "VARCHAR"},
        column_masks={"SSN": "NULL"},
    )

    with pytest.raises(ValidationError, match="unknown column 'SSN'"):
        rewrite_query("SELECT * FROM gold.customers", {"gold.customers": source})


def test_a_policy_on_a_table_with_no_declared_columns_raises() -> None:
    """Without a declared column list there is nothing to build an explicit projection from.

    Falling through silently would render `SELECT  FROM read_parquet($name)` —
    syntactically invalid SQL DuckDB would reject with an opaque parser
    error far from the real cause.
    """
    source = TableSource(
        urls=["s3://bucket/a.parquet"], file_format="parquet", row_filters=["id > 0"]
    )

    with pytest.raises(ValidationError, match="no declared column list"):
        rewrite_query("SELECT * FROM gold.customers", {"gold.customers": source})


def test_cte_alias_distinguishes_names_that_sanitize_identically() -> None:
    """`a.b_c` and `a_b.c` sanitize to the same text but get different aliases."""
    assert _cte_alias("a.b_c") != _cte_alias("a_b.c")
    assert _cte_alias("a.b_c").startswith("__lakana_a_b_c_")
    assert _cte_alias("a.b_c") == _cte_alias("a.b_c")


def test_rewrite_refuses_an_alias_claimed_by_two_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the derivation ever collides again, the rewrite fails loudly instead of reusing."""
    import lakana_core.query_rewrite as query_rewrite

    monkeypatch.setattr(query_rewrite, "_cte_alias", lambda dotted: "__lakana_same")
    with pytest.raises(ValidationError, match="__lakana_same"):
        rewrite_query(
            "SELECT * FROM a.b UNION ALL SELECT * FROM c.d",
            sources={"a.b": _parquet("x.parquet"), "c.d": _parquet("y.parquet")},
        )


def test_extract_table_refs_skips_table_functions() -> None:
    """A table function is not a table reference; a real table beside it still is."""
    sql = "SELECT * FROM bronze.x AS b, range(3) AS r(i), generate_series(1, 2) AS g(j)"
    assert extract_table_refs(sql) == ["bronze.x"]


@pytest.mark.parametrize("sql", ["SELECT * FROM range(3)", "SELECT * FROM unnest([1])"])
def test_blacklist_allows_generator_table_functions(sql: str) -> None:
    """Pure generators read nothing, so the guard lets them through."""
    check_blacklist(sql)


@pytest.mark.parametrize(
    "function", ["read_parquet", "read_csv_auto", "iceberg_scan", "postgres_scan", "mystery"]
)
def test_blacklist_refuses_other_table_functions(function: str) -> None:
    """Every table function outside the generator allow-list is refused with guidance."""
    with pytest.raises(ValidationError, match=f"table function {function} is not allowed"):
        check_blacklist(f"SELECT * FROM {function}('x')")


_LATERAL_BYPASSES = [
    ("SELECT * FROM (SELECT 1 a) b, LATERAL iceberg_scan('x')", "iceberg_scan"),
    ("SELECT * FROM (SELECT 1 a) b, LATERAL delta_scan('x')", "delta_scan"),
    ("SELECT * FROM (SELECT 1 a) b, LATERAL sqlite_scan('x', 't')", "sqlite_scan"),
    ("SELECT * FROM (SELECT 1 a) b JOIN LATERAL duckdb_types() ON true", "duckdb_types"),
    ("SELECT * FROM (SELECT 1 a) b, LATERAL duckdb_functions()", "duckdb_functions"),
    ("SELECT * FROM (SELECT 1 a) b, LATERAL test_all_types()", "test_all_types"),
    ("SELECT * FROM (SELECT 1 a) b, LATERAL json_each('[1]')", "json_each"),
    ("SELECT * FROM (SELECT 1 a) b, LATERAL repeat_row(1, 2)", "repeat_row"),
    ("SELECT * FROM table(iceberg_scan('x'))", "iceberg_scan"),
    ("SELECT * FROM table(mystery('x'))", "mystery"),
    ("SELECT * FROM table(1)", "table"),
]


@pytest.mark.parametrize(("sql", "function"), _LATERAL_BYPASSES)
def test_blacklist_refuses_table_functions_in_any_from_position(sql: str, function: str) -> None:
    """`LATERAL fn(...)` and `TABLE(fn(...))` are held to the same allow-list as `FROM fn(...)`."""
    with pytest.raises(ValidationError, match=f"table function {function} is not allowed"):
        check_blacklist(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM (SELECT 1 a) b, LATERAL range(3)",
        "SELECT * FROM (SELECT [1] a) b, LATERAL unnest(b.a)",
        "SELECT * FROM (SELECT 1 a) b JOIN LATERAL generate_series(1, 2) ON true",
    ],
)
def test_lateral_generators_stay_allowed_and_are_no_references(sql: str) -> None:
    """A lateral generator is allowed and contributes no table reference."""
    check_blacklist(sql)
    assert extract_table_refs(sql) == []


def test_extract_table_refs_never_returns_an_empty_name() -> None:
    """`ROWS FROM (...)` yields a nameless table node that must not become a reference."""
    assert extract_table_refs("SELECT * FROM ROWS FROM (range(3))") == []


def test_blacklist_refuses_rows_from() -> None:
    """`ROWS FROM (...)` is refused rather than half-understood."""
    with pytest.raises(ValidationError, match="table function rows from is not allowed"):
        check_blacklist("SELECT * FROM ROWS FROM (iceberg_scan('x'))")


@pytest.mark.parametrize(
    "function",
    [
        "iceberg_scan",
        "delta_scan",
        "sqlite_scan",
        "postgres_scan",
        "mysql_scan",
        "postgres_query",
        "mysql_query",
        "sqlite_attach",
    ],
)
def test_foreign_scanners_are_blacklisted_by_name_in_any_position(function: str) -> None:
    """Foreign scanners are refused even as a scalar-position call."""
    with pytest.raises(ValidationError, match=function):
        check_blacklist(f"SELECT {function}('x')")


_QUALIFIED_LATERAL = [
    ("SELECT * FROM (SELECT 1 a) b, LATERAL main.duckdb_types()", "duckdb_types"),
    ("SELECT * FROM (SELECT 1 a) b, LATERAL system.main.duckdb_functions()", "duckdb_functions"),
    ('SELECT * FROM (SELECT 1 a) b, LATERAL "main"."duckdb_types"()', "duckdb_types"),
    ("SELECT * FROM (SELECT 1 a) b, LATERAL main.test_all_types()", "test_all_types"),
    ("SELECT * FROM (SELECT 1 a) b, LATERAL main.json_each('[1]')", "json_each"),
    (
        "SELECT * FROM (SELECT 1 a) b, LATERAL "
        "(SELECT * FROM (SELECT 2 c) d, LATERAL duckdb_types()) s",
        "duckdb_types",
    ),
]


@pytest.mark.parametrize(("sql", "function"), _QUALIFIED_LATERAL)
def test_blacklist_refuses_schema_qualified_and_nested_lateral_functions(
    sql: str, function: str
) -> None:
    """A schema-qualified or nested `LATERAL fn()` meets the same allow-list."""
    with pytest.raises(ValidationError, match=f"table function {function} is not allowed"):
        check_blacklist(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM (SELECT 1 a) b, LATERAL main.range(3)",
        "SELECT * FROM (SELECT 1 a) b, LATERAL range(3) AS r(i)",
        "SELECT * FROM (SELECT 1 a) b, LATERAL (SELECT 2 c) s",
    ],
)
def test_qualified_lateral_generators_and_subqueries_stay_allowed(sql: str) -> None:
    """Generators (qualified or aliased) and lateral subqueries are allowed, no references."""
    check_blacklist(sql)
    assert extract_table_refs(sql) == []


def test_an_unrecognised_lateral_shape_is_refused() -> None:
    """A `LATERAL` that is neither a subquery nor a function call fails closed."""
    node = sqlglot.parse_one("SELECT * FROM t, LATERAL (SELECT 1) s", dialect="duckdb").find(
        exp.Lateral
    )
    assert node is not None
    node.set("this", exp.column("x"))
    assert query_rewrite._from_item_function_name(node) == "lateral"


def test_a_source_that_lists_only_its_declared_columns_never_reads_with_a_star() -> None:
    """Without any policy the files are still read through the explicit column list.

    A read at an older snapshot must not show a column that its files have and the source
    does not declare; ``SELECT *`` over the files would.
    """
    source = TableSource(
        urls=["s3://b/f.parquet"],
        file_format="parquet",
        columns={"id": "long", "name": "string"},
        declared_columns_only=True,
    )

    rewritten = rewrite_query("SELECT * FROM gold.t", {"gold.t": source})

    assert 'SELECT "id", "name" FROM READ_PARQUET(' in rewritten.sql
    assert "* FROM READ_PARQUET" not in rewritten.sql
    assert " WHERE " not in rewritten.sql
    plain = TableSource(urls=["s3://b/f.parquet"], file_format="parquet", columns={"id": "long"})
    assert "* FROM READ_PARQUET" in rewrite_query("SELECT * FROM gold.t", {"gold.t": plain}).sql


def test_a_source_that_lists_only_its_declared_columns_needs_some() -> None:
    """No column to list is refused, never rendered as an empty select list."""
    source = TableSource(
        urls=["s3://b/f.parquet"], file_format="parquet", declared_columns_only=True
    )

    with pytest.raises(ValidationError, match="no declared column list"):
        rewrite_query("SELECT * FROM gold.t", {"gold.t": source})


def test_named_columns_of_a_statement_that_does_not_parse_is_empty() -> None:
    """A statement sqlglot cannot parse names no column, so no error is blamed on the reader."""
    assert query_rewrite.named_columns("SELECT tier FROM (") == frozenset()


_NOT_A_READ = [
    pytest.param("DELETE FROM bronze.t RETURNING *", "DELETE", id="delete"),
    pytest.param("dElEtE fRoM bronze.t ReTuRnInG *", "DELETE", id="delete-mixed-case"),
    pytest.param("INSERT INTO bronze.t VALUES (1) RETURNING *", "INSERT", id="insert"),
    pytest.param(
        "INSERT OR REPLACE INTO bronze.t VALUES (1) RETURNING *", "INSERT", id="insert-or-replace"
    ),
    pytest.param("UPDATE bronze.t SET a = 1 RETURNING *", "UPDATE", id="update"),
    pytest.param(
        "MERGE INTO bronze.t USING bronze.s ON t.a = s.a WHEN MATCHED THEN DELETE",
        "MERGE",
        id="merge",
    ),
    pytest.param("CREATE TABLE x AS SELECT 1", "CREATE", id="create"),
    pytest.param("DROP TABLE x", "DROP", id="drop"),
    pytest.param("ATTACH ':memory:' AS x", "ATTACH", id="attach"),
    pytest.param("DETACH x", "DETACH", id="detach"),
    pytest.param("USE x", "USE", id="use"),
    pytest.param("PRAGMA version", "PRAGMA", id="pragma"),
    pytest.param("BEGIN TRANSACTION", "BEGIN", id="transaction"),
    pytest.param("INSTALL httpfs", "INSTALL", id="install"),
    pytest.param("UPDATE EXTENSIONS", "UPDATE", id="update-extensions"),
    pytest.param("COMMENT ON TABLE bronze.t IS 'x'", "COMMENT", id="comment"),
    pytest.param("DESCRIBE bronze.t", "DESCRIBE", id="describe"),
    pytest.param("SHOW TABLES", "SHOW", id="show"),
    pytest.param("SUMMARIZE SELECT 1", "SUMMARIZE", id="summarize"),
    pytest.param("SELECT 1 AS a INTO bronze.t2", "INTO", id="select-into"),
]
"""Statements that are not a read, each with the keyword its refusal names."""

_STATEMENT_POSITIONS = [
    pytest.param("WITH m AS ({s}) SELECT * FROM m", id="cte"),
    pytest.param("WITH m AS MATERIALIZED ({s}) SELECT * FROM m", id="materialized-cte"),
    pytest.param("WITH ok AS (SELECT 1 AS a), m AS ({s}) SELECT * FROM ok", id="second-cte"),
    pytest.param("WITH a AS (WITH b AS ({s}) SELECT * FROM b) SELECT * FROM a", id="nested-cte"),
    pytest.param("SELECT * FROM (WITH b AS ({s}) SELECT * FROM b) AS q", id="cte-in-subquery"),
    pytest.param(
        "SELECT 1 AS a UNION ALL (WITH b AS ({s}) SELECT * FROM b)", id="cte-in-set-operation"
    ),
    pytest.param(
        "SELECT * FROM bronze.x, LATERAL (WITH b AS ({s}) SELECT * FROM b) AS l",
        id="cte-in-lateral",
    ),
]
"""Every place sqlglot's duckdb dialect parses a statement inside a query (verified 30.22)."""


@pytest.mark.parametrize("position", _STATEMENT_POSITIONS)
@pytest.mark.parametrize(("statement", "keyword"), _NOT_A_READ)
def test_a_statement_that_is_not_a_read_is_refused_wherever_it_sits(
    statement: str, keyword: str, position: str
) -> None:
    """DuckDB 2 runs DML inside a CTE and reports the whole text as a SELECT.

    Parameters
    ----------
    statement
        The statement that is not a read.
    keyword
        The keyword the refusal names.
    position
        Where in the query the statement sits.
    """
    with pytest.raises(ValidationError, match=keyword):
        check_blacklist(position.format(s=statement))


def test_dml_inside_a_cte_is_refused_with_the_reason() -> None:
    """The message says what a query may do and that depth does not matter."""
    with pytest.raises(ValidationError) as caught:
        check_blacklist("WITH m AS (DELETE FROM bronze.t RETURNING *) SELECT * FROM m")

    assert str(caught.value) == (
        "A query reads data and cannot change it: `DELETE` is not allowed, also not inside a "
        "CTE or subquery."
    )


@pytest.mark.parametrize(
    ("sql", "keyword"),
    [
        ("WITH m AS (CHECKPOINT) SELECT 1", "CHECKPOINT"),
        ("WITH m AS (VACUUM) SELECT 1", "VACUUM"),
    ],
)
def test_a_cte_body_sqlglot_reads_as_something_else_is_refused(sql: str, keyword: str) -> None:
    """The parser reads `CHECKPOINT` as a column and `VACUUM` as a command; neither is a query.

    Parameters
    ----------
    sql
        A CTE whose body is a statement sqlglot has no class for.
    keyword
        The keyword the refusal names.
    """
    with pytest.raises(ValidationError, match=f"`{keyword}` is not allowed"):
        check_blacklist(sql)


def test_the_rewrite_refuses_dml_inside_a_cte_on_its_own() -> None:
    """Defence in depth: a caller that skipped `check_blacklist` is refused all the same."""
    with pytest.raises(ValidationError, match="`DELETE` is not allowed"):
        rewrite_query(
            "WITH m AS (DELETE FROM gold.sales RETURNING *) SELECT * FROM m",
            sources={"gold.sales": _parquet("a.parquet")},
        )
    with pytest.raises(ValidationError, match="`DELETE` is not allowed"):
        extract_table_refs("WITH m AS (DELETE FROM gold.sales RETURNING *) SELECT * FROM m")


@pytest.mark.parametrize(
    "sql",
    [
        "WITH m AS (VALUES (1)) SELECT * FROM m",
        "WITH m AS (PIVOT bronze.t ON a USING sum(b)) SELECT * FROM m",
        "WITH m AS (FROM bronze.t) SELECT * FROM m",
        "SELECT * FROM (FROM bronze.t SELECT a) AS s",
        "SELECT * FROM (bronze.v JOIN bronze.x ON v.a = x.a)",
        "SELECT 'DELETE FROM t' AS s",
        'SELECT 1 AS "delete"',
        "SELECT update FROM (SELECT 1 AS update)",
    ],
)
def test_a_read_that_merely_names_a_statement_keyword_passes(sql: str) -> None:
    """Over-refusal guard: reads in DuckDB's own spellings stay allowed.

    Parameters
    ----------
    sql
        A read.
    """
    check_blacklist(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT getvariable('v_0_files') AS u",
        "SELECT GETVARIABLE('v_0_files') AS u",
        "SELECT \"getvariable\"('v_0_files') AS u",
        "SELECT main.getvariable('v_0_files') AS u",
        "SELECT system.main.getvariable('v_0_files') AS u",
        "SELECT list_transform([1], lambda x: getvariable('v_0_files')) AS u",
        "SELECT id FROM bronze.t WHERE getvariable('v_0_files') IS NOT NULL",
        "WITH c AS (SELECT getvariable('v_0_schema') AS s) SELECT * FROM c",
    ],
)
def test_user_sql_cannot_read_a_variable_of_the_connection(sql: str) -> None:
    """The query path sets values as DuckDB variables under predictable names.

    Parameters
    ----------
    sql
        A query that reads a variable.
    """
    with pytest.raises(ValidationError, match="getvariable may not be called directly"):
        check_blacklist(sql)


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("SET VARIABLE v_0_files = ['x']", "not SET"),
        ("RESET VARIABLE v_0_files", "Only a single query"),
    ],
)
def test_user_sql_cannot_set_a_variable_of_the_connection(sql: str, message: str) -> None:
    """Pin: a variable can be neither set nor reset by a submitted statement.

    Parameters
    ----------
    sql
        A statement that changes a variable.
    message
        Text the refusal must contain.
    """
    with pytest.raises(ValidationError, match=message):
        check_blacklist(sql)


def test_a_views_stored_sql_cannot_read_a_variable_either() -> None:
    """A view expands into the reader's query, so its stored SQL is checked the same way."""
    view = ViewDefinition(name="gold.v", sql="SELECT getvariable('v_0_files') AS u")

    with pytest.raises(ValidationError, match="getvariable may not be called directly"):
        expand_views("SELECT * FROM gold.v", lambda dotted: view if dotted == "gold.v" else None)


def test_select_into_is_refused_at_the_top_level() -> None:
    """The rewrite would turn `SELECT ... INTO` into `CREATE TABLE ... AS`, so it is refused."""
    with pytest.raises(ValidationError, match="`INTO` is not allowed"):
        check_blacklist("SELECT x.a INTO main.u FROM bronze.t AS x")


def test_a_statement_with_its_own_with_clause_is_named_by_its_keyword() -> None:
    """The message names `DELETE`, not the `WITH` the statement starts with."""
    sql = "WITH m AS (WITH x AS (SELECT 1 AS a) DELETE FROM bronze.t RETURNING *) SELECT * FROM m"

    with pytest.raises(ValidationError, match="`DELETE` is not allowed"):
        check_blacklist(sql)


def test_rewrite_reads_each_file_list_through_a_variable_of_its_own() -> None:
    """Two reads, two variables, numbered in the order the CTEs are generated; no placeholder."""
    rewritten = rewrite_query(
        "SELECT a.id FROM bronze.a AS a JOIN gold.b AS b ON a.id = b.id",
        sources={"bronze.a": _parquet("a.parquet"), "gold.b": _parquet("b.parquet")},
    )

    assert "READ_PARQUET(GETVARIABLE('v_0_files'))" in rewritten.sql
    assert "READ_PARQUET(GETVARIABLE('v_1_files'))" in rewritten.sql
    assert rewritten.parameters == {"v_0_files": ["a.parquet"], "v_1_files": ["b.parquet"]}
    assert "$" not in rewritten.sql
    assert "?" not in rewritten.sql


def test_a_table_without_files_takes_no_variable_number() -> None:
    """An empty relation reads nothing, so the next read is still `v_1_files`."""
    rewritten = rewrite_query(
        "SELECT a.id FROM bronze.a AS a, gold.empty AS e, gold.c AS c",
        sources={
            "bronze.a": _parquet("a.parquet"),
            "gold.empty": TableSource(urls=[], file_format="parquet", columns={"id": "long"}),
            "gold.c": TableSource(urls=["c.csv"], file_format="csv"),
        },
    )

    assert rewritten.parameters == {"v_0_files": ["a.parquet"], "v_1_files": ["c.csv"]}
    assert "READ_CSV(GETVARIABLE('v_1_files'))" in rewritten.sql


_FIELDS = (ReadField(1, "id", "BIGINT"), ReadField(2, "name", "VARCHAR"))


def test_an_iceberg_read_reads_its_files_by_field_id() -> None:
    """The map is a second variable of the same read, never SQL text."""
    source = TableSource(
        urls=["a.parquet"],
        file_format="parquet",
        columns={"id": "long", "name": "string"},
        field_ids=_FIELDS,
    )

    rewritten = rewrite_query("SELECT * FROM gold.t", {"gold.t": source})

    assert (
        "READ_PARQUET(GETVARIABLE('v_0_files'), schema = GETVARIABLE('v_0_schema'))"
        in rewritten.sql
    )
    assert rewritten.parameters == {"v_0_files": ["a.parquet"], "v_0_schema": _FIELDS}


def test_a_projected_read_by_field_id_keeps_its_policy() -> None:
    """Filters and masks still apply in the CTE over the map's columns."""
    source = TableSource(
        urls=["a.parquet"],
        file_format="parquet",
        columns={"id": "long", "name": "string"},
        row_filters=("id > 1",),
        column_masks={"name": "NULL"},
        field_ids=_FIELDS,
    )

    rewritten = rewrite_query("SELECT * FROM gold.t", {"gold.t": source})

    assert (
        'SELECT "id", CAST(NULL AS TEXT) AS "name" FROM READ_PARQUET(GETVARIABLE(\'v_0_files\'), '
        "schema = GETVARIABLE('v_0_schema')) WHERE (id > 1)"
    ) in rewritten.sql


def test_a_read_by_field_id_of_files_that_are_not_parquet_is_refused() -> None:
    """Field ids belong to Parquet; any other format cannot be read by them."""
    source = TableSource(
        urls=["a.orc"],
        file_format="orc",
        columns={"id": "long", "name": "string"},
        field_ids=_FIELDS,
    )

    with pytest.raises(ValidationError, match="A read by field id needs Parquet files"):
        rewrite_query("SELECT * FROM gold.t", {"gold.t": source})


def test_a_field_id_map_that_misses_a_declared_column_is_refused() -> None:
    """A column outside the map would not be read at all; that is a defect, refused."""
    source = TableSource(
        urls=["a.parquet"],
        file_format="parquet",
        columns={"id": "long", "name": "string"},
        field_ids=_FIELDS[:1],
    )

    with pytest.raises(ValidationError, match="has no field id for its column 'name'"):
        rewrite_query("SELECT * FROM gold.t", {"gold.t": source})


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        pytest.param(
            (*_FIELDS, ReadField(3, "extra", "BIGINT")),
            "names the column 'extra', which the table does not declare",
            id="a name the table does not declare",
        ),
        pytest.param(
            (ReadField(1, "id", "BIGINT"), ReadField(1, "name", "VARCHAR")),
            "field id 1 twice",
            id="a field id twice",
        ),
        pytest.param(
            (
                ReadField(1, "id", "BIGINT"),
                ReadField(2, "id", "VARCHAR"),
                ReadField(3, "name", "X"),
            ),
            "column 'id' twice",
            id="a name twice",
        ),
    ],
)
def test_a_field_id_map_that_is_not_the_declared_columns_is_refused(
    fields: tuple[ReadField, ...], message: str
) -> None:
    source = TableSource(
        urls=["a.parquet"],
        file_format="parquet",
        columns={"id": "long", "name": "string"},
        field_ids=fields,
    )

    with pytest.raises(ValidationError, match=message):
        rewrite_query("SELECT * FROM gold.t", {"gold.t": source})
