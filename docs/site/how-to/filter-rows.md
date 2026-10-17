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
  `t.region`. A field of a struct column (`address.country`) is not supported, but a filter on
  another column works on a table that has a struct column, and the struct comes back whole;
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

## An expression must not fail on any stored value

A filter runs on every stored value, also on the ones it hides. Lakana checks the expression when you
create it, but not against the values in the table. If it fails on one, for example a conversion of text
that is not a number, everyone it applies to gets this message instead of the database's, which would
quote the value:

<!-- output -->
```text
The query failed on a value it could not convert or compute, under a row filter or column mask on `gold.customers`; the value is not shown. Check the casts and functions in the query. If the query has none that could fail, a row filter or column mask on the table fails on some stored values: ask an administrator to check it (for example with try_cast or TRY).
```

The same message appears when a person's own query fails on a value under a row filter or column mask. Where a conversion
may meet a value it cannot convert, write it so that it returns `NULL` instead of failing: `try_cast`,
`try_strptime` or `TRY(...)`. A query on a table with no filter or mask keeps the database's own message.

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

## Columns of a table made outside Lakana

Lakana's own writers make flat tables, but a table made by other tools can have a struct column
or a column whose type was widened. A filter names top-level columns only:

- **A struct column.** A filter on another column (`region = 'EU'`) works as on any table, and
  the struct column is returned whole. A filter that names a field of it (`address.country`) is
  refused, as the table above shows. To hide a struct column from someone, use a
  [column mask](mask-a-column.md) on the column as a whole.
- **A float column that was widened to double.** Rows written before the widening hold 32-bit
  values and rows written after hold 64-bit ones, so `1.1` is not the same number in both. What
  an equality with a decimal literal (`price = 1.1`) or its negation (`price <> 1.1`,
  `NOT (price = 1.1)`) matches depends on how the filter is
  written, so a filter meant to hide such rows can show some. Always write a range
  (`price > 1.09 AND price < 1.11`) in a filter. A query can also use a rounded value
  (`round(price, 2) = 1.1`). Both match the rows of old and new files alike, in either order.

## When a table's schema changes

Lakana reads an Iceberg table's data files by the field id of each column, as the Iceberg
specification requires, so a filter keeps working after the schema changes:

- **An added column** reads as NULL in the rows written before it was added. A filter on it
  (`tier = 'gold'`) passes none of those rows; `tier IS NULL` passes all of them.
- **A renamed column** keeps its data under the new name, in old and new files alike. The filter
  itself does not follow the rename: see the next section.
- **A column dropped and added again** under the same name is a new column. The rows written
  before read as NULL, never as the old column's values. A write with `evolve_schema` that
  changes a column's type (other than widening it) does the same.
- **A widened column** (`int` to `long`, `float` to `double`) reads in its new type. A float
  column needs a range in a filter, as said above.

A field inside a struct, list or map column is read by name. After such a field was renamed, or
dropped and added again, every read of the table fails with ``'gold.customers' cannot be read:
the field `town` of its column `address` was renamed, ...`` until its owner writes all of its rows
again (an overwrite). Nothing is shown in the meantime.

A raw-zone (external) table has no field ids: its files are read by column name. When some of its
files lack a column it declares, a read under a row filter of that table fails with
``The data files of `TABLE` do not all have the column `COLUMN` ...``, and nothing is shown
unfiltered. A column mask whose expression reads the column fails too, with a message that
depends on the order of the files; a constant mask works. Write the files again so that every
file has the column.

A column of a type Lakana cannot read yet (nanosecond timestamps, `unknown`, and the geospatial
types of Iceberg format version 3) makes every read of the table fail with ``'gold.events' has the
column 'x' of type timestamp_ns, which Lakana cannot read yet. Ask the owner of the table to store
the column as another type.`` The whole table is refused, also by a query that does not name the
column, so that nothing is hidden without a word. The owner stores the column as a type Lakana
reads (a microsecond timestamp, for example) and writes the table's rows again.

## Filters and dropped columns

A filter is bound to a column **name**, not to the column itself. When the column is dropped
and nothing takes its name, a query of the filtered person that reads the table fails instead of
showing rows. Revoke the filter and create it again for the column it is meant for. A rename is
different: see the next section.

### After a rename

A rename can change what a filter covers: a column renamed *onto* the filtered name would be
tested in the old column's place. The filter would test the other column's values and could pass
rows it was meant to hide, and the renamed column would no longer be filtered. Lakana does not let
that happen. Each filter records which version of the table's schema it was created against. When
a column was renamed since, or replaced by a new column of the same name (dropped and added
again), and the old or the new name is one the filter reads, a read of the table by the filtered
person is refused, and nothing is shown:

<!-- output -->
```text
`gold.customers` has a row filter or column mask that uses column `region`, and a column was renamed or replaced since the policy was created, so the policy may now cover a different column. Nothing is shown. An administrator must review the table's row filters and column masks, create each one that uses a renamed or replaced column again for the column it is meant for, and then revoke the old one.
```

A column is also replaced when a write with `evolve_schema` changes its type (not a widening, such
as `int` to `long`): the column gets a new field id, reads of the table are refused until the
filter is created again, and the rows written before read as NULL in that column.

A rename therefore goes in this order:

1. Rename the column. From then on, reads of the table by the filtered person are refused. That is
   the point: no rows are passed or hidden by a filter that is out of date.
2. Create a filter for the column it is meant for. Lakana only accepts a filter on a column the
   table has now, so it cannot be created before the rename. Reads are still refused, because the
   old filter is still out of date, and filters are ANDed, so the new one only narrows.
3. Revoke the old filter. List the filters and masks of the table with the `list` subcommand of
   `lakana row-filter` and of `lakana column-mask`.
4. Reads work again, with the filter on the right column.

Do not revoke the old filter first: until the new one exists, the person reads every row.

A filter created after the rename records the new version of the schema, so it is not refused. A
filter that an earlier release created has no recorded version: it is judged against every schema
the table has had, so a rename from any time before also refuses the read until it is created again.

A filter stored by an earlier release is judged by the same rule, even if its text would not be
accepted by `create` today.
