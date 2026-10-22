# SQL in queries

Facts about the SQL a query, a view, a check or a SQL asset may contain, where Lakana's
behaviour differs from what you may expect from DuckDB alone. Lakana checks every statement
before DuckDB sees it and hands DuckDB its own rendering of what it checked.

## Lambdas

A lambda is an argument of a list function such as `list_transform`, `list_filter` or
`list_reduce`. Write it in any of these forms; all three mean the same:

| You write | Parameters |
|---|---|
| `x -> x + 1`, `(x, y) -> x + y` | one, or two in parentheses |
| `lambda x: x + 1`, `lambda x, y: x + y` | one or two |
| `LAMBDA x : x + 1`, `LAMBDA x, y : x + y` | one or two |

Lakana hands DuckDB every lambda as `LAMBDA x : x + 1`, the only form DuckDB 2.0 accepts
(it refuses `x -> x + 1` with "Deprecated lambda arrow (->) detected"). Queries, views and
SQL assets written with `->` keep running; there is nothing to change.

An arrow is a lambda only in a function that takes one: `list_transform`, `list_apply`,
`list_filter`, `list_reduce`, their `array_` names, `filter` and `COLUMNS`. Write `list_apply`
for DuckDB's `apply` and `list_reduce` for its `reduce`: Lakana cannot read `apply` with a
lambda, and reads `reduce` with its arguments in another order, so DuckDB refuses it. Anywhere else, as in `upper(j -> '$.a')` or `coalesce(j -> 'a', 'x')`, it is the JSON operator,
as in DuckDB, and runs as one. A lambda written `lambda x:` or with two parameters is always
a lambda.

A lambda's body is checked like the rest of the query: a function a query may not call is
refused inside a lambda too. A row filter or a column mask may not contain a lambda.

## Integer division and remainder by zero

`//` (integer division) and `%` (remainder) by zero stop the query with "Division by zero in
expression (...)". Wrap the expression in `TRY(...)` to get `NULL` instead, or divide by
`NULLIF(x, 0)`: `TRY(total // count)`, `total % NULLIF(count, 0)`. `/` by zero gives
`Infinity` (or `NaN` for `0 / 0`), as before.

Lakana ran on DuckDB 1.5 before, where both returned `NULL`; a query or asset that relied on
that now fails with the message above until you add `TRY` or `NULLIF`.

## `~` matches the whole text

`'abc' ~ 'b'` is false in Lakana: `~` and `!~` match the whole text, as `regexp_full_match`
does. DuckDB 2.0 alone treats `~` as a partial match. For a partial match write
`regexp_matches(x, 'b')`, or `'.*b.*'` as the pattern. `SIMILAR TO` matches the whole text
too.

## A table function's alias is its value

In `SELECT i FROM generate_series(1, 3) i`, `i` is the number (a `bigint`), so the query
returns the numbers 1 to 3 and a write stores them as a `long` column. The same holds for
`range` and `unnest`. `i.generate_series` still works. On DuckDB 1.5 `i` was a struct, which a
write refused.

## A column that is only `NULL` needs a type

`SELECT NULL AS x` has no type of its own. Give it one with `CAST`:
`CAST(NULL AS VARCHAR) AS x`, or the type the column should have in the table.

| Where | Without the `CAST` |
|---|---|
| A read | Runs; the column is empty. |
| A view | Is created; on DuckDB 2.0 the column's type reads `null`. |
| A write to a table | On DuckDB 2.0 refused with "Column 'x' is NULL without a type, which a Lakana table can't store; write `CAST(NULL AS <type>) AS x`, for example `CAST(NULL AS VARCHAR)`." On DuckDB 1.5 the column is created as an `integer`, which is rarely what you want. |

## Functions a query may not call

A query reads tables, so a function that reads files, runs SQL from a string, reveals the
engine's settings or changes its state is refused, and the message names the function. Two
groups have a message of their own:

| Functions | Refused because | What to do |
|---|---|---|
| `setval`, `nextval`, `currval` | they change or read a sequence, which is the engine's state, not data | remove the call |
| `to_aggregate_state` | it picks an aggregate function by a name given as data | remove the call |
| any name starting with `__` | it is internal to DuckDB | write the SQL it stands for, such as `CAST` or `BETWEEN` |
