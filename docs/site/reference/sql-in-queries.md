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

A lambda's body is checked like the rest of the query: a function a query may not call is
refused inside a lambda too. A row filter or a column mask may not contain a lambda.

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
