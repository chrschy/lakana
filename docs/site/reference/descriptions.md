# Descriptions

The description of a table and of a column: where it comes from, the commands, the SDK, the REST
routes, the rights and the audit events. For the steps, see [Describe a table](../how-to/describe-a-table.md).

## Two sources

| Source | Set by | Shown when |
|---|---|---|
| `project` | A deploy or a rollback, from the `description:` and `columns:` of the asset's header; or the creation of the table by a run, when the deployed project describes it | there is no edit |
| `edited` | `lakana table describe`, the SDK or the REST routes | always, once set |

A deploy never writes or removes an edit. A reset removes the edit. A column whose text the header no
longer has loses its project text at the next deploy. The text of a column that is no longer in the
table's schema is kept and is not shown; it shows again if the column comes back.

A description is at most 4,000 characters and is not blank. A table the project describes, but that
does not exist yet, gets the text when a run creates it.

## What the table shows

`GET .../tables/{name}` and the table list return, next to the existing fields:

| Field | Type | Meaning |
|---|---|---|
| `description` | string or `null` | The text shown: the edit if there is one, else the project's. `null` for none. |
| `description_source` | `"edited"`, `"project"` or `null` | Where `description` came from. |

Each entry of `schema` has the same two fields for its column.

## Commands

| Command | What it does |
|---|---|
| `lakana table describe NAME --text TEXT` | Sets the table's edit. |
| `lakana table describe NAME --column COLUMN --text TEXT` | Sets a column's edit. |
| `lakana table describe NAME [--column COLUMN] --reset` | Removes the edit. |
| `lakana table get NAME` | Shows the description and the column descriptions. |

Exactly one of `--text` and `--reset` is required.

## SDK

`client.tables.describe(environment, "namespace.table", text, column=None)` returns the `Table`.
`text=None` resets. `Table.description`, `Table.description_source`, `Column.description` and
`Column.description_source` hold what the server returned; they are `None` for an older server.

## REST routes

Under `/api/v1/environments/{env}/namespaces/{path}/tables/{name}`:

| Route | Body | Answer |
|---|---|---|
| `PATCH …/{name}` | `{"description": "text"}` or `{"description": null}` | The table (`200`) |
| `PUT …/{name}/columns/{column}/description` | the same | The table (`200`) |

`null` resets the edit, and a blank text is refused. The field must be sent: a body without
`description` is `422`. A text over 4,000 characters is `422`, a column the table does not have is
`404` and names the columns it has.

## Rights

The table's owner (the principal that created it), or a principal holding `DROP_TABLE` on the table.
Both routes also need `READ_TABLE`, checked before anything is written. There is no separate action. A refusal is `403` and is
recorded as `AUTHORIZATION_DENIED`.

## Audit events

| Event | When | `detail` |
|---|---|---|
| `TABLE_DESCRIBED` | A table's edit was set or reset | `table`, `old_length`, `new_length` (length of the edit, `0` for none), `reset` |
| `COLUMN_DESCRIBED` | A column's edit was set or reset | the same, and `column` |

The text is not recorded: it may be long, and the table shows it to anyone who may read the table.
