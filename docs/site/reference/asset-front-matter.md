# Asset front-matter

The `/* lakana … */` block that starts an asset file: every key it takes and what a deploy
does with it. The layout of the files is in [Project layout](project-layout.md).

An optional `/* lakana … */` block, first in the file:

<!-- not tested: moved as is; sorted in the second pull request -->
```sql
/* lakana
description: Cleaned hourly weather observations, nulls dropped.
owner: data-eng
tags: [weather, hourly]
materialize: incremental      # table | view | incremental
incremental_key: ts           # required exactly when incremental
evolve_schema: false          # incremental only
retries: 3                    # run policy, see lakana.yaml: "Retries and timeouts"
retry_delay: 1m
timeout: 5m
columns:
  station_id: WMO station identifier.
*/
SELECT station_id, ts, temp_c FROM bronze.weather WHERE temp_c IS NOT NULL
```

`description` and `columns` describe the table and its columns. A deploy publishes them as the
project's text, and a description set with `lakana table describe` wins over them and survives
every deploy; see [Describe a table](../how-to/describe-a-table.md).

An asset may call only the generator table functions `range`, `generate_series` and `unnest`,
which read no data and are not inputs; every other table function (`read_parquet`, `iceberg_scan`,
`glob`, anything unknown) fails the deploy with an issue at `path:line` (the line where the SQL body starts) — register the
data as a table and reference it.

An asset is one `SELECT`. A statement that changes data or the session (`INSERT`, `UPDATE`,
`DELETE`, `MERGE`, `COPY`, `CREATE`, `DROP`, `ATTACH`, `SET` and the like) fails the deploy
wherever it sits, also inside a CTE or a subquery, with ``A query reads data and cannot change
it: `DELETE` is not allowed, also not inside a CTE or subquery.`` A query may not call
`getvariable` either.

Unknown keys are errors. YAML anchors, aliases, `<<` merge keys and duplicate keys are refused
(the error names the file and line); `evolve_schema` and `column_docs` must be real booleans, not
quoted strings or numbers. `evolve_schema` that changes a column's type (other than widening it)
makes the column a new one: the rows written before read as NULL in it, and the row filters and
column masks that use it must be created again, as reads under them are refused until then. Each block is at most 64 KiB, and it ends at the first `*/`, even
inside a string or scalar (when the leftover text then fails to parse, the error says so).
`table` is written with `replace` (`create` the first time), `view` as a view, `incremental` with
`append` on `incremental_key`. An incremental asset needs no table made beforehand: its first run
creates the table with every row the `SELECT` returns, and each later run appends the rows whose
`incremental_key` is above the highest value already in the table. A first run that returns no
rows creates the table empty, and the next run takes every row. The key must be a column of the
result, of an integer, decimal, date or timestamp type; a first run with another key fails before
it creates anything. Because the first run creates the table, the run's principal needs the right
to create tables in the asset's namespace (`CREATE_TABLE`), not only to write them. Without it the
first run fails with a message that names the right and the table, and the next run works once
you grant it, or create the table yourself.
