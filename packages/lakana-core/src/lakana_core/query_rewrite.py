"""Rewriting submitted SQL to read scoped files instead of catalog tables.

This is sqlglot's "policy rewriting" role: the executor never lets DuckDB resolve a table reference
against ambient storage. Every qualified table name in the query is replaced
with a CTE reading exactly the files the catalog already resolved for it.

Those file URLs are **never interpolated into the SQL**. A URL is derived
from a storage object key, and a raw-zone landing area accepts arbitrary
writes by design, so an object key is attacker-controlled text: a key
containing a single quote would close the string literal an interpolated
list built and run the rest of the key as SQL inside another principal's
authorized query. Instead each CTE reads `read_parquet(getvariable('v_<n>_files'))`
and the query process sets that variable to the URL list, bound as a value
(`SET VARIABLE "v_0_files" = ?`), before it runs the statement, which then
takes no parameters at all — see `RewrittenQuery`.

Those variables must in turn be unreachable from the user's own SQL: on S3
they hold presigned URLs of the raw files, and a caller holding one reads the
file directly, past every row filter and column mask. `getvariable` and
`duckdb_variables` are blacklisted, a `SET` or `RESET` of a variable is a
statement that is not a read and is refused, and every `exp.Placeholder`/
`exp.Parameter` in the user's tree is refused too (`check_blacklist`, and
again in every parse `rewrite_query`/`extract_table_refs` make — see
`_reject_placeholders`): nothing is bound to the statement, so a placeholder
has no use, and DuckDB refuses a statement with one.

A `TableSource`'s `row_filters` and `column_masks` are a second kind of data
reaching the generated SQL, and unlike the URLs they are spliced into the SQL
text directly rather than bound as a parameter. That is safe only because
they are trusted, not because this module checks them: both are required to
have already passed `lakana_core.policy.expressions.validate_restricted_expression`
and `lakana_core.policy.resolver.PolicyResolver`'s context substitution before
they ever reach a `TableSource`. This module performs no independent
validation of that expression text — see `TableSource` and `_reader_select_sql`.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import NamedTuple

import sqlglot
import sqlglot.expressions as exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope, traverse_scope

from lakana_core.errors import ValidationError
from lakana_core.sql_syntax import SQL_SYNTAX_ERRORS

_BLACKLISTED_FUNCTIONS = frozenset(
    {
        "read_parquet",
        "parquet_scan",
        "read_csv",
        "read_csv_auto",
        "read_json",
        "read_json_auto",
        "read_json_objects_auto",
        "read_ndjson",
        "read_ndjson_auto",
        "read_ndjson_objects",
        "read_json_objects",
        "read_text",
        "read_blob",
        "read_duckdb",
        "iceberg_scan",
        "delta_scan",
        "sqlite_scan",
        "postgres_scan",
        "mysql_scan",
        "postgres_query",
        "mysql_query",
        "sqlite_attach",
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
        "json_execute_serialized_sql",
        "which_secret",
        "current_query",
        "current_setting",
        "duckdb_settings",
        "duckdb_secrets",
        "duckdb_extensions",
        "duckdb_databases",
        "duckdb_memory",
        "duckdb_temporary_files",
        "duckdb_external_file_cache",
        "duckdb_logs",
        "duckdb_logs_parsed",
        "duckdb_log_contexts",
        "enable_logging",
        "duckdb_prepared_statements",
        "duckdb_variables",
        "getvariable",
        "duckdb_views",
        "duckdb_tables",
        "duckdb_columns",
        "duckdb_constraints",
        "duckdb_dependencies",
        "duckdb_indexes",
        "duckdb_schemas",
        "duckdb_sequences",
        "pragma_database_size",
        "pragma_metadata_info",
        "pragma_storage_info",
        "pragma_table_info",
        "pragma_show",
        "pragma_user_agent",
        "pragma_version",
        "pragma_platform",
        "sql_auto_complete",
        "stats",
        "duckdb_profiling_settings",
        "pg_get_viewdef",
        "pg_get_constraintdef",
        "pg_get_expr",
        "get_block_size",
    }
)
"""Function calls a user's own SQL must never make directly.

Defence in depth (spec Section 6.1): the executor's own rewrite (this module)
is what legitimately calls these, on SQL it constructs itself, never on the
user's original text — see `rewrite_query`, called only after this check
passes against the *original* SQL.

Every DuckDB alias of a file reader has to be listed, not just the canonical
name: `parquet_scan` is `read_parquet`, `read_csv_auto` is `read_csv`, and
`read_ndjson`/`read_ndjson_auto`/`read_ndjson_objects`/`read_json_objects`/
`read_json_objects_auto` are all `read_json` variants — missing any one of
them is a working bypass. `read_text`, `read_blob`, `read_duckdb` and `glob`
read and enumerate files of any type at all: `read_duckdb` opens another
DuckDB database file directly (its own catalog, not this connection's).

Everything from `query` onward is a second family, added as a hardening step (the full list came
from running `SELECT
function_name FROM duckdb_functions() WHERE function_type = 'table'`
against DuckDB 1.5.5 and picking out every function that reads or evaluates
something other than the rows this module's own rewrite hands it):
`query`/`query_table`/`json_execute_serialized_sql` each evaluate a
*string argument* as SQL/a table reference — the AST-blacklist's whole
premise (block by function name) means nothing once a query can construct
and run new SQL from a string at runtime, and `json_execute_serialized_sql`
in particular takes `json_serialize_sql`'s own output, so a query naming
only `json_execute_serialized_sql` in its own text (nothing that looks like
`read_parquet` to an AST walk) can still read an arbitrary file once
executed — see `test_json_execute_serialized_sql_would_otherwise_read_another_files_rows`
for a reproduction against a raw connection. `sniff_csv`/`parquet_metadata`/
`parquet_schema`/`parquet_file_metadata`/`parquet_kv_metadata`/
`parquet_full_metadata`/`parquet_bloom_probe` all read a named file's
structure or contents directly, the same class of bypass as `read_parquet`
itself, just reading metadata instead of rows. `which_secret` reveals which
configured credential DuckDB would use for a given path — not a file read,
but a secret-scope disclosure this module treats the same way.

Everything from `current_query` onward is a third family, added later,
and it is *introspection*: functions that return the connection's own state
rather than data. `current_query` is the one that matters most — it returns
the full *rewritten* statement as an ordinary result value, row-filter
predicates and column-mask expressions spliced into the CTEs included, plus
the names of the URL variables, and since a write run persists its
result, it could store that text in a table others read. The rest were
probed against DuckDB 1.5.5 through the real rewriter and executor:
`current_setting`/`duckdb_settings`/`duckdb_secrets`/`duckdb_extensions`/
`duckdb_databases`/`duckdb_temporary_files`/`duckdb_external_file_cache`
disclose worker settings, filesystem paths, secrets configuration or cached
file URLs; `duckdb_memory` and the `pragma_*` functions disclose host and
resource state; `duckdb_logs`/`duckdb_logs_parsed`/`duckdb_log_contexts`/
`enable_logging` can record and replay executed statements; and
`duckdb_views`/`duckdb_tables`/`duckdb_columns`/`duckdb_schemas` and their
siblings dump whatever this connection's catalog holds (today only DuckDB's
own built-ins, but a view or macro created inside the connection would show
its SQL); `pg_get_viewdef`/`pg_get_constraintdef`/`pg_get_expr`/
`get_block_size` are scalar macros over those same catalog dumps, and
`duckdb_profiling_settings` exposes profiling configuration. `getvariable`
reads a DuckDB variable of the connection, and the query path sets the values
a statement reads (file lists, the field-id map, a watermark) as variables
under names anyone can predict, so it is refused like `duckdb_variables`.
None has a use
in a governed query.

`stats` is the odd one out: it is a *scalar* that reads column statistics.
DuckDB takes Parquet statistics at bind time, below the rewriter's CTE
`WHERE`, so `stats(col)` reports the min and max of every row in the file,
including rows a row filter hides, and on a string column a prefix of the
hidden value; a user `WHERE` does not narrow it. Swept against 1.5.5's
`duckdb_functions()`: no other function reads statistics or file metadata
(the Parquet metadata readers are in the second family above). Table
functions and table macros that take a table *name* as a string
(`histogram`, `duckdb_table_sample`, `summary`) cannot see the rewriter's
CTEs, and `check_blacklist` refuses every table function outside
`_ALLOWED_TABLE_FUNCTIONS` in any `FROM` position anyway, so they are not
listed. The `pg_*` and `information_schema` views are table references that
fail catalog resolution rather than reaching DuckDB. Allowed on purpose, as
static *scalar* facts: `typeof`, `pg_typeof`, `version`, `current_database`,
`current_schema(s)` and `current_query_id`; `test_all_types` is a table
function, so it is refused like any other outside the allow-list.

**This list's criterion, not a completeness claim.** Every entry above is a
file-reading, SQL-evaluating, secret-revealing, introspecting or
statistics-reading DuckDB function (table, scalar or macro) as of
DuckDB 1.5.5 — a *reference* list confirmed against that one version's
`duckdb_functions()` output, not a permanent enumeration: DuckDB adds table
functions across releases, and a future one in any of these four
categories needs adding here as it's found, the same way this residual
round found two the initial pass missed. This blacklist is deliberately
never the primary control for that reason — see this module's own
docstring and `lakana_worker.executor`'s: catalog resolution and credential
scoping are what make an unauthorized read structurally unreachable
regardless of which DuckDB function a query calls; this list only makes
the common, obvious bypasses fail with a clear message instead of a
confusing one.

`read_parquet` and `read_csv` parse to sqlglot's own dedicated expression
classes (`exp.ReadCSV`, `exp.ReadParquet`), whose `.sql_name()` returns the
real function name. The rest have no dedicated class — they parse as
`exp.Anonymous`, whose `.sql_name()` always returns the literal string
`"ANONYMOUS"` regardless of which function was called; `check_blacklist`
reads the real name from `.this` for those. Adding a name here that also
lacks a dedicated sqlglot expression class needs the same `exp.Anonymous`
handling, or it will silently bypass this check.

The `GLOB` *operator* (`WHERE path GLOB '*.csv'`) parses to `exp.Glob`,
which is a binary predicate and not an `exp.Func` at all, so listing the
`glob()` table function here does not reject it. Verified against sqlglot
30.18.0.
"""

_BLACKLISTED_COMMANDS = frozenset({"load", "create"})
"""Leading keywords of statements sqlglot parses to a generic `exp.Command`.

`LOAD <extension>` and `CREATE SECRET ...` are both unsupported syntax in
sqlglot's duckdb dialect, so neither gets a dedicated expression class: both
fall back to `exp.Command`, whose `.this` is the leading keyword. `LOAD`
pulls in an extension the connection deliberately did not load, and `CREATE
SECRET ... PROVIDER credential_chain` re-acquires the worker's own ambient
cloud credentials that `lakana_worker.executor` just cleared. Naming them
gives a precise error; the `exp.Query` requirement in `check_blacklist`
rejects them regardless.
"""

_RECURSIVE_ARG = "recursive"
"""sqlglot's argument key for the `RECURSIVE` keyword on a `WITH` clause.

Read from `sqlglot.expressions.With.arg_types`, not assumed: the key is
`"recursive"` and its value is `True` only when the keyword was actually
written, `None` otherwise. Verified against sqlglot 30.18.0.
"""

_RECURSIVE_CTE_MESSAGE = "Recursive CTEs (WITH RECURSIVE) are not supported in submitted queries."
"""Why a `WITH RECURSIVE` query is refused, stated once for all three callers."""

_PLACEHOLDER_MESSAGE = "Query parameters/placeholders are not allowed in a submitted query."
"""Why a query carrying a placeholder is refused.

Deliberately fixed text: it never echoes the placeholder, the query, or any
resolved file URL back to the caller.
"""

_NOT_A_READ: tuple[type[exp.Expr], ...] = (
    exp.DML,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Attach,
    exp.Detach,
    exp.Install,
    exp.Use,
    exp.Set,
    exp.Pragma,
    exp.Command,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Comment,
    exp.Describe,
    exp.Show,
    exp.Summarize,
    exp.Analyze,
    exp.Export,
    exp.Kill,
    exp.Cache,
    exp.Uncache,
    exp.Refresh,
    exp.LoadData,
    exp.Into,
)
"""sqlglot's classes of statements that are not a read, refused at any depth of a query.

`exp.DML` covers `INSERT`, `UPDATE`, `DELETE`, `MERGE` and `COPY`. `exp.Into` is `SELECT ... INTO`,
which the rewrite would render as `CREATE TABLE ... AS`. `exp.Command` is what
sqlglot falls back to for a statement it has no class for (`VACUUM`, `RESET`). Checked
against sqlglot 30.22: a `SELECT`, a set operation, `VALUES`, `PIVOT`, a DuckDB `FROM`-first
query and a parenthesised join contain none of these classes.
"""

_READ_BODIES: tuple[type[exp.Expr], ...] = (exp.Query, exp.Pivot)
"""What the body of a CTE may be. sqlglot reads `WITH m AS (CHECKPOINT)` as a CTE whose body
is a column named `CHECKPOINT`, which no class above matches, so the body is checked too."""

_NOT_A_READ_MESSAGE = (
    "A query reads data and cannot change it: `{keyword}` is not allowed, also not inside a "
    "CTE or subquery."
)
"""Why a query holding a statement that is not a read is refused."""

_KEYWORD_CHARS = 40
"""The most characters of a refused statement's leading word the message repeats."""

_READERS = {"parquet": "read_parquet", "csv": "read_csv", "json": "read_json_auto"}
"""Maps a raw-zone table's stored file format to the DuckDB reader function."""


class ReadField(NamedTuple):
    """One column of an Iceberg read, as the field-id map hands it to DuckDB.

    Parameters
    ----------
    field_id
        The column's Iceberg field id.
    name
        The column's name in the schema the read uses.
    duckdb_type
        Its DuckDB type, as DuckDB's type parser reads it (`BIGINT`, `STRUCT("a" INTEGER)`).
    """

    field_id: int
    name: str
    duckdb_type: str


def _schema_variable(index: int) -> str:
    """Name the variable one read's field-id map is set under.

    Parameters
    ----------
    index
        The read's number, as for `_files_variable`.

    Returns
    -------
    str
        ``"v_<index>_schema"``.
    """
    return f"v_{index}_schema"


def _files_variable(index: int) -> str:
    """Name the variable one read's file list is set under.

    The name is predictable on purpose: no user SQL can reach a variable (`getvariable` and
    `duckdb_variables` are blacklisted, `SET` and `RESET` are refused), so a random part would
    protect nothing.

    Parameters
    ----------
    index
        The read's position among the reads of this rewrite that have files.

    Returns
    -------
    str
        ``"v_<index>_files"``.
    """
    return f"v_{index}_files"


_WITH_ARG = "with_"
"""sqlglot's argument key for a query's `WITH` clause.

Read rather than assumed: `sqlglot.expressions.Query.ctes` reads this exact
key, so a rename in sqlglot breaks `ctes` too and this module's own tests
fail loudly rather than silently dropping the caller's CTEs.
"""


@dataclass(frozen=True)
class TableSource:
    """Where one referenced table's rows come from.

    Parameters
    ----------
    urls
        The already-resolved, already-authorized file URLs to read. Treated
        as data throughout: set as a DuckDB variable the user's own
        SQL cannot address, never rendered into SQL text.
    file_format
        The stored format, one of `"parquet"`, `"csv"`, or `"json"`.
    columns
        The table's declared columns, mapped to the type names DuckDB should
        give them. Used when `urls` is empty, to stand in for the files with
        a typed relation of zero rows — an Iceberg table with no current
        snapshot has no data files at all, and `read_parquet([])` is an
        error rather than an empty result. Also used, whenever `row_filters`
        or `column_masks` is non-empty, to enumerate an explicit column list
        for the generated CTE instead of a blind `SELECT *` — see
        `_reader_select_sql`.
    row_filters
        Row-filter predicates already resolved and context-substituted for
        the requesting principal (`lakana_core.policy.PolicyResolver`) — ANDed
        together into the generated CTE's own `WHERE` clause. Empty means no
        filter applies. Spliced verbatim into the generated SQL text, never
        bound as a parameter: this is safe only because each expression is
        required to have already passed
        `lakana_core.policy.expressions.validate_restricted_expression` and
        `PolicyResolver`'s substitution before construction. These are
        trusted, pre-validated SQL fragments, not user input — this module
        performs no independent validation of them.
    column_masks
        Column name to its already-resolved, already-substituted mask
        expression. A column with no entry here is selected unmasked. Empty
        means no mask applies. Cast to the column's declared type and rendered into the
        generated SQL text, never bound as a parameter, for the same reason and under the same
        pre-validation requirement as `row_filters`.
    declared_columns_only
        Read the files through the explicit list of `columns` also when no policy applies,
        never with ``SELECT *``. Set for a read at an older snapshot of a table, whose files
        can hold a column that `columns` leaves out on purpose; `columns` must then not be
        empty.
    field_ids
        The columns of an Iceberg read, by field id, in the order the read returns them.
        When given, the files are read through DuckDB's field-id map (`read_parquet(...,
        schema = ...)`), so a renamed, re-added or widened column reads its own data and a
        column a file lacks reads as NULL. Empty for a raw-zone table, whose files are read by
        name. Must hold every key of `columns`; set as a variable, never rendered into SQL.
    """

    urls: Sequence[str]
    file_format: str
    columns: Mapping[str, str] = field(default_factory=dict)
    row_filters: Sequence[str] = field(default_factory=tuple)
    column_masks: Mapping[str, str] = field(default_factory=dict)
    declared_columns_only: bool = False
    field_ids: Sequence[ReadField] = field(default_factory=tuple)


@dataclass(frozen=True)
class RewrittenQuery:
    """A rewritten query and the variables to set before executing it.

    Parameters
    ----------
    sql
        The rewritten SQL. Every resolved file list is read through
        `getvariable('v_<n>_files')`, never as literal text, and no placeholder appears in
        it: the user's own SQL may contain none and nothing is bound to the statement.
    parameters
        Variable name to the value the query process sets it to before it runs the
        statement (`SET VARIABLE "<name>" = ?`, the value bound): one URL list, as a DuckDB
        LIST, per CTE that reads files, and, for an Iceberg read, its field-id map
        (`v_<n>_schema`), numbered in the order the CTEs were generated.
    """

    sql: str
    parameters: dict[str, list[str] | tuple[ReadField, ...]]


MAX_VIEW_DEPTH = 10
"""How deeply views may nest."""

MAX_EXPANDED_VIEWS = 100
"""Most view references one query may expand in total.

`max_depth` alone bounds how deep a chain may nest, not how many times a
query, or a view in it, may reference the *same* view: a view referencing
the next one `k` times, chained `d` levels deep, expands `k**d` references
without this — cheap to construct (one `CREATE OR REPLACE VIEW` per level)
and expensive for every later reader, since expansion runs again on every
query that touches the outermost view. This cap is checked against
`expanded` — the same list `expand_views` returns as `ExpandedQuery.views`
— before each reference is expanded, so a query already over the limit
fails before doing further work rather than after building a queryable but
enormous tree.
"""


@dataclass(frozen=True)
class ViewDefinition:
    """A view, as `expand_views` needs it.

    Parameters
    ----------
    name
        The view's dotted name.
    sql
        Its stored SELECT.
    references
        The dotted names it was created against, for error messages.
    """

    name: str
    sql: str
    references: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExpandedQuery:
    """A query with every view reference replaced by its definition.

    Parameters
    ----------
    sql
        The expanded SQL, or the original text when no view is referenced.
    views
        The dotted names of the views expanded, innermost first, once per
        reference.
    """

    sql: str
    views: tuple[str, ...]


_ALLOWED_TABLE_FUNCTIONS = frozenset({"range", "generate_series", "unnest"})
"""Table functions a query may call: pure generators that read no external data.

Deliberately small and explicit. Every other table function — a file or URL
reader, a foreign-database scanner, an introspection function, or one this
list has never heard of — is refused by `check_blacklist` in any `FROM`-item
position (`FROM fn()`, `LATERAL fn()`, `TABLE(fn())`), so a function DuckDB
adds later is refused by default rather than allowed. sqlglot parses `range`
as `exp.GenerateSeries` and a `FROM unnest(...)` as `exp.Unnest`; the latter
is never an `exp.Table`, so its entry here is a belt-and-braces spelling
guard rather than something the current parser reaches.
"""


def _from_item_function_name(node: exp.Table | exp.Lateral) -> str | None:
    """Name the function a `FROM` item calls, if it calls one.

    Parameters
    ----------
    node
        An `exp.Table` or `exp.Lateral` node.

    Returns
    -------
    str or None
        The lowercase function name when the item is a table function call
        (`FROM range(10)`, `LATERAL fn(...)`, `TABLE(fn(...))` — the inner
        function is named, not `table`; a schema-qualified `LATERAL main.fn()`
        is unwrapped to `fn`); `"rows from"` for the nameless node
        `ROWS FROM (...)` produces; `"lateral"` for any `LATERAL` shape that is
        neither a subquery nor a function call (fail closed); `None` for a
        named table or a lateral subquery.
    """
    inner = node.this
    if isinstance(node, exp.Lateral):
        if isinstance(inner, exp.Subquery):
            return None
        while isinstance(inner, exp.Dot):
            inner = inner.expression
        if not isinstance(inner, exp.Func):
            return "lateral"
    elif inner is None:
        return "rows from"
    if not isinstance(inner, exp.Func):
        return None
    while isinstance(inner, exp.Anonymous) and inner.name.lower() == "table":
        wrapped = inner.expressions[0] if inner.expressions else None
        if not isinstance(wrapped, exp.Func):
            break
        inner = wrapped
    return inner.name.lower() if isinstance(inner, exp.Anonymous) else inner.sql_name().lower()


def _dotted_table_name(table: exp.Table) -> str:
    """Build a dotted table name from a sqlglot Table node's catalog/db/name parts.

    Parameters
    ----------
    table
        A parsed `exp.Table` node.

    Returns
    -------
    str
        The dotted name (e.g. "gold.sales.monthly"), joining whichever of
        `catalog`, `db`, and the table's own name are present.
    """
    name_parts = (table.args.get("catalog"), table.args.get("db"), table.this)
    return ".".join(part.name for part in name_parts if part)


def _reject_recursive_ctes(parsed: exp.Expr) -> None:
    """Refuse a statement containing a `WITH RECURSIVE` clause anywhere in it.

    This closes a whole feature surface rather than one more spelling of a
    bypass. sqlglot's `optimizer/scope.py::_traverse_ctes` pre-seeds a
    recursive CTE's own name into `Scope.cte_sources` **before** traversing
    its body, and where that body is a set operation — `base_term UNION ALL
    recursive_term`, the standard shape — the pre-seeded binding is
    inherited into *both* branches. DuckDB does not bind it that way: only
    the recursive term legitimately self-references the CTE being built,
    while in the base term the same name still resolves to a real relation
    through DuckDB's ordinary replacement-scan resolution — a local file, an
    HTTP(S) URL, or a catalog table. Every scope-based classifier in this
    module therefore reads a base-term reference as "already bound, not a
    table", drops it, and authorizes nothing, while DuckDB goes and reads the
    real thing. Thirteen working spellings of that were found, reaching
    arbitrary local file reads in single-node mode and worker-side SSRF in
    team and cloud mode (fetching an `https://` URL needs no credential, and
    `disabled_filesystems` does not cover `httpfs`).

    Modelling DuckDB's recursive binding rules exactly, per branch, is the
    second special case recursive CTEs have demanded here — and getting it
    subtly wrong a third time reopens an unauthenticated read. `WITH
    RECURSIVE` is an advanced, rare feature with no part in the ad-hoc
    analyst queries M2 serves, so it is refused outright instead: a query
    either contains the keyword or it does not, which is a question with no
    edge cases.

    `find_all` rather than reading the statement's own `WITH` clause: a
    recursive CTE nested inside a subquery, a derived table, or an `IN`
    predicate is the same bug, and the outermost clause is not the only
    place one can appear.

    Parameters
    ----------
    parsed
        The parsed statement to check. Not modified.

    Raises
    ------
    ValidationError
        If any `WITH` clause in `parsed` carries the `RECURSIVE` keyword.
    """
    for with_clause in parsed.find_all(exp.With):
        if with_clause.args.get(_RECURSIVE_ARG):
            raise ValidationError(_RECURSIVE_CTE_MESSAGE)


def _reject_placeholders(parsed: exp.Expr) -> None:
    """Refuse a statement carrying a query parameter or placeholder anywhere.

    The rewriter binds every resolved file list as a query parameter, and on
    S3 those lists are presigned URLs of the raw files. A `?`, `$1`, or
    `$name` in the user's own SQL would bind to one and return it as an
    ordinary value — a URL anyone can then fetch directly, bypassing every
    row filter and column mask. No submitted query has a legitimate use for
    a placeholder: the platform binds no user-supplied values.

    `find` rather than a top-level check: a placeholder nested in a
    subquery, a CTE, a `WHERE`, or a function argument binds all the same.
    sqlglot's duckdb dialect parses `?`, `$<n>`, and `$<identifier>` to
    `exp.Placeholder`; `exp.Parameter` is what other spellings (`@x`,
    `${x}`) parse to in other dialects, refused too so a dialect change
    cannot quietly reopen this. What DuckDB executes is sqlglot's rendering
    of this tree, never the raw text, and only these two node types render
    as a placeholder.

    Parameters
    ----------
    parsed
        The parsed statement to check. Not modified.

    Raises
    ------
    ValidationError
        If `parsed` contains any `exp.Placeholder` or `exp.Parameter` node.
        The message is fixed text and never echoes the query or a URL.
    """
    if parsed.find(exp.Placeholder, exp.Parameter) is not None:
        raise ValidationError(_PLACEHOLDER_MESSAGE)


def _leading_keyword(node: exp.Expr, dialect: str) -> str:
    """Name a refused statement by its first word, as sqlglot writes it.

    Parameters
    ----------
    node
        The refused node.
    dialect
        The SQL dialect to render it in.

    Returns
    -------
    str
        The keyword in capitals, at most `_KEYWORD_CHARS` characters: for DML and `INTO` the
        statement's own keyword (`DELETE`, `INSERT`, `INTO`), for an `exp.Command` its own
        keyword (`VACUUM`), otherwise the first word (`BEGIN`).
    """
    if isinstance(node, (exp.DML, exp.Into)):
        # Its rendering can start with the statement's own `WITH` clause or a literal.
        return node.key.upper()[:_KEYWORD_CHARS]
    text = str(node.this) if isinstance(node, exp.Command) else node.sql(dialect=dialect)
    words = text.split(None, 1)
    return (words[0] if words else node.key).upper()[:_KEYWORD_CHARS]


def _reject_statements_inside(parsed: exp.Expr, dialect: str) -> None:
    """Refuse a statement that is not a read, wherever in the tree it sits.

    DuckDB 2 runs DML inside a CTE (`WITH m AS (DELETE ... RETURNING *) SELECT ...`) and its
    `extract_statements` calls the whole text a `SELECT`; sqlglot parses it as an `exp.Select`
    holding an `exp.Delete`. Requiring the statement as a whole to be a query is therefore not
    enough: every node is looked at, and so is the body of every CTE.

    Parameters
    ----------
    parsed
        The parsed statement. Not modified.
    dialect
        The SQL dialect it was parsed as, for naming the refused keyword.

    Raises
    ------
    ValidationError
        If any node is one of `_NOT_A_READ`, or a CTE's body is not one of `_READ_BODIES`. The
        message names the statement's first word and nothing else of the query.
    """
    for node in parsed.walk():
        if isinstance(node, _NOT_A_READ):
            raise ValidationError(
                _NOT_A_READ_MESSAGE.format(keyword=_leading_keyword(node, dialect))
            )
        if isinstance(node, exp.CTE) and not isinstance(node.this, _READ_BODIES):
            raise ValidationError(
                _NOT_A_READ_MESSAGE.format(keyword=_leading_keyword(node.this, dialect))
            )


def _require_unambiguous_references(parsed: exp.Expr, dialect: str) -> None:
    """Refuse a query whose own references sqlglot cannot resolve unambiguously.

    Four separate authorization bypasses were found in the hand-rolled
    classification this gate now stands in front of, each one a different way
    for two unrelated things in a query to end up sharing a name: an inner
    `WITH` binding and an outer real table; a bare binding and a qualified
    `db.x`; a derived table's alias and a real read beside it; a `WITH` name
    and an *alias* an attacker hung on a genuine external read. The common
    shape is always the same — a name that means two things at once — so the
    check made here is for that shape itself, rather than for one more of its
    spellings.

    `sqlglot.optimizer.qualify.qualify` is sqlglot's own, separately tested
    pass for exactly this: it resolves every reference in the statement and
    raises where it cannot. It runs against a **copy**, purely as a gate: the
    tree the caller goes on to classify and rewrite is the one they parsed,
    so the SQL the executor finally runs is still the user's own query with
    its table references swapped, not an optimizer's re-rendering of it
    (which renames a `COUNT(*)` result column to `_col_0`, among other
    surprises).

    `validate_qualify_columns` is off deliberately. With it on, `qualify`
    also demands that every *column* resolve to a known source, which no
    caller here can satisfy: the platform passes no schema, so an ordinary
    `SELECT id FROM bronze.a JOIN gold.b ON ...` — legal SQL DuckDB runs
    fine — would be refused. Table and alias resolution, which is what this
    gate is for, is unaffected by the flag.

    Reading `Scope.selected_sources` is what raises on two sources of one
    scope sharing a name, so every scope's is read here explicitly rather
    than trusting that `qualify` internally visited them all.

    Parameters
    ----------
    parsed
        The parsed statement to check. Not modified.
    dialect
        The SQL dialect `parsed` was parsed as.

    Raises
    ------
    ValidationError
        If sqlglot cannot resolve the statement's references unambiguously.
        Fail closed: an ambiguity sqlglot itself declines to resolve is not
        one this module may resolve on its behalf. DuckDB refuses these
        queries too — `WITH t AS (...) SELECT * FROM a.t JOIN t` is
        `Ambiguous reference to table "t"` there — so nothing a user can
        legitimately run is lost.
    """
    try:
        qualified = qualify(parsed.copy(), dialect=dialect, validate_qualify_columns=False)
        root = build_scope(qualified)
        for scope in root.traverse() if root is not None else ():
            _ = scope.selected_sources
    except Exception as exc:
        raise ValidationError(
            f"This query's table references cannot be resolved unambiguously: {exc}"
        ) from exc


def unreadable_view_message(dotted: str) -> str:
    """Say a stored view no longer parses, without any of its SQL.

    Parameters
    ----------
    dotted
        The view's dotted name, which the reader's own query used.

    Returns
    -------
    str
        A message naming the view and the next step, and none of its text, which the
        reader may not be allowed to see.
    """
    return f"View {dotted!r} could not be read. Ask its owner to recreate it."


def _parse_checked(sql: str, dialect: str) -> exp.Expr:
    """Parse a query and refuse it unless its references resolve unambiguously.

    Parameters
    ----------
    sql
        The query to parse.
    dialect
        The SQL dialect to parse `sql` as.

    Returns
    -------
    sqlglot.expressions.Expr
        The parsed statement, safe to classify with `_cte_reference_ids`.

    Raises
    ------
    ValidationError
        If the statement contains a query parameter or placeholder, which
        would bind to the rewriter's own URL lists — see
        `_reject_placeholders`. Or if it contains a `WITH RECURSIVE` clause, whose scope
        bindings `_cte_reference_ids` cannot classify soundly — see
        `_reject_recursive_ctes`. Or if it holds a statement that is not a read
        anywhere, see `_reject_statements_inside`. `check_blacklist` already refuses these
        first in `lakana_worker.pipeline.run_one`, so this is defence in
        depth: the promise `extract_table_refs` makes its caller is that
        every name it leaves out is genuinely not a table, and it cannot
        keep that promise here, so it must not be reachable by a future
        caller that skips the blacklist.
    """
    parsed = sqlglot.parse_one(sql, dialect=dialect)
    _reject_placeholders(parsed)
    _reject_statements_inside(parsed, dialect)
    _reject_recursive_ctes(parsed)
    _require_unambiguous_references(parsed, dialect)
    return parsed


def _is_cte_reference(scope: Scope, table: exp.Table) -> bool:
    """Decide whether one table node names a `WITH` binding rather than a real table.

    A `WITH` name is not a catalog table: it has no namespace, it is not
    authorized against, and it must not be rewritten. Which names are bound
    is a property of a *scope*, not of the whole query — binding `x` in an
    inner scope says nothing about a real `x` read in an outer one — so the
    question is asked of `Scope.cte_sources`, sqlglot's own record of the
    `WITH` names visible at this point, inherited into nested scopes and
    holding only bindings a `WITH` clause actually made.

    The name asked about is `table.name`, the reference's own name, never
    `alias_or_name`. An alias is attacker-chosen text that names the
    *result*, not the thing being read, and DuckDB resolves a reference by
    its name: aliasing a genuine external read to a `WITH` name does not make
    DuckDB read the binding, so it must not make this function say so either
    — that was the fourth bypass in this class.

    A table node carrying a `db` or `catalog` part is a real reference
    whatever the scope binds: DuckDB binds a `WITH` name as a single
    identifier and never resolves a qualified name to one.

    Parameters
    ----------
    scope
        The scope the reference appears in, from `build_scope`.
    table
        The table node to classify.

    Returns
    -------
    bool
        `True` if this reference resolves to a `WITH` binding visible in
        `scope`, and so is not a catalog table at all.
    """
    if table.args.get("db") is not None or table.args.get("catalog") is not None:
        return False
    return table.name in scope.cte_sources


def _cte_reference_ids(parsed: exp.Expr) -> set[int]:
    """Identify every table node that is really a reference to a `WITH` binding.

    Identity (`id`), not equality: two textually identical references are
    equal and hash equal as sqlglot expressions, but one may be a CTE
    reference and the other a real table read in a different scope — which is
    the entire distinction being drawn here.

    Anything scope resolution does not positively classify as a binding stays
    a real table reference. A statement sqlglot builds no scope for at all (a
    `DELETE`, an `UPDATE`) therefore keeps every reference: failing open here
    would mean skipping authorization, which is the failure this function
    exists to prevent. `parsed` must already have passed
    `_require_unambiguous_references`.

    Parameters
    ----------
    parsed
        The parsed statement.

    Returns
    -------
    set of int
        The `id` of every `exp.Table` node that names a `WITH` binding
        visible in the scope it appears in.
    """
    root = build_scope(parsed)
    if root is None:
        return set()
    return {
        id(table)
        for scope in root.traverse()
        for table in scope.tables
        if _is_cte_reference(scope, table)
    }


def _empty_relation(dotted: str, columns: Mapping[str, str]) -> exp.Select:
    """Build a zero-row relation shaped like a table's declared schema.

    Parameters
    ----------
    dotted
        The dotted table name, for the error message only.
    columns
        Column name to DuckDB type name. Both are rendered through sqlglot
        — the name as a quoted identifier, the type through sqlglot's own
        type parser — so neither can contribute SQL syntax of its own.

    Returns
    -------
    sqlglot.expressions.Select
        `SELECT CAST(NULL AS <type>) AS <name>, ... WHERE FALSE`.

    Raises
    ------
    ValidationError
        If the table declares no columns, leaving nothing to shape the
        empty relation from.
    """
    if not columns:
        raise ValidationError(
            f"Table {dotted!r} has no files and no declared columns to stand in for them."
        )
    projections = [
        exp.alias_(exp.cast(exp.null(), type_name, dialect="duckdb"), name, quoted=True)
        for name, type_name in columns.items()
    ]
    return exp.select(*projections).where(exp.false())


def _quoted_identifier(name: str, dialect: str) -> str:
    """Render a bare identifier name as a safely quoted, escaped SQL identifier.

    Parameters
    ----------
    name
        The identifier to quote — a column name from the catalog's declared
        schema, never trusted as pre-escaped text: an external table's
        schema is inferred from real file content, no more trusted than a
        storage object key (see this module's own docstring).
    dialect
        The SQL dialect to render the identifier for.

    Returns
    -------
    str
        The identifier, quoted and escaped through sqlglot's own renderer —
        never built by hand-splicing quote characters.
    """
    return exp.to_identifier(name, quoted=True).sql(dialect=dialect)


def _reader_select_sql(source: TableSource, index: int, dialect: str) -> str:
    """Build the SELECT reading a table's files, applying its row filters/column masks.

    Parameters
    ----------
    source
        The table's resolved files and any applicable row filters/column
        masks — already composed and context-substituted for the
        requesting principal by `lakana_core.policy.PolicyResolver.resolve`,
        never raw, unvalidated policy text.
    index
        The read's number, from which its variable names are made (`_files_variable`).
    dialect
        The SQL dialect to render the generated SQL as.

    Returns
    -------
    str
        `SELECT * FROM <reader>(getvariable('v_<n>_files'))` when no policy applies and the
        source does not ask for its declared columns only. Otherwise an explicit column list
        built from `source.columns`, with a masked column's projection replaced by its
        mask expression instead of the raw value, plus a `WHERE` clause
        ANDing every applicable row filter. A masked column's real value is
        therefore never selected in the first place, not merely hidden
        after the fact — and because this is the *only* place any query
        ever reads the table's raw files (every reference above it, in the
        user's own query, resolves against this CTE's output, however many
        joins or nested CTEs deep), applying policy here is enough on its
        own, with no separate `SELECT *`-expansion pass over the user's own
        query needed: masking survives nesting, as `test_query_rewrite.py` checks.

    Raises
    ------
    ValidationError
        If `source.row_filters` or `source.column_masks` is non-empty, or
        `source.declared_columns_only` is set, but `source.columns` is empty:
        there is no declared column list to build
        an explicit projection from, and silently falling through to an
        empty `SELECT` list would render `SELECT  FROM <reader>(...)` —
        syntactically invalid SQL that DuckDB would reject with an opaque
        parser error at execution time, far from the real cause. Or if
        `source.column_masks` names a column that is not a key of
        `source.columns`: silently ignoring the mismatched key would select
        that column's raw, unmasked value — breaking the guarantee that a
        masked column's real value never reaches the caller, via nothing
        more than an operator's typo or a case mismatch, with no
        diagnostic. Column names are matched exactly, case included:
        Lakana's identifiers are always rendered quoted, so normalizing case
        here would be inconsistent with quoted-identifier semantics
        everywhere else in this codebase. Or if `source.field_ids` is given for files that
        are not Parquet, or lacks a key of `source.columns`: such a column would not be
        read at all. Or if its map names a column outside `source.columns`, which a
        ``SELECT *`` would show, or repeats a field id or a name.
    """
    arguments = f"getvariable('{_files_variable(index)}')"
    if source.field_ids:
        known = [read.name for read in source.field_ids]
        missing = [name for name in source.columns if name not in known]
        if missing:
            raise ValidationError(
                f"The read by field id has no field id for its column {missing[0]!r}."
            )
        extra = [name for name in known if source.columns and name not in source.columns]
        if extra:
            raise ValidationError(
                f"The read by field id names the column {extra[0]!r}, which the table does "
                "not declare."
            )
        for label, values in (
            ("field id", [read.field_id for read in source.field_ids]),
            ("column", known),
        ):
            repeated = [value for value, count in Counter(values).items() if count > 1]
            if repeated:
                raise ValidationError(
                    f"The read by field id has the {label} {repeated[0]!r} twice."
                )
        if source.file_format != "parquet":
            raise ValidationError("A read by field id needs Parquet files.")
        arguments += f", schema = getvariable('{_schema_variable(index)}')"
    reader = f"{_READERS[source.file_format]}({arguments})"
    if not source.row_filters and not source.column_masks and not source.declared_columns_only:
        # B608: the interpolated values are a literal reader name from the
        # closed `_READERS` dict and a generated variable name; the URLs are
        # the variable's value, never text.
        return f"SELECT * FROM {reader}"  # nosec B608
    if not source.columns:
        raise ValidationError(
            "A row filter or column mask applies to this table, but it has no "
            "declared column list to build an explicit projection from."
        )
    unknown_masks = [name for name in source.column_masks if name not in source.columns]
    if unknown_masks:
        raise ValidationError(
            f"Column mask references unknown column {unknown_masks[0]!r}; "
            f"known columns: {sorted(source.columns)}"
        )
    read_types = {read.name: read.duckdb_type for read in source.field_ids}
    projections = [
        _masked_projection(
            source.column_masks[name], name, read_types.get(name, source.columns[name]), dialect
        )
        if name in source.column_masks
        else _quoted_identifier(name, dialect)
        for name in source.columns
    ]
    # B608: `projections` entries are either a safely quoted identifier or an
    # already-validated-and-substituted policy expression (never raw,
    # unvalidated user input — see
    # `lakana_core.policy.expressions.validate_restricted_expression` and
    # `lakana_core.policy.resolver`); `reader` is the same closed literal as
    # above. The URLs are still the variable's value, never text.
    sql = f"SELECT {', '.join(projections)} FROM {reader}"  # nosec B608
    if source.row_filters:
        sql += " WHERE " + " AND ".join(f"({expression})" for expression in source.row_filters)
    return sql


def _masked_projection(mask: str, name: str, type_name: str, dialect: str) -> str:
    """Render a column mask as a projection that keeps the column's declared type.

    The value is cast to the column's DuckDB type, taken from the table's recorded schema and
    never from the mask's text, so a reader gets the type the column has without the mask. The
    cast is built with sqlglot, so neither the mask nor the type can add syntax of its own.
    Creating a mask checks the value fits that type, so the cast cannot fail.

    Parameters
    ----------
    mask
        The resolved, context-substituted mask expression.
    name
        The masked column's name.
    type_name
        The column's DuckDB type name: the field-id read's type for an Iceberg table, otherwise
        the one in `TableSource.columns`.
    dialect
        The SQL dialect to parse and render in.

    Returns
    -------
    str
        `CAST(<mask> AS <type>) AS "<name>"`.
    """
    value = exp.cast(sqlglot.parse_one(mask, dialect=dialect), type_name, dialect=dialect)
    return f"{value.sql(dialect=dialect)} AS {_quoted_identifier(name, dialect)}"


def named_columns(sql: str, dialect: str = "duckdb") -> frozenset[str]:
    """List the columns a statement names, lower-cased.

    Parameters
    ----------
    sql
        The statement, with its views expanded.
    dialect
        The SQL dialect to parse `sql` as.

    Returns
    -------
    frozenset of str
        The name of every column reference in it (the last part of a dotted one); empty when
        the statement cannot be parsed, which `rewrite_query` refuses before this is asked.
    """
    try:
        parsed = sqlglot.parse_one(sql, dialect=dialect)
    except SQL_SYNTAX_ERRORS:
        return frozenset()
    return frozenset(column.name.lower() for column in parsed.find_all(exp.Column))


def extract_table_refs(sql: str, dialect: str = "duckdb") -> list[str]:
    """List every distinct qualified table name a query references.

    Parameters
    ----------
    sql
        The query to analyse.
    dialect
        The SQL dialect to parse `sql` as.

    Returns
    -------
    list of str
        Dotted table names (`"bronze.weather"`, `"gold.sales.monthly"`), each
        listed once, in the order first encountered. A table function
        (`FROM range(10)`) is not a table and is left out. A reference the query
        binds itself in a `WITH` clause visible at that point is not a table
        reference and is left out — resolved per scope, so a binding in one
        scope never hides a real table of the same name read in another.

    Raises
    ------
    ValidationError
        If sqlglot cannot resolve the query's references unambiguously; see
        `_require_unambiguous_references`. This list is what the caller
        authorizes against, so a reference left out of it is a reference
        never checked: where the meaning of a name is in doubt, the query is
        refused rather than classified. Also if the query contains a `WITH
        RECURSIVE` clause, whose base-term bindings no scope-based classifier
        here can read the way DuckDB does — see `_reject_recursive_ctes`.
    """
    parsed = _parse_checked(sql, dialect)
    bound = _cte_reference_ids(parsed)
    seen: dict[str, None] = {}
    for table in parsed.find_all(exp.Table):
        if id(table) not in bound and _from_item_function_name(table) is None:
            seen[_dotted_table_name(table)] = None
    return list(seen)


def expand_views(
    sql: str,
    lookup: Callable[[str], ViewDefinition | None],
    *,
    root: str | None = None,
    max_depth: int = MAX_VIEW_DEPTH,
    dialect: str = "duckdb",
) -> ExpandedQuery:
    """Replace every view reference with a subquery of the view's stored SQL.

    This must run before `extract_table_refs`/`rewrite_query` see the query:
    once views are expanded, every remaining reference is a base table, so
    grants, row filters and column masks apply to the base tables exactly
    as for a direct query, and a view can never widen access (invoker's
    rights; the "future view feature" `rewrite_query`'s
    docstring anticipates). Expansion is recursive, bounded by `max_depth`,
    and guarded against cycles; a stored view's SQL is re-checked against
    the AST guard as defence in depth.

    Parameters
    ----------
    sql
        The query.
    lookup
        Maps a dotted name to its view, or `None` for anything that is not
        a view. It is where the caller authorizes READ on each view.
        Memoized for the duration of this call: the
        same dotted name reached from two different places in the query —
        two occurrences of the same view, or the same view reached through
        two different nesting paths — is looked up (a database read plus an
        authorization check) at most once, not once per occurrence.
    root
        The view being created, if any, so a replacement can't reference
        itself through another view.
    max_depth
        How deeply views may nest.
    dialect
        The SQL dialect.

    Returns
    -------
    ExpandedQuery
        The expanded query and the views it expanded.

    Raises
    ------
    ValidationError
        If views form a cycle, nest deeper than `max_depth`; if more than
        `MAX_EXPANDED_VIEWS` references are expanded in total; if a view
        reference carries a construct expansion can't safely reproduce; or
        if a view's stored SQL fails the AST guard or doesn't parse
        unambiguously.
    """
    parsed = _parse_checked(sql, dialect)
    expanded: list[str] = []
    cache: dict[str, ViewDefinition | None] = {}

    def cached_lookup(dotted: str) -> ViewDefinition | None:
        """Call `lookup` at most once per distinct dotted name, for this call only.

        Parameters
        ----------
        dotted
            The dotted name to look up.

        Returns
        -------
        ViewDefinition or None
            `lookup`'s own result, cached. A raised exception (a denied
            view) is never cached: it aborts this call outright before a
            result exists to store.
        """
        if dotted not in cache:
            cache[dotted] = lookup(dotted)
        return cache[dotted]

    named_views: list[tuple[tuple[str, str, str], exp.Expr]] = []
    _expand_into(
        parsed,
        cached_lookup,
        () if root is None else (root,),
        expanded,
        named_views,
        max_depth,
        dialect,
    )
    if not expanded:
        return ExpandedQuery(sql=sql, views=())
    counts = _bare_name_counts(parsed)
    _drop_schema_from_qualified_columns(
        list(traverse_scope(parsed)),
        [(key, node) for key, node in named_views if counts[key[2]] == 1],
    )
    return ExpandedQuery(sql=parsed.sql(dialect=dialect), views=tuple(expanded))


_SAFE_TABLE_ARGS = frozenset({"this", "db", "catalog", "alias"})
"""`exp.Table` args that name and alias a reference, nothing more.

A view reference carrying only these needs no special handling: the
replacing `exp.Subquery` gets the same alias, and `this`/`db`/`catalog` are
what `_dotted_table_name` already read to identify the view.
"""

_TRANSFERABLE_TABLE_ARGS = frozenset({"joins", "pivots", "sample"})
"""`exp.Table` args `_expand_into` moves onto the replacing `exp.Subquery`.

`exp.Subquery.arg_types` accepts each of these directly, and moving them is
a structural transplant, not a rewrite: a `JOIN` inside a parenthesized view
reference (`FROM (gold.v JOIN silver.x ON …)` parses `joins` onto `v`'s own
`Table` node, not the enclosing `Select`'s — confirmed against the pinned
sqlglot version), a `PIVOT` and a `TABLESAMPLE` attach to whichever relation
they follow and attach the same way whether that relation is a table or,
after expansion, a subquery. Verified end to end against DuckDB for all
three (a parenthesized join, `PIVOT`, and `TABLESAMPLE`): each executes
correctly once expanded this way.

Before this fix, `_expand_into` kept only `alias` when replacing a `Table`
node, so any of these constructs on a view reference vanished silently and
the query returned wrong results with no error. Anything else `exp.Table.arg_types` allows
(`laterals`, `hints`,
`system_time` versioning, `pattern`, `ordinality`, `changes`, `partition`,
`format`, `rows_from`, `when`, `only`, `indexed`) has no established-safe
transplant onto a `Subquery`, so `_expand_into` refuses those with a
`ValidationError` instead of guessing — silent wrong results are never
acceptable here. `laterals` (a parenthesized `LATERAL VIEW`) was once carried
over, but it rendered SQL DuckDB refused with a raw parser error, so it is
refused up front like the rest.
"""


def _expand_into(
    parsed: exp.Expr,
    lookup: Callable[[str], ViewDefinition | None],
    stack: tuple[str, ...],
    expanded: list[str],
    named_views: list[tuple[tuple[str, str, str], exp.Expr]],
    max_depth: int,
    dialect: str,
) -> None:
    """Expand the view references in one parsed query, in place.

    Parameters
    ----------
    parsed
        The query, modified in place.
    lookup
        As for `expand_views`.
    stack
        The views being expanded around this query, outermost first.
    expanded
        Appended with each view expanded, innermost first.
    named_views
        Appended with the `_qualifier_key` and subquery of each view reference that had no
        alias and so took the view's name.
    max_depth
        As for `expand_views`.
    dialect
        The SQL dialect.

    Raises
    ------
    ValidationError
        As `expand_views` documents.
    """
    bound = _cte_reference_ids(parsed)
    for table in list(parsed.find_all(exp.Table)):
        if id(table) in bound:
            continue
        dotted = _dotted_table_name(table)
        view = lookup(dotted)
        if view is None:
            continue
        extra_args = set(table.args) - _SAFE_TABLE_ARGS
        unsupported = extra_args - _TRANSFERABLE_TABLE_ARGS
        if unsupported:
            raise ValidationError(
                f"{dotted!r} can't be used with {', '.join(sorted(unsupported))} yet."
            )
        if dotted in stack:
            path = " -> ".join((*stack, dotted))
            raise ValidationError(f"View {dotted!r} references itself: {path}.")
        if len(stack) >= max_depth:
            raise ValidationError(f"Views nest more than {max_depth} deep at {dotted!r}.")
        if len(expanded) >= MAX_EXPANDED_VIEWS:
            raise ValidationError(
                f"This query expands more than {MAX_EXPANDED_VIEWS} view references; "
                "simplify it or the views it reads."
            )
        try:
            check_blacklist(view.sql, dialect)
            inner = _parse_checked(view.sql, dialect)
        except SQL_SYNTAX_ERRORS as exc:
            # The stored SQL may name tables the reader cannot see: name the view only.
            raise ValidationError(unreadable_view_message(dotted)) from exc
        _expand_into(inner, lookup, (*stack, dotted), expanded, named_views, max_depth, dialect)
        alias = table.args.get("alias")
        key = _qualifier_key(table)
        if alias is None:
            alias = exp.TableAlias(this=exp.to_identifier(table.name))
        subquery = exp.Subquery(this=inner, alias=alias)
        if table.args.get("alias") is None:
            named_views.append((key, subquery))
        for arg in extra_args:
            subquery.set(arg, table.args[arg])
        table.replace(subquery)
        expanded.append(dotted)


_ALIAS_HASH_HEX_CHARS = 12


def _qualifier_key(node: exp.Table | exp.Column) -> tuple[str, str, str]:
    """Name the schema-qualified spelling of a table or of a column's table part.

    Parameters
    ----------
    node
        A table reference, or a column whose qualifier is read.

    Returns
    -------
    tuple of str
        The catalog, schema and table name, lower-cased (the database compares names
        without regard to case); an absent part is an empty string.
    """
    return (
        node.catalog.lower(),
        node.db.lower(),
        (node.name if isinstance(node, exp.Table) else node.table).lower(),
    )


def _bare_name_counts(parsed: exp.Expr) -> Counter[str]:
    """Count how often each bare name is taken by a source anywhere in a statement.

    Parameters
    ----------
    parsed
        The statement.

    Returns
    -------
    collections.Counter of str
        For each lower-cased name, the number of aliases (of tables, derived tables, CTEs and
        table functions) and unaliased table references called so, in every scope.
    """
    counts = Counter(alias.name.lower() for alias in parsed.find_all(exp.TableAlias))
    counts.update(
        table.name.lower()
        for table in parsed.find_all(exp.Table)
        if table.args.get("alias") is None
    )
    return counts


def _drop_schema_from_qualified_columns(
    scopes: Sequence[Scope], named: Sequence[tuple[tuple[str, str, str], exp.Expr]]
) -> None:
    """Cut the schema part off a column qualified with the full name of a read named by its table.

    A reference such as `gold.orders` is replaced by a read the database knows only by its
    table name, so the column `gold.orders.id` has to become `orders.id`, or the database looks
    for a schema called `gold`. Only a read whose bare name is used nowhere else in the
    statement is named that way (see `_bare_name_counts`), and only a column of the read's own
    scope is changed. In a nested scope the database would bind `orders.id` to a struct column
    called `orders` of that scope's own sources before it reached the outer read, and sqlglot
    makes a CTE body or a derived table a child of the scope around it although the read is not
    visible there. Any other dotted column is therefore left as written and fails in the
    database, as it always did, instead of binding to something else.

    Parameters
    ----------
    scopes
        The scopes of the statement, built while the reads were still in it.
    named
        The `_qualifier_key` of each read that took its bare name as alias, with the node.
    """
    owner = {id(node): scope for scope in scopes for node in (*scope.tables, *scope.derived_tables)}
    roots = {id(scope.expression): scope for scope in scopes}
    seen: set[int] = set()
    for scope in scopes:
        # A scope lists the correlated columns of its subqueries too, so each column is
        # attributed to the scope whose expression it sits in directly.
        for column in (*scope.columns, *scope.external_columns):
            if id(column) in seen or column.args.get("db") is None:
                continue
            seen.add(id(column))
            node: exp.Expr | None = column
            while node is not None and id(node) not in roots:
                node = node.parent
            own = None if node is None else roots[id(node)]
            key = _qualifier_key(column)
            for named_key, read in named:
                if named_key == key and own is owner.get(id(read)):
                    column.set("db", None)
                    column.set("catalog", None)


def _cte_alias(dotted: str) -> str:
    """Derive a table's CTE alias, distinct for every distinct dotted name.

    The sanitized prefix keeps generated SQL readable. The hash suffix is
    what makes it collision-resistant: sanitizing alone is lossy, so
    ``a.b_c`` and ``a_b.c`` would otherwise share ``__lakana_a_b_c``.

    Parameters
    ----------
    dotted
        The table's dotted name, as `_dotted_table_name` returns it.

    Returns
    -------
    str
        ``"__lakana_" + sanitized + "_" + <first 12 hex chars of sha256(dotted)>``.
    """
    digest = hashlib.sha256(dotted.encode("utf-8")).hexdigest()[:_ALIAS_HASH_HEX_CHARS]
    return "__lakana_" + re.sub(r"\W", "_", dotted) + "_" + digest


def rewrite_query(
    sql: str, sources: Mapping[str, TableSource], dialect: str = "duckdb"
) -> RewrittenQuery:
    """Replace each qualified table reference with a CTE reading its resolved files.

    Parameters
    ----------
    sql
        The original query.
    sources
        Maps each dotted table name `extract_table_refs` would return from
        `sql` to where that table's rows come from.
    dialect
        The SQL dialect to parse and render `sql` as.

    Returns
    -------
    RewrittenQuery
        `sql` with every reference in `sources` replaced by a uniquely-named
        CTE (`read_parquet(getvariable('v_<n>_files'))` or the matching reader), keeping the
        original reference's alias so the rest of the query still resolves,
        plus the URL lists to set as those variables. This is what lets a
        multi-level Lakana name (`gold.sales.monthly`) run against DuckDB at
        all — the name never reaches DuckDB's own catalog, only the
        rewritten form does. A source with no `row_filters` or
        `column_masks` gets that bare reader call, unchanged from before
        those fields existed; a source with either gets an explicit column
        list instead, built from its declared `columns`, with a masked
        column's projection replaced by its mask expression and a `WHERE`
        clause ANDing every row filter — see `_reader_select_sql`.

    Raises
    ------
    ValidationError
        If a statement that is not a query (an `UPDATE`, a `DELETE`) names a
        catalog table: there is nowhere to attach the CTEs to, and no such
        statement has a defined meaning against a read-only scoped file set.
        Or if the query contains a query parameter or placeholder (see
        `_reject_placeholders`), if sqlglot cannot resolve the query's
        references unambiguously, or if the query contains a `WITH
        RECURSIVE` clause — all checked
        here independently of `extract_table_refs`, so that this function
        never depends on that one having been called first. Or, for a source
        with an applicable row filter or column mask, if it declares no
        columns, or if a column mask names a column outside its declared
        columns — see `_reader_select_sql`. Or if two distinct tables would
        share one generated CTE alias — a defence against the silent reuse
        of an alias.
    """
    parsed = _parse_checked(sql, dialect)
    bound = _cte_reference_ids(parsed)
    ctes: dict[str, exp.Expr] = {}
    owners: dict[str, str] = {}
    parameters: dict[str, list[str] | tuple[ReadField, ...]] = {}
    reads = 0
    named: list[tuple[tuple[str, str, str], exp.Expr]] = []
    name_counts = _bare_name_counts(parsed)
    scopes = list(traverse_scope(parsed))
    for table in list(parsed.find_all(exp.Table)):
        dotted = _dotted_table_name(table)
        if id(table) in bound or dotted not in sources:
            continue
        source = sources[dotted]
        alias = _cte_alias(dotted)
        owner = owners.setdefault(alias, dotted)
        if owner != dotted:
            raise ValidationError(
                f"Internal alias {alias!r} was derived for both {owner!r} and {dotted!r}."
            )
        if alias not in ctes:
            if source.urls:
                index = reads
                reads += 1
                try:
                    ctes[alias] = sqlglot.parse_one(
                        _reader_select_sql(source, index, dialect), dialect=dialect
                    )
                except SQL_SYNTAX_ERRORS as exc:
                    # The text holds stored row filters and column masks, which the reader
                    # may not see: name none of it.
                    raise ValidationError(
                        "A stored row filter or column mask on a table this query reads could "
                        "not be read. Ask an administrator to correct or remove it."
                    ) from exc
                parameters[_files_variable(index)] = list(source.urls)
                if source.field_ids:
                    parameters[_schema_variable(index)] = tuple(source.field_ids)
            else:
                ctes[alias] = _empty_relation(dotted, source.columns)
        replacement = exp.to_table(alias)
        original_alias = table.args.get("alias")
        if original_alias is not None:
            replacement.set("alias", original_alias)
        elif name_counts[table.name.lower()] == 1:
            # The caller refers to the table by its own name, so that name is the alias
            # (`orders.id` resolves). Only when nothing else in the statement is called so:
            # otherwise the read keeps its generated name, as it always did, and a clash
            # fails in the database instead of binding to the wrong table.
            replacement.set("alias", exp.TableAlias(this=table.this.copy()))
            named.append((_qualifier_key(table), table))
        table.replace(replacement)
    _drop_schema_from_qualified_columns(scopes, named)
    if not ctes:
        return RewrittenQuery(sql=parsed.sql(dialect=dialect), parameters=parameters)
    if not isinstance(parsed, exp.Query):
        raise ValidationError(f"{parsed.key.upper()} may not reference a catalog table.")
    new_ctes = [
        exp.CTE(this=body, alias=exp.TableAlias(this=exp.to_identifier(alias)))
        for alias, body in ctes.items()
    ]
    existing = parsed.args.get(_WITH_ARG)
    if existing is None:
        parsed.set(_WITH_ARG, exp.With(expressions=new_ctes))
    else:
        # The query's own CTEs may read the tables being rewritten, so the
        # generated ones have to be defined first, not appended after.
        existing.set("expressions", [*new_ctes, *existing.expressions])
    return RewrittenQuery(sql=parsed.sql(dialect=dialect), parameters=parameters)


def check_blacklist(sql: str, dialect: str = "duckdb") -> None:
    """Reject SQL that is not a plain read, or that reads files directly.

    Two structural rules carry the weight here, together: a submitted query
    must parse to a single `exp.Query` (a `SELECT`, a `WITH ... SELECT`, a
    `UNION`), and `_reject_statements_inside` then refuses a statement that is
    not a read anywhere inside it. The first rejects the whole *class* of
    non-query statements at the top level in one check —
    `LOAD`, `CREATE SECRET`, `SET`, `PRAGMA`, `DESCRIBE`, `EXPLAIN`,
    `ATTACH`, `COPY`, `INSTALL` — and, because sqlglot's duckdb dialect
    parses `;`-separated text to a single `exp.Block`, multi-statement
    payloads too; the second covers the ones that sit inside a CTE or subquery.
    The named checks exist only to give a precise error message for the
    statements a user is most likely to try; they are not what makes the check
    sound.

    A second structural rule stands beside it: no submitted query may contain
    a `WITH RECURSIVE` clause anywhere. That is a deliberate non-support
    decision, not an oversight — sqlglot and DuckDB disagree about what a
    recursive CTE's name binds to inside its *base* term, and every
    scope-based classifier here inherits that disagreement as an
    authorization bypass. See `_reject_recursive_ctes`.

    A third structural rule: no submitted query may contain a query parameter
    or placeholder (`?`, `$1`, `$name`) anywhere. The rewrite binds each
    table's resolved file URLs as parameters, and a user placeholder would
    bind to them and return them as data — see `_reject_placeholders`.

    A fourth structural rule: no statement that is not a read may sit anywhere
    inside the query, in a CTE, a subquery, a set operation or a `LATERAL` join,
    in any letter case — see `_reject_statements_inside`.

    Parameters
    ----------
    sql
        The original, pre-rewrite SQL to check.
    dialect
        The SQL dialect to parse `sql` as.

    Raises
    ------
    ValidationError
        If `sql` uses a table function other than the generators in
        `_ALLOWED_TABLE_FUNCTIONS`; if it calls a blacklisted function; if it contains an
        `ATTACH`, `COPY`, or `INSTALL` statement (each parses to sqlglot's
        own dedicated expression class — `exp.Attach`, `exp.Copy`,
        `exp.Install` — rather than a generic command node, confirmed against
        the pinned `sqlglot` version, not assumed); if it is a `LOAD` or
        `CREATE SECRET` command; if it contains a query parameter or
        placeholder; if the statement as a whole is not a query; if a statement that
        is not a read sits anywhere inside it; or if it
        contains a `WITH RECURSIVE` clause.
    """
    parsed = sqlglot.parse_one(sql, dialect=dialect)
    for item in parsed.find_all(exp.Table, exp.Lateral):
        function = _from_item_function_name(item)
        if function is not None and function not in _ALLOWED_TABLE_FUNCTIONS:
            raise ValidationError(
                f"table function {function} is not allowed; only range, generate_series "
                "and unnest may be called in FROM. Register the data as a table and reference it."
            )
    for func in parsed.find_all(exp.Func):
        name = func.name.lower() if isinstance(func, exp.Anonymous) else func.sql_name().lower()
        if name in _BLACKLISTED_FUNCTIONS:
            raise ValidationError(f"{name} may not be called directly; query the catalog instead.")
    for node_type, label in ((exp.Attach, "ATTACH"), (exp.Copy, "COPY"), (exp.Install, "INSTALL")):
        if parsed.find(node_type) is not None:
            raise ValidationError(f"{label} is not permitted in a submitted query.")
    for command in parsed.find_all(exp.Command):
        keyword = str(command.this).lower()
        if keyword in _BLACKLISTED_COMMANDS:
            raise ValidationError(f"{keyword.upper()} is not permitted in a submitted query.")
    _reject_placeholders(parsed)
    if not isinstance(parsed, exp.Query):
        raise ValidationError(
            f"Only a single query may be submitted, not {parsed.key.upper()}; "
            "submit one SELECT statement."
        )
    _reject_statements_inside(parsed, dialect)
    _reject_recursive_ctes(parsed)
