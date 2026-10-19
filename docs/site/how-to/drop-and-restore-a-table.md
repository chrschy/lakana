# Drop and restore a table

After this guide you can drop a table, see which dropped tables can still be restored, bring one
back with its data and its access rules, and delete a table's files for good. You need a managed
table (see [Getting started](../tutorials/getting-started.md)) and the right to drop tables, which
the data engineer and admin roles include.

Dropping a table removes it from the catalog at once: nobody can read it or write to it, and its
grants, row filters and column masks stop applying. Its files stay in storage for the **undo
period**, seven days by default, and during that time the table can be restored as it was. After
the undo period the worker deletes the files.

## Drop a table

Name the table as `namespace.table`. Without `--yes` the command asks first:

<!-- source: docs/examples/drop-and-restore-a-table/run.sh#drop -->
```bash
lakana table delete silver.orders --yes
```

The table is gone from `lakana table list` and from every query. The answer of the command says how
to restore it.

## See what can be restored

`--dropped` lists the tables dropped from a namespace whose files are not deleted yet, newest first:

<!-- source: docs/examples/drop-and-restore-a-table/run.sh#list -->
```bash
lakana table list silver --dropped
```

`purge_after` is the moment the undo period ends (UTC). `restorable` is `yes` until then, and `no`
once it has passed or the worker has started to delete the files. You see the dropped tables of a
namespace if you may read the tables in it.

## Restore a table

<!-- source: docs/examples/drop-and-restore-a-table/run.sh#undrop -->
```bash
lakana table undrop silver.orders
```

The table comes back as it was when it was dropped:

- the same table, with the same id and the same data and history (an engine that kept the table's
  id sees the same table);
- its description and the descriptions of its columns, and its owner;
- exactly the grants, row filters and column masks that the drop took away. One that was revoked
  before the drop stays revoked.

What does not come back is the record of who read the table: [table health](../reference/table-health.md)
counts readers again from the restore on.

Restoring needs the right to drop tables and the right to create tables in the namespace. In a
protected environment it is refused like any other change outside a run; add `--break-glass` with a
`--reason`, as for a create.

### When the name is taken

If a new table has the dropped table's name, restore the old one under another name in the same
namespace:

<!-- source: docs/examples/drop-and-restore-a-table/run.sh#as -->
```bash
lakana table undrop silver.orders --as orders_before
```

If a name was dropped more than once, `lakana table undrop` brings back the newest. Restore it, or
drop the table that holds the name, then run the command again (with `--as` if needed) for the
older one.

## Delete a table's files now

`--purge` skips the undo period: the worker deletes the files at its next clean-up, and the table
cannot be restored.

<!-- source: docs/examples/drop-and-restore-a-table/run.sh#purge -->
```bash
lakana table delete silver.orders_before --purge --yes
```

## When the files are deleted

The worker's sweep deletes a dropped table's files **no earlier than the end of its undo period,
and no later than one sweep after it**. The sweep runs every `LAKANA_RUN__SWEEP_INTERVAL` seconds
(an hour by default); see [The sweep](deploy-with-compose.md#the-sweep). With the sweep turned off
(`LAKANA_RUN__SWEEP_INTERVAL=0`) nothing is deleted.

- **The length of the undo period** is `LAKANA_TABLES__UNDO_DAYS`, a setting of the server (7 by
  default, 0 to 365; see [Settings](../reference/settings.md)). Each drop records the end of its own
  period, so a change applies to later drops only. With 0, every drop is deleted at the next sweep
  and cannot be restored.
- **With versioning on the bucket**, a deleted file stays as an older version of its object, and the
  bucket's own rules decide when it is gone.
- **A table nested inside another table's directory** (in a namespace named after that directory)
  keeps its files for its own undo period even when the outer table is purged: the outer table's
  files wait until the nested one is restored or deleted.
- **An external table's files are never deleted.** Dropping one removes the catalog entry only; it
  is not listed by `--dropped` and cannot be restored. Register it again with
  `lakana table create-external`. A view has no files; a deploy of its project creates it again.
- **Tables dropped before this release** have no undo period: their files stay, and the worker's log
  counts their directories as directories without a record. A later release adds a command to remove
  them.

## Through the APIs

- **REST.** `DELETE /api/v1/environments/{env}/namespaces/{path}/tables/{name}` drops, with
  `?purge=true` to delete the files at the next sweep.
  `GET /api/v1/environments/{env}/namespaces/{path}/dropped-tables` lists the dropped tables (`name`,
  `namespace`, `uuid`, `kind`, `dropped_at`, `purge_after`, `restorable`).
  `POST /api/v1/environments/{env}/namespaces/{path}/tables/{name}/undrop` restores, with an optional
  body `{"new_name": "..."}`, and answers with the table.
- **SDK.** `client.tables.delete(env, "silver.orders", purge=False)`,
  `client.tables.list_dropped(env, "silver")` and
  `client.tables.undrop(env, "silver.orders", new_name=None)`.
- **Iceberg REST catalog.** A drop from an engine keeps the files for the undo period like any other;
  `purgeRequested=true` makes them due at the next sweep.

## If it does not work

| Message | Why | What to do |
|---|---|---|
| `No dropped table 'orders' in silver can be restored: ...` | Nothing of that name was dropped there, or its files are already deleted. | Check the name with `lakana table list silver --dropped`. |
| `silver.orders cannot be restored as 'orders': a table of that name exists in silver now. ...` | A new table, view or artifact has the name. | Add `--as NEW_NAME`, or drop the new one first. |
| `silver.orders cannot be restored: its undo period ended at ... (UTC), ...` | The period is over, or the table was dropped with `--purge`. | The files are deleted or about to be; restore from a backup if you have one. |
| `silver.orders cannot be restored: the worker's sweep has started deleting its files, ...` | The sweep began deleting before the restore. | Restore from a backup if you have one. |
| `silver.orders cannot be restored: its files are gone ...` | Its metadata file was deleted from storage by something other than Lakana. | Restore from a backup of the storage if you have one. |
| `silver.orders cannot be restored: its namespace silver was dropped after it. ...` | The namespace is gone. | Create the namespace again (`lakana ns create`), then restore the table. |

## What the audit log records

- `TABLE_DROPPED` for every drop, by the person who dropped it: `reason` (`user`, or `purge` for a
  drop with `--purge`), `table_id`, `kind`, `purge_after` (empty for a table whose files are not
  deleted, such as an external table) and `revoked_policies`, the number of grants, row filters and
  column masks the drop took away.
- `TABLE_UNDROPPED` for a restore: `table_id`, `name`, `dropped_name`, `dropped_at` and
  `restored_policies`, the kind and id of each grant, row filter and column mask that came back.
- `TABLE_PURGED` when the sweep deletes the files, with no person: see
  [The sweep](deploy-with-compose.md#the-sweep).
