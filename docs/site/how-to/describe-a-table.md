# Describe a table

After this guide a table and its columns carry a description that people see in `lakana table get`
and through the REST API. You know where the text comes from, how to change it by hand, and how to
go back to the text in your project. You need a table (see [Getting started](../tutorials/getting-started.md))
and to own it or to hold the right to drop it, which the data engineer and admin roles include.

A table has two possible descriptions, and Lakana shows one of them:

- **The project's text.** It is the `description:` and `columns:` of the asset's header. Every
  deploy publishes it, so git stays the place where the text is kept up to date.
- **An edit.** It is a text you set with `lakana table describe`. It is shown instead of the
  project's text, and a deploy never replaces it.

The same holds for each column.

## Write the text in the project

Put `description:` and `columns:` in the header of the asset that builds the table:

<!-- source: docs/examples/describe-a-table/project/assets/silver/orders.sql -->
```sql
/* lakana
description: One row per customer order.
columns:
  id: The order's number.
  status: Where the order stands.
*/
SELECT id, status FROM bronze.orders
```

A deploy writes these texts onto the table, and onto its columns, as the project's text. A table
the project builds is described as soon as the first run creates it. A column that the header does
not name has no project text.

## Change a description by hand

Name the table as `namespace.table`, and add `--column` to describe one of its columns:

<!-- source: docs/examples/describe-a-table/run.sh#describe -->
```bash
lakana table describe silver.orders --text "One row per customer order, whatever its state."
lakana table describe silver.orders --column status --text "open, shipped or cancelled."
```

A description is at most 4,000 characters and is not blank. A longer one is refused with the limit
and a request to write a shorter one. A column must be in the table's schema now.

`lakana table get` shows the table's text, whether it is your edit or the project's, and the text of
each column:

<!-- source: docs/examples/describe-a-table/run.sh#get -->
```bash
lakana table get silver.orders
```

With `--output json` the answer also says where each text came from: `description_source` is
`edited` for your text and `project` for the header's.

## Go back to the project's text

`--reset` removes your edit, so the project's text shows again. A table the project does not describe
then shows no description.

<!-- source: docs/examples/describe-a-table/run.sh#reset -->
```bash
lakana table describe silver.orders --reset
lakana table describe silver.orders --column status --reset
```

## Who may change a description

The table's owner, which is whoever created it, and anyone who holds `DROP_TABLE` on it. Changing a
table's or a column's text also needs the right to read the table. A refusal is recorded in the audit log like any
other. Every change is recorded as `TABLE_DESCRIBED` or `COLUMN_DESCRIBED`; the record holds the table,
the column and the length of the edit before and after, not the text. The facts of the routes, the
SDK and the audit events are in [Descriptions](../reference/descriptions.md).
