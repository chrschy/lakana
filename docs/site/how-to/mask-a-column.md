# Mask a column for some people

After this guide a column of a table shows a replacement value, such as `***`, to the people you
choose, and everyone else still sees the real value. You also know how to list and remove a mask,
and what a mask does not protect. You need a table (see "Create the table first" below, and
[Getting started](../tutorials/getting-started.md)), a principal or a group to mask it for, and an
administrator's profile: creating, listing and removing masks needs the `MANAGE_POLICY` action, which
only the `ADMIN` role includes.

A **column mask** says: when this principal (or anyone in this group) reads this column of this
table, give them this expression instead of the stored value. The data in storage does not change.
The mask is applied inside every query the person runs, so a join, a view, a `SELECT *` and a write
into another table all see the masked value.

Create the table with its columns first, then its masks and filters, and only then write data or
give anyone `READ_TABLE` (see [Create the table first](filter-rows.md#create-the-table-first)).

A mask hides a column only from the people it names. A person it does not name sees the real value
until a mask covers them. Group membership is looked up at every query: someone you add to a masked
group is masked from their next query, and someone you remove from it sees the real value again from
their next query.

## Add a mask

Name the table as `namespace.table`, the column, the person or group, and the replacement:

<!-- not tested: needs a running instance and an administrator's profile; packages/lakana-server/tests/cli/test_column_masks_howto.py runs these commands against the real server and checks what they print -->
```bash
lakana column-mask create gold.customers ssn --principal alice --expression "'***'"
lakana column-mask create gold.customers ssn --group support --expression "substr(ssn, 8, 4)"
```

The first command masks `ssn` for the principal `alice` with the text `***`; the second masks it for
the group `support` so that they see only its last four characters. Pass exactly one of `--principal`
and `--group`. The principal or group must already exist. `lakana column-mask create` prints the
new mask's id:

<!-- output -->
```text
Created column mask 49fa89ee-275a-4b86-a6fc-76b2b5b32b7e on dev/gold.customers.ssn
```

The replacement is a small SQL expression. It may use:

- literals (`NULL`, `'***'`, numbers) and comparisons and boolean logic;
- the **bare** names of columns of the same table, which give the stored value (`ssn`, never `customers.ssn`; a field of a struct column such as `address.country` is not supported; to cover a struct column, mask the column as a whole, for example `address` with the replacement `NULL`, and a query that reads a field of it gets `NULL`);
- `current_user()` and `has_group('name')`;
- `substr(...)` or `substring(...)`, the `||` operator that joins text, and `md5(...)`.

Everything else is refused when you create the mask, for example `sha256(ssn)`, `upper(ssn)`,
`id + 1`, `CASE` and a subquery. The expression is kept as written and may be at most 100,000
characters (`LAKANA_QUERY_LIMITS__MAX_STATEMENT_CHARS`).

## What a reader sees

Take the table `gold.customers`, with two rows, and the mask on `ssn` for `alice` made above. Alice
runs a query with her own profile:

<!-- not tested: needs a running instance and alice's profile; packages/lakana-server/tests/cli/test_column_masks_howto.py runs these commands against the real server and checks what they print -->
```bash
lakana --profile alice sql "SELECT id, name, ssn FROM gold.customers ORDER BY id"
lakana --profile bob sql "SELECT id, name, ssn FROM gold.customers ORDER BY id"
```

Alice, who has the mask, gets the replacement:

<!-- output -->
```text
┏━━━━┳━━━━━━━┳━━━━━┓
┃ id ┃ name  ┃ ssn ┃
┡━━━━╇━━━━━━━╇━━━━━┩
│ 1  │ Ada   │ *** │
│ 2  │ Grace │ *** │
└────┴───────┴─────┘
2 rows
```

Bob, who has no mask, gets the stored values:

<!-- output -->
```text
┏━━━━┳━━━━━━━┳━━━━━━━━━━━━━┓
┃ id ┃ name  ┃ ssn         ┃
┡━━━━╇━━━━━━━╇━━━━━━━━━━━━━┩
│ 1  │ Ada   │ 111-11-1111 │
│ 2  │ Grace │ 222-22-2222 │
└────┴───────┴─────────────┘
2 rows
```

The mask applies before the query's own conditions, so Alice cannot find a value by asking for it:
`WHERE ssn = '111-11-1111'` compares the masked value and returns no row. Other columns are not
masked: Alice still sees `name`. Mask every column that identifies a person.

When two or more masks apply to the same column of the same person (one made for her, one for a group
she is in), Lakana does not guess which should win. The column reads `NULL` for her.

## List and remove masks

<!-- not tested: needs a running instance and an administrator's profile; packages/lakana-server/tests/cli/test_column_masks_howto.py runs these commands against the real server and checks what they print -->
```bash
lakana column-mask list --principal alice
lakana column-mask revoke 49fa89ee-275a-4b86-a6fc-76b2b5b32b7e --yes
```

`list` takes `--principal`, `--group` or `--env` to narrow the list; it shows the id of each mask,
which `revoke` needs:

<!-- output -->
```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━┓
┃ id                                   ┃ principal_id                         ┃ group_id ┃ environment ┃ namespace ┃ table     ┃ column ┃ expression ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━┩
│ 49fa89ee-275a-4b86-a6fc-76b2b5b32b7e │ 52acdc6f-ad90-4c8f-9f92-e0f4c990b1b1 │          │ dev         │ gold      │ customers │ ssn    │ '***'      │
└──────────────────────────────────────┴──────────────────────────────────────┴──────────┴─────────────┴───────────┴───────────┴────────┴────────────┘
```

After `revoke` (without `--yes` it asks first) the person sees the stored value again on her next
query; `list` then prints `(none)`:

<!-- output -->
```text
Revoked column mask 49fa89ee-275a-4b86-a6fc-76b2b5b32b7e
```

Revoking is the only way to change a mask: to change an expression, create the new mask and revoke
the old one. Until you revoke the old one, the two masks on the column make it read `NULL`. The same
commands are `client.column_masks.create(...)`, `.list(...)` and `.revoke(...)` in the Python SDK
and `/api/v1/column-masks` in the REST API.

## When Lakana refuses

Lakana checks a mask when you create it, so a mistake is an error now and not a failure of someone's
query later. The messages are the ones the commands print; the exit code of the command is in
brackets.

| You see | Why and what to do |
|---|---|
| `Table 'gold.customers' in environment 'dev' has no column 'sn'. Its columns are: id, name, ssn. Check the spelling; column names are case-sensitive.` (8) | The column is not in the table. Pick one of the columns listed. If the name differs only by case, the message ends with `Did you mean 'ssn'?` |
| `'read_parquet.ssn' is not a valid mask expression: the column 'read_parquet.ssn' is written with a qualifier. Write the bare column name, 'ssn'; a column mask always reads the table it is set on.` (8) | A column name in the expression has a dot in front of it (`t.ssn`, `customers.ssn`). Write `ssn`. A name whose first part is a column of the table says instead that a column mask does not support struct fields. |
| `'sha256(ssn)' is not a valid mask expression: SHA2 is outside the restricted subset.` (8) | The expression uses something outside the list above. Use only what the list allows. |
| `Pass exactly one of --principal or --group` (2) | Name one person or one group, not both and not neither. |
| `No principal named 'nobody'.` (6) | The principal does not exist. Create it first, or fix the spelling. A group says `No group named '...'.` |
| `No table 'nope' in 'dev'/gold.` (6) | The table does not exist in that environment. Check the name and `--env`. |
| `'bob' may not MANAGE_POLICY this.` (5) | Your profile is not an administrator's. Ask one to create the mask. |
| `'gold.names' is a view; row filters and column masks apply to tables. Attach them to the view's base tables.` (8) | A view has no data of its own: mask the table the view reads. Whoever reads the view gets the masks of the tables beneath it. |
| `'gold.churn' is an artifact; row filters and column masks cannot be attached to an artifact. Whoever may read an artifact reads all of it.` (8) | A mask cannot cover a file. See "What a mask does not protect". |
| `You need READ_TABLE on 'gold.customers' so that the column can be checked.` (5) | To check the column, Lakana reads the table's columns with your rights. Ask for `READ_TABLE` on the table. |
| `Table 'gold.customers' has no schema recorded yet, so its columns cannot be checked. Create the table with its columns first (lakana table create NAMESPACE.TABLE --column NAME:TYPE), then create the mask, then grant READ_TABLE or write data.` (7) | Lakana has no schema for the table: it was dropped or replaced while you created the mask. Create the table with its columns first, then the mask, then write data (see "Create the table first" in [Show some people only some rows](filter-rows.md#create-the-table-first)). |
| `The columns of table 'gold.customers' could not be read; the server's log has the cause. Create the mask again when the table is readable.` (7) | Storage or the table's metadata could not be read. Ask the person who runs the instance to look at the server's log, then try again. |
| `The column-mask expression is 100001 characters long; the most Lakana accepts is 100000 (LAKANA_QUERY_LIMITS__MAX_STATEMENT_CHARS). ...` (8) | The expression is far longer than a mask needs. Shorten it. |

A column added to the table later reads as NULL in the rows written before it, also under a
mask, and a renamed column keeps its data. See [Show some people only some rows](filter-rows.md#when-a-tables-schema-changes).

A mask is bound to a column **name**, not to the column itself. When the column is dropped and
nothing takes its name, every query of the masked person that reads the table fails with
`Column mask references unknown column 'ssn'; known columns: ['id', 'name']`. Revoke the mask and
create it again for the column it is meant for. A rename is different: see the next section.

### After a rename

A rename can change what a mask covers: a column renamed *onto* the masked name would be masked in
the old column's place, and the renamed column would show its values in clear under its new name.
Lakana does not let that happen. Each mask records which version of the table's schema it was
created against. When a column was renamed since, or replaced by a new column of the same name
(dropped and added again), and the old or the new name is one the mask uses (the masked column, or
a column its expression reads), a read of the table by the masked person is refused, and nothing
is shown:

<!-- output -->
```text
`gold.customers` has a row filter or column mask that uses column `ssn`, and a column was renamed or replaced since the policy was created, so the policy may now cover a different column. Nothing is shown. An administrator must review the table's row filters and column masks, create each one that uses a renamed or replaced column again for the column it is meant for, and then revoke the old one.
```

A column is also replaced when a write with `evolve_schema` changes its type (not a widening, such
as `int` to `long`): the column gets a new field id, reads of the table are refused until the mask
is created again, and the rows written before read as NULL in that column.

A rename therefore goes in this order:

1. Rename the column. From then on, reads of the table by the masked person are refused. That is
   the point: nothing shows in clear while the mask is out of date.
2. Create a mask for the column it is meant for. Lakana only accepts a mask on a column the table
   has now, so it cannot be created before the rename. Reads are still refused, because the old
   mask is still out of date, and two masks on one column read as `NULL`.
3. Revoke the old mask. List the masks and row filters of the table with the `list` subcommand of
   `lakana column-mask` and of `lakana row-filter`.
4. Reads work again, with the mask on the right column.

Do not revoke the old mask first: until the new one exists, the renamed column would show its
values in clear.

A mask created after the rename records the new version of the schema, so it is not refused. A
mask that an earlier release created has no recorded version: it is judged from the oldest schema
of the table, so a rename from any time before also refuses the read until it is created again.

## What a mask does not protect

A mask is a way to keep a column out of the answers of Lakana's own queries. It is not
encryption, and it does not cover the following.

- **Files read directly.** Masks are applied by Lakana's query path. Anyone who holds credentials to
  the object storage by some other means, or who reads files through the Iceberg REST endpoint
  with storage credentials of their own, reads the raw files with no mask. Lakana's Iceberg REST
  endpoint does not give out storage credentials. Keep the storage credentials with the people who run
  the instance.
- **Artifacts.** A mask cannot be attached to a file. A model that was trained on the unmasked table
  can reveal what it was trained on to everyone who may read the artifact. Choose with that in mind who
  trains a model on a table that holds masked columns, and who may read the model.
- **Inference from aggregates.** A mask stops someone from reading a value. It does not stop them from
  working out a value by combining enough counts, sums and groupings of other columns. Lakana does not
  try to prevent that.
- **What the replacement gives away.** `substr` shows part of the value on purpose, and `md5` gives
  everyone with the mask a fixed fingerprint: for a value that has few possibilities, such as a
  birth date, anyone can compute the fingerprints of all of them and read the value back. Use `NULL` or
  a fixed text when the value must not be reconstructed.
- **Copies.** A write run copies what its author sees. If an unmasked person writes the real values
  into another table, that table has no mask until you create one; and a person who is masked writes
  only masked values.
- **People who can change masks.** The administrators who may create and revoke masks may take them
  away. An administrator is masked too, if a mask names them: nothing is exempt from the masks that
  apply to a person. A compromised administrator account is not a case Lakana defends against.
- **Python tasks.** A Python task runs as the worker's operating system user, with the task's own
  token as its only limit. Anyone who may deploy Python code can therefore read whatever the worker
  can read, including the files of every table and the credentials the worker holds, and masks do not
  apply to that. One Lakana instance is one trust domain: deploy Python code only from people you would
  trust with the stored data, or run one instance per client.
- **Anyone no mask names.** See the start of this guide: a mask is added per person or per group.

Row filters (`lakana row-filter`) work the same way for rows: they hide the rows that fail a condition.
They have the same limits; see [Show some people only some rows](filter-rows.md).
