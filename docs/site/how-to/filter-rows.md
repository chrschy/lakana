# Show some people only some rows

After this guide a table shows only the rows that meet a condition to the people you choose, and
everyone else still sees every row. You also know how to list and remove a filter, and what
Lakana refuses when you create one. You need a table (see "Create the table first" below, and
[Getting started](../tutorials/getting-started.md)), a principal or a group to filter for, and an
administrator's profile: creating, listing and removing filters needs the `MANAGE_POLICY` action,
which only the `ADMIN` role includes, and creating one also needs `READ_TABLE` on the table so
that Lakana can check its columns.

A **row filter** says: when this principal (or anyone in this group) reads this table, they get
only the rows for which this condition is true. The data in storage does not change. The filter is
applied inside every query the person runs, so a join, a view, a `SELECT *` and a write into another
table all see only the filtered rows. It is the same idea as a [column mask](mask-a-column.md), for
rows instead of values, and it has the same limits.

## Create the table first

A filter names columns of the table, so Lakana checks them when you create it, and the table must
have them. Create the table with its columns, then its filters and masks, and only then write
data or give anyone `READ_TABLE`:

<!-- source: docs/examples/filter-rows/run.sh#table -->
```bash
lakana table create gold.customers --column id:long --column region:string --column tier:string
```

The order matters for safety. A grant on the environment or the namespace covers a table the
moment it exists, so rows written before the filter exists can be read unfiltered by everyone
that grant reaches. A table that already holds data works the same way; the filter applies from
its creation on.

## Add a filter

Name the table as `namespace.table`, the person or group, and the condition:

<!-- source: docs/examples/filter-rows/run.sh#create -->
```bash
lakana row-filter create gold.customers --principal alice --expression "region = 'EU'"
lakana row-filter create gold.customers --group sales_us --expression "region = 'US' AND NOT (tier = 'vip')"
```

Pass exactly one of `--principal` and `--group`; the principal or group must exist. The condition
is a small SQL expression. It may use:

- literals, comparisons (`=`, `<>`, `<`, `<=`, `>`, `>=`) and `AND`, `OR`, `NOT`;
- the **bare** names of the table's columns: write `region`, never `customers.region` or
  `t.region`. A field of a struct column (`address.country`) is not supported;
- `current_user()` and `has_group('name')`.

Everything else is refused when you create the filter, for example `IN (...)`, `LIKE`, a function
call and a subquery. A condition on a column that holds `NULL` is not true for that row, so the row
is hidden. The condition is kept as written and may be at most 100,000 characters
(`LAKANA_QUERY_LIMITS__MAX_STATEMENT_CHARS`).

Each column the condition reads must exist in the table now. Lakana compares names without regard
to letter case, as the query does, so `REGION = 'EU'` is accepted for a column called `region`. A
condition that reads no column (`has_group('staff')`) is not checked against the table.

## List and remove filters

<!-- source: docs/examples/filter-rows/run.sh#list -->
```bash
lakana row-filter list --principal alice
```

`list` takes `--principal`, `--group` or `--env` to narrow the list; it shows the id of each filter,
which `revoke` needs:

`$ROW_FILTER_ID` is the id that `list` printed:

<!-- source: docs/examples/filter-rows/run.sh#revoke -->
```bash
lakana row-filter revoke "$ROW_FILTER_ID" --yes
```

Revoking is the only way to change a filter: to change a condition, create the new filter and
revoke the old one. When several filters apply to one person, a row must pass all of them. The same
commands are `client.row_filters.create(...)`, `.list(...)` and `.revoke(...)` in the Python SDK
and `/api/v1/row-filters` in the REST API.

## When Lakana refuses

Lakana checks a filter when you create it, so a mistake is an error now and not a failure of
someone's query later. The messages are the ones the commands print; the exit code is in brackets.

| You see | Why and what to do |
|---|---|
| `'t.region = 'EU'' is not a valid filter expression: the column 't.region' is written with a qualifier. Write the bare column name, 'region'; a row filter always reads the table it is set on.` (8) | A column name in a filter has a dot in front of it (`t.region`, `read_parquet.region`, `customers.region`). Write `region`. |
| `'address.country = 'DE'' is not a valid filter expression: 'address.country' reads a field of the column 'address', and a row filter does not support struct fields. Use a column of the table as a whole.` (8) | The first part of the dotted name is a column of the table, so it is a field of a struct column. Filter on a column as a whole. |
| `Table 'gold.customers' in environment 'dev' has no column 'regoin', which the row filter reads. Its columns are: id, region. Check the spelling.` (8) | The filter reads a column the table does not have. Pick one of the columns listed. If the table has no columns yet, the message says `The table has no recorded columns.` |
| `Table 'gold.customers' has no schema recorded yet, so its columns cannot be checked. Create the table with its columns first (lakana table create NAMESPACE.TABLE --column NAME:TYPE), then create the row filter, then grant READ_TABLE or write data.` (7) | Lakana has no schema for the table: it was dropped or replaced while you created the filter. Create the table with its columns first, then the filter, then write data, as at the top of this page. |
| `The columns of table 'gold.customers' could not be read; the server's log has the cause. Create the row filter again when the table is readable.` (7) | Storage or the table's metadata could not be read. Ask the person who runs the instance to look at the server's log, then try again. |
| `You need READ_TABLE on 'gold.customers' so that the column can be checked.` (5) | To check the columns, Lakana reads the table's columns with your rights. Ask for `READ_TABLE` on the table. |
| `'sha256(a)' is not a valid filter expression: ...` (8) | The condition uses something outside the list above. |
| `Pass exactly one of --principal or --group` (2) | Name one person or one group, not both and not neither. |
| `No principal named 'nobody'.` (6) | The principal does not exist. Create it first, or fix the spelling. A group says `No group named '...'.` |
| `No table 'nope' in 'dev'/gold.` (6) | The table does not exist in that environment. Check the name and `--env`. |
| `'bob' may not MANAGE_POLICY this.` (5) | Your profile is not an administrator's. Ask one to create the filter. |
| `'gold.names' is a view; row filters and column masks apply to tables. Attach them to the view's base tables.` (8) | A view has no data of its own: filter the table the view reads. |
| `'gold.churn' is an artifact; row filters and column masks cannot be attached to an artifact. Whoever may read an artifact reads all of it.` (8) | A filter cannot cover a file. |
| `The row-filter expression is 100001 characters long; the most Lakana accepts is 100000 (LAKANA_QUERY_LIMITS__MAX_STATEMENT_CHARS). ...` (8) | The condition is far longer than a filter needs. Shorten it. |

## When a table gains a column

After an append that adds a column, the files written before it do not have it. A read under a
row filter or a column mask on the table selects every column, so **every reader under any filter
or mask of that table fails**, whichever column the filter names, until the files agree. Nothing
is shown unfiltered. What the reader sees depends on whether their own query names the new column:

- A query that does not name it (`SELECT id FROM gold.customers`) gets ``The data files of
  `gold.customers` do not all have the column `tier` (it was added after some or all of them were
  written). A read under a row filter or column mask selects every column of the table, so it
  fails on them whichever column the policy names. Ask the owner of the table to write it again,
  so that every file has the column.``
- A query that names it (`SELECT tier FROM gold.customers`, or `WHERE tier = 'vip'`) gets the
  database's own line, without its list of candidate columns, and a hint: ``Query failed: Binder Error: Referenced column "tier" not found in FROM clause! `gold.orders` declares the column `tier`; if the query reads it from there, older data files of the table lack it (it was added after some or all of them were written). Ask the owner of the table to write it again, so that every file has the column.``
  The hint is the same whatever order the files are in. If your query has a mistake of its own (a
  subquery that leaves `tier` out), fix the query; the hint says only that the table's older files
  lack the column too.

The owner writes the table's data again (a `replace`), which gives every file all the columns, and
the reads run again. Readers under no filter or mask are not affected by these messages.

## Filters and dropped columns

A filter does not follow a column that is dropped or renamed afterwards. From then on a query of
the filtered person that reads the table fails instead of showing rows, so nothing is shown
unfiltered. Revoke the filter and create it again for the new column name. A filter stored by an
earlier release keeps working as it did, even if it would be refused today.
