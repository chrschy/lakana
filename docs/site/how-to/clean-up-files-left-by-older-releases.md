# Clean up files left by older releases

After this guide you can see which table directories in an environment's storage no table owns
any more, and delete them. You need the admin role on the whole instance (the right to manage
environments, given on the instance and not on one environment).

Older releases kept a table's files for ever when the table was dropped, when its create failed,
or when its environment was deleted. Since the release that added table location records, the
catalog keeps a record of every table's directory, and the worker's sweep deletes the files of dropped tables after the undo
period. The directories from before have no record, so the sweep never touches them: it only
counts them, in a worker log line that starts `<n> table directories under managed/ in the
storage of environment <env> have no location record`. This guide removes them when you decide
to.

Nothing here is needed for Lakana to work: the leftovers only take up space.

## See what is there

Name the environment whose storage you want to look at. The command deletes nothing:

<!-- source: docs/examples/drop-and-restore-a-table/run.sh#orphaned-list -->
```bash
lakana env orphaned-locations legacy
```

It prints a table with each directory under `managed/`, relative to the environment's storage
root, with its number of files, their size and the time of its newest file. A line under the table
adds them up and names the command that deletes them.

A directory is listed only when nothing in the catalog uses it:

- no table, in any environment on the same storage root (deleted environments and dropped tables
  that can still be restored included), lives in it, inside it, or around it;
- no location record names it, lies inside it or around it;
- its name ends in a table id (a hyphen and 32 hexadecimal digits), as every table directory's
  does; and it is not another environment's storage root, nor inside one.

**Recent directories are kept.** A directory with a file newer than `--older-than` (30 days by
default) is listed with `recent` set and is never deleted. Use a longer period if you are not sure,
for example `--older-than 90d`.

A deleted environment can be named too: its leftovers are exactly what this is for. To clean up the
storage of an environment that an older release deleted, and that so no longer exists at all,
create an environment on the same storage root and name that one.

## Delete them

Add `--delete`. The command asks first; `--yes` skips the question:

<!-- source: docs/examples/drop-and-restore-a-table/run.sh#orphaned-delete -->
```bash
lakana env orphaned-locations legacy --older-than 30d --delete --yes
```

Each directory is checked again right before its files are deleted, with the same checks the
worker's sweep makes before it deletes a dropped table's files, and under the same lock. A
directory that a table, a create in progress or a dropped table started to use in the meantime is
kept, and the table names the reason. The command stops after about 20 seconds; when it says so,
run it again and it continues where it stopped.

The audit log records one `ORPHANED_LOCATIONS_DELETED` event per `--delete`, with the environment,
`older_than_days`, the number of directories deleted (`count`), `files`, `bytes`, the first 100
locations (`locations`, and `more_locations` for the rest), how many were `kept`, and `complete`.
A run without `--delete` records nothing.

!!! warning "After restoring the catalog from a backup"
    A table created after the backup has no record in the restored catalog, so its directory is
    listed here like a leftover. Do not delete directories younger than the backup: set
    `--older-than` to more days than the backup's age. See "After a restore" in
    [Deploy with Docker Compose](deploy-with-compose.md#after-a-restore).

## The same over REST and the SDK

- `GET /api/v1/environments/{name}/orphaned-locations?older_than_days=30` lists;
  `POST /api/v1/environments/{name}/orphaned-locations/delete` with `{"older_than_days": 30}`
  deletes. Both answer with `environment`, `older_than_days`, `complete` and `locations`, one
  object per directory with `location`, `files`, `bytes`, `newest_file_at`, `recent`, `deleted`,
  `files_deleted`, `bytes_deleted` and `kept_because`.
- In Python, `client.environments.orphaned_locations("legacy")` and
  `client.environments.delete_orphaned_locations("legacy", older_than_days=30)`.

`older_than_days` is 1 to 3,650. An administrator of one environment is refused (403), because the
storage root may be shared with other environments.

## Related

- [Drop and restore a table](drop-and-restore-a-table.md): what happens to a table's files today.
- [The sweep](deploy-with-compose.md#the-sweep): what the worker deletes on its own, and when.
