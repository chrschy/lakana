# Security overview

How Lakana decides who may do what, where it keeps secrets, what it records, and how a query is
kept inside what its author may read. This page is the short version for people who use or run an
instance; each part links to the page with the steps and the exact rules.

## Who may do what

Every request is made by a **principal**: a person or a service account, signed in with a token.
A principal may do something only because a **grant** says so. A grant gives an action, or a
**role** (a named set of actions such as `VIEWER`, `ANALYST`, `DATA_SCIENTIST`, `DATA_ENGINEER` or
`ADMIN`), to a principal or a group, on an object: the instance, an environment, a namespace or a
table. Without a grant the answer is no, and the refusal is recorded.

- **Give the smallest set.** A role on an environment covers every table in it, tables created
  later included. [Give a second person access](../how-to/give-a-second-person-access.md) shows the
  grants for a reader, and how to take access away.
- **Ask why.** [Why may (or may not) someone do this?](../how-to/why-may-someone-do-this.md) names the
  grant, role or group behind an answer. The lists and the actions are in
  [Grants, permissions, principals and groups](../reference/access.md).
- **Row filters** show a person only the rows that meet a condition. **Column masks** show a
  replacement value instead of a column. Both are applied inside every query the person runs, a join, a
  view and a write into another table included, and they follow a person or a group. Create them
  before you give access: [Show some people only some rows](../how-to/filter-rows.md) and
  [Mask a column for some people](../how-to/mask-a-column.md).
- **Protected environments** accept writes only from deployed projects, so a person cannot change a
  production table by hand ([Projects and deploys](../how-to/projects.md#protected-environments-lockdown-and-break-glass)).

## Where secrets live

A secret (a storage key, a repository token, a package index password, a webhook address) is stored
encrypted in the instance's database, under a master key that you set with
`LAKANA_SECRETS__MASTER_KEY`. No route, command or page returns its value again; to change it you set it
again. A secret is **bound** to one use, for example one package index or one repository, and Lakana
refuses it for anything else, so the person who registers an index cannot send a secret to a host
of their choice. A secret is decrypted only where it is used, and only while it is used.

Keep the master key and the `.env` file of a Compose bundle with the people who run the instance: whoever
holds them holds every secret, and a lost master key makes every stored secret unrecoverable. The steps
are in [Manage secrets](../how-to/manage-secrets.md) and
[Use a private package index](../how-to/private-package-index.md).

## What the audit log records

The audit log is an append-only list of events: who signed in or failed to, what was granted
and revoked, who created, rotated or deleted a secret, who changed an environment, a filter or a mask,
who deployed, ran or undid something, and every refusal. A completed query is recorded with its SQL
text. For that reason reading the log needs its own right, `READ_AUDIT_LOG`, which the `ADMIN` role
holds, and seeing query text and column values in **other people's** events (the SQL text of a
query, the watermark of an incremental write) needs one more, `READ_QUERY_TEXT`, which no role
holds: an administrator sees a query's SHA-256 and length instead, and of a watermark only that
it is hidden, until someone grants the right to them by name. Your own events always show in full. An operator can also store less
text in new events (`LAKANA_AUDIT__QUERY_TEXT`: `full`, `hash` or `off`); see
[Other people's query text and column values](../reference/lists.md#other-peoples-query-text-and-column-values-in-the-audit-log).
`lakana audit list` filters it by event type, environment, principal and time
([the command](../reference/cli.md#lakana-audit-list)). To keep the log bounded, repeated refusals of one
principal are counted after a number per minute that an operator sets
(`LAKANA_DENIAL_AUDIT__PER_PRINCIPAL_PER_MINUTE`, [settings](../reference/settings.md)).

Seeing other people's activity is recorded too. When a manager lists other people's queries
(`lakana query list`), an administrator sees who read a table (`lakana table health`), or someone
reads other people's events in the audit log itself (`lakana audit list`), the log gets
an `ACTIVITY_VIEWED` event: who looked, at which environment or table, with which filters, and how many
rows of how many other people the answer showed, never their names or their SQL. It is written once per
person and view per hour, so paging, other filters and a screen that refreshes itself add nothing; a
filter on one person (`--submitter`) is its own view. Looking only at your own activity records nothing.
If the event cannot be written, the read is refused rather than served unrecorded. The events and
the setting that turns them off are in [the lists reference](../reference/lists.md#audit).

## How a query is kept inside its limits

Policies are applied by rewriting the SQL a person submits, but Lakana does not rely on the rewrite
alone, and it does not try to forbid dangerous DuckDB functions one by one. A query is kept inside
what its author may read by **credential scoping**:

- The worker works out, from the catalog, the files the author may read, and gives DuckDB a
  time-limited link to exactly those files.
- The process that runs the query holds no storage credential, no database address and no token,
  and the local file system is closed to it. A query that reads a path it was not given has nothing to
  read it with.
- Limits on time, memory, rows and bytes stop a query that asks for too much
  ([Stop a query](../how-to/stop-a-query.md)).

A query only reads. It is one `SELECT` (also with `WITH`, or a set operation such as `UNION`), or
a `VALUES` list such as `VALUES (1, 'a'), (2, 'b')`, whose columns are named `col0`, `col1` and
so on. A statement that changes data or the session is refused wherever it sits, also inside a CTE
or a subquery, and so is a query that reads files directly instead of a table: register the data
as a table and select from it.

## What this does not protect

- **One instance is one trust domain for Python code.** A Python task runs as the worker's operating
  system user, so someone who may deploy Python code can read what the worker reads, filters and masks
  included. Deploy Python only from people you would trust with the stored data, or run one instance per
  group that does not trust the other
  ([why](python-assets.md#isolation-one-instance-is-one-trust-domain)).
- **Files read outside Lakana.** A filter or a mask guards Lakana's own query path, not the files in
  the object storage. Keep the storage credentials with the people who run the instance.
- **Local mode.** With storage on the local disk there is no cloud credential to scope, so a local
  instance relies on the other checks alone. Use a team deployment ([Deploying with Compose](../how-to/deploy-with-compose.md))
  for more than one person.

For the connections an instance makes, see [What leaves the machine](what-leaves-the-machine.md).
