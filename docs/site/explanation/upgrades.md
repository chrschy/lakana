# Upgrade policy

This page says what to expect when you move to a newer Lakana release, and what every upgrade
asks of you. It describes what is true today, before the public beta. For what the maintainer
promises and does not promise, see [Support](support.md).

## Versions

- Lakana is at 0.x. The four packages (`lakana`, `lakana-core`, `lakana-server` and
  `lakana-worker`) share one version, and the server, the worker and the CLI of one release belong
  together.
- **A minor release (0.N.0) may contain breaking changes**, until v1.0.0. A breaking change in a
  commit makes the next release a minor one, not 1.0.0.
- **A patch release (0.N.P) is for fixes** and is not meant to break anything. Nothing technical
  stops a breaking change from landing in a patch release: a change counts as breaking only when
  its author marks it so. Read the changelog entry of every release you skip.
- Fixes go into the latest release only (see [Support](support.md)). Upgrade first, then report.

## Where a breaking change is announced

- **The changelog.** Each release in
  [`CHANGELOG.md`](https://github.com/chrschy/lakana/blob/main/CHANGELOG.md) has a section
  "BREAKING CHANGES" when it breaks something. Each entry says what changed and how to migrate.
  Read it before you upgrade, for every release between yours and the new one.
- **A how-to page, when a release needs steps.** A short entry cannot carry an order of steps and
  a way back, so such a release has its own page. [Upgrade to the release with
  artifacts](../how-to/upgrade-to-artifacts.md) is the example.

## What every upgrade asks

1. **Back up first.** Take the Postgres dump and the storage backup of
   [Backups](../how-to/deploy-with-compose.md#backups).
2. **Upgrade the server and every worker together.** A worker of another release refuses some
   tasks. Do not leave an old worker running next to the new
   server.
3. **Run the database migrations before the new server starts.** The Compose bundle does that: its
   `migrate` service runs first, and the server and the workers wait for it, when you run
   `docker compose pull && docker compose up -d --wait` as in
   [Upgrading](../how-to/deploy-with-compose.md#upgrading). A local instance
   (`lakana server start --local`) migrates when it starts.
4. **Read the changelog entries**, and do what each says.

## What every minor release asks of projects with Python assets

Before v1.0.0 the `lakana` in a project's `uv.lock` must have the same minor version as the
instance. After every minor upgrade, in each project that has a Python asset (a `.py` file under
`assets/`):

1. In `pyproject.toml`, pin `lakana[assets]` to the new version.
2. Run `uv lock`.
3. Commit `pyproject.toml` and `uv.lock`, and deploy the project again.

Until then, each of that project's Python tasks fails with a message that ends like this, after
naming the two versions:

<!-- output -->
```text
This project version was deployed for another platform version: update the lock and redeploy.
```

A project with only SQL assets is not affected. A patch release does not ask for this. The full
steps, with the commands, are in
[Upgrade to the release with artifacts](../how-to/upgrade-to-artifacts.md#the-order), step 5.

This step is a known cost. [Issue 73](https://github.com/chrschy/lakana/issues/73) looks at
reducing it. From v1.0.0 the check only requires the major versions to match.

## Going back

A downgrade is not generally supported. No `lakana` command downgrades an instance. The way back
that always exists is to restore the backup you took before the upgrade, with
[After a restore](../how-to/deploy-with-compose.md#after-a-restore), on the previous image: it
loses everything changed since. For the release with artifacts, a manual downgrade of the database
migration also exists, and its page says what it loses: see
[Going back](../how-to/upgrade-to-artifacts.md#going-back).

## What v1.0.0 changes

v1.0.0 is the API freeze, planned for the hardening milestone (M10) of the
[roadmap](roadmap.md), which also lists upgrade paths. From then on the API stops changing in ways
that break your code ([Support](support.md)). This page has nothing more to say about it yet.

## Upgrade notes of the last releases

Each cell is a summary. The page named in the last column has the detail. The changelog has an entry for every release from 0.12.0 on; earlier releases were developed before the repository was public and have none.

| Release | What it asks | Where |
|---|---|---|
| 0.12.0 | Migrations `0020` to `0032`. Before you upgrade, check that an administrator can sign in. A grant of `MANAGE_GRANT`, `MANAGE_GROUP` or `MANAGE_TOKEN` as a single action no longer has an effect: only the `ADMIN` role does. A project whose `uv.lock` names a package index that is not registered, or a git or URL dependency, stops at its next run: an administrator registers the index, and the project declares it with `explicit = true`, is locked and deployed again. The worker ignores a project's `[tool.uv]` settings. Every query runs in a process of its own: a query past its time limit is ended, a query can be cancelled and can end as `CANCELLED`, and a worker over its memory limit ends its largest query or Python task before it stops itself. If you search the server's or the worker's log: a record of several lines now has `    | ` at the start of its later lines, control characters are written as escapes (a carriage return as `\x0d`), and uvicorn's level names have no colours. A query or a deploy that an earlier release left `RUNNING` for ever is ended within about a minute and shows `FAILED` with the cause `worker_lost` (a deploy shows `failed`); nothing is run again by itself. Stop every old worker before you migrate and start the new ones after: a worker of the earlier release does not register, so the new scheduler ends its queries and deploys after a lease. `lakana query list` and `lakana worker list` are new. With three or more workers that take ad-hoc queries, one user can have at most all but one of them busy at once (three workers: two queries running at once; four: three); the Compose bundle's default of two workers is not affected. Data quality checks are new, and a check can keep the data that failed it (`on_failure: keep`); a project can set the default of its checks in `lakana.yaml`: a project without a `checks/` directory runs unchanged, one with checks needs every worker upgraded with the server, and a commit through the Iceberg REST catalog that would leave a table with no current snapshot is now refused. `lakana check rows` shows the rows that a check found, and `lakana table maintain` removes the failed snapshot of a check after seven days. | [Notes for 0.12.0](#notes-for-0120), [Data quality checks](#data-quality-checks), [Workers for one kind of work, and a limit per user](#workers-for-one-kind-of-work-and-a-limit-per-user), [Use a private package index](../how-to/private-package-index.md), [Queries run in a process of their own](#queries-run-in-a-process-of-their-own), [Queries and deploys cannot get stuck](#queries-and-deploys-cannot-get-stuck), [Control characters in the log](../how-to/deploy-with-compose.md#the-sweep) |
| 0.11.0 | Migrations `0018` and `0019`; set every secret you created again; every worker upgraded first; lock Python projects again; new limits on request bodies and SQL text; workers stop at 4 GB. | [Upgrade to the release with artifacts](../how-to/upgrade-to-artifacts.md) |
| 0.10.0 | A package index URL with a query string is refused; move a `uv.toml` into `pyproject.toml`; lineage and failure causes changed. | This row |
| 0.9.0 | Upgrade every worker before you deploy Python assets; rewrite `asset(deps=...)` as `asset(inputs=...)`; move helper modules out of `assets/`; an older bundle needs three additions. | [Upgrading](../how-to/deploy-with-compose.md#upgrading), [Projects and deploys](../how-to/projects.md) |

## Notes for the next release

### Errors under a row filter or column mask

No migration. A query that reads a table under a row filter or column mask no longer shows the
database's text for a failure, except binder, catalog and parser errors: the run's error, and a check's error, hold one fixed message that names
the table and does not show the value. Other errors, and errors on a table with no filter or mask, keep their text.

### Columns can be qualified with the table's name

No migration. `SELECT orders.id FROM gold.orders`, `gold.orders.id` and `o.id` after
`FROM gold.orders AS o` now work, also under a row filter or column mask; before, they failed with a
message about a read called `__lakana_...`. The table's own name serves as the alias only when no
other table, alias, CTE, derived table or view of the query has that name. The full name
(`gold.orders.id`) works in the level of the query that reads the table; inside a subquery, a CTE,
a derived table or a `LATERAL` join write `orders.id` or give the table an alias. When two
tables share a name (`gold.orders JOIN silver.orders`, or a subquery over `silver.orders`
inside a query over `gold.orders`), nothing changes: the query runs as before, and a column qualified with that name
fails in the database as before. Give each table an alias.

### A query that changes data is refused wherever the statement sits

No migration. A query, an asset, a check or a view whose SQL holds `INSERT`, `UPDATE`,
`DELETE`, `MERGE`, `COPY`, `CREATE`, `DROP`, `ATTACH`, `SET` or another statement that is not a
read is refused also when the statement sits inside a CTE or a subquery:
``A query reads data and cannot change it: `DELETE` is not allowed, also not inside a CTE or
subquery.`` DuckDB 1.5 rejects these shapes itself; the next major DuckDB release runs them, so
Lakana refuses them first. A query may not call `getvariable` either.

`DESCRIBE`, `SUMMARIZE`, `SHOW` and `SELECT ... INTO` are refused in a query too, also inside
a subquery or a CTE (`SELECT * FROM (SUMMARIZE t)` no longer works). `DESCRIBE`, `SUMMARIZE`
and `SHOW` were never allowed as the whole statement; `SELECT ... INTO` is now refused there
too. To get the numbers, use an aggregate query (`SELECT count(*), min(x), max(x) FROM t`) and
read the column list from the result.

### Iceberg tables are read by field id

Migration `0033` runs automatically (see below). Lakana reads the data files of an Iceberg table by the field id of each column, as
the Iceberg specification requires. After a schema change, an added column reads as NULL in older
rows, a renamed column keeps its data, a column dropped and added again under its name is a new
column (a type change through `evolve_schema` is one too: its older rows read as NULL), and a
widened column reads in its new type. Queries, row filters and column masks that
failed after such a change (``The data files of `TABLE` do not all have the column ...``) now
run; that message remains only for raw-zone (external) tables. A field inside a struct, list or
map column is read by name: after one was renamed, or dropped and added again, reads of the
table are refused with ``'TABLE' cannot be read: the field ...`` until its rows are written
again. A table with a column of a type Lakana cannot read yet (nanosecond timestamps, `unknown`, the
geospatial types of format version 3) is refused as a whole: ``'TABLE' has the column 'x' of type
..., which Lakana cannot read yet.`` Store the column as another type. A filter on a float column
widened to double still needs a range
([Show some people only some rows](../how-to/filter-rows.md#columns-of-a-table-made-outside-lakana)).

Row filters and column masks are bound to a column name, while a table is read by field id, so a
rename could leave a policy on another column. From this release a read is refused, and nothing is
shown, when a column was renamed or replaced (dropped and added again, also by a type change
through `evolve_schema`) since the policy was created, and the old or the new name is one the
policy uses:
`` `TABLE` has a row filter or column mask that uses column `COLUMN`, and a column was renamed or replaced since the policy was created ...``
Each policy now records the table schema it was created against. A policy of an earlier release
records none and is judged against every schema the table has had, so after the upgrade a table whose
policy column was renamed or replaced at any time is refused until an administrator creates the
policy again for the column it is meant for and revokes the old one. Before you upgrade, list the
row filters and column masks (the `list` subcommand of `lakana row-filter` and of
`lakana column-mask`) and look for tables whose columns were renamed or retyped since the policy
was made. The order after a rename is: rename, create the policy for the new name, revoke the old
one. Reads are refused until the old one is revoked, and nothing shows in clear at any step
([Mask a column](../how-to/mask-a-column.md#after-a-rename),
[Show some people only some rows](../how-to/filter-rows.md#after-a-rename)). Migration `0033` adds
a column `schema_id` to the tables of row filters and column masks.

From this release on, a commit through the Iceberg REST catalog that adds a data file without
field ids, for example one registered with pyiceberg's `add_files`, is refused and nothing is
applied ([the refusal](../reference/checks.md#when-a-commit-through-the-iceberg-rest-catalog-is-refused)).
Write the files with a client that sets field ids: pyiceberg's `append` and `overwrite` do, and
Lakana's own writes always do. A commit may add at most 2,000 data files, and the check has 60
seconds; split a larger load into several commits. Files that other Iceberg clients committed
before this release are not checked. If such a file lacks field ids, every column of it reads
as NULL, with no error. Ask the owner of the table to write its rows again, with Lakana
(`replace`) or with a client that writes field ids. Field ids that match no column of the table
read as NULL as well.

## Notes for 0.12.0

This release changed a lot: who may hand out access, which package sources a project may
use, how a query runs, what happens to a query or a deploy whose worker is gone, which work a
worker takes and how many ad-hoc queries one user runs at once, how snapshots are expired, and
that a project can state in SQL what must be true of its tables. Read every part below. Run the
migrations as always: `0020` is new, it creates the table of registered
package indexes and widens the `action` column of the grants table; `0021` adds one column to the
table of query runs; `0022` adds the table of workers, a column to the query runs and to the
deploy runs, and two indexes; `0023` adds one column to the table of workers; `0024` adds the table of check results and
a few columns to the tables, query runs, task runs and deploy runs; `0025` adds three columns to the table of tables
(the mark of a table that keeps data after a failed check); `0026` adds one column to the table of query runs
(the check result whose rows a query shows).

### Snapshots are expired with `lakana table maintain`

**This is a breaking change for Iceberg clients.** A commit through the Iceberg REST catalog can no
longer remove snapshots, remove schemas or partition specs, or remove or set a ref named
`lakana-check-failed-*`. It is refused with 403 for every caller, an administrator included, and a
PyIceberg job, or one from another engine, that expires snapshots now gets that 403. It also cannot assign another UUID
to a table (422). Expiring snapshots was possible for anyone who could write a table, and it could take the
snapshot that a running task or a kept tag still needs. If a job from another engine or a PyIceberg script
expires snapshots on a Lakana table, change it to run `lakana table maintain` instead (see
[`lakana table maintain`](../reference/checks.md#lakana-table-maintain)). Removing a branch or a
tag of your own is unchanged.

Two more changes of the same kind. A caller without `READ_TABLE` can no longer make an earlier
schema, partition spec or sort order of a table current; it can still add a new one and make that
one current. **A task token that no task owns (one made by hand over REST) can no longer run
`lakana check rows`**, and it is refused the read of a table kept after a failed check, like a
pipeline task: read with your own token. See [When a commit through the Iceberg REST
catalog is refused](../reference/checks.md#when-a-commit-through-the-iceberg-rest-catalog-is-refused).

### A deployed snapshot is a project version

<!-- old-name -->

What a deploy records for one commit in one environment is now called a *project version*. It was
called a job version. The word *job* is now free for a named entry in `lakana.yaml` that you can
start by hand, through the API or on a schedule. Nothing is migrated, and the database tables keep
their names. The old names are gone with no alias, so change these where you use them:

| Where | Before | Now |
|---|---|---|
| Command line | `lakana job-version show ID` | `lakana project-version show ID` |
| REST route | `GET /api/v1/environments/{env}/job-versions/{id}` | `GET /api/v1/environments/{env}/project-versions/{id}` |
| Python SDK | `client.job_versions.get(...)`, which returns `JobVersion` | `client.project_versions.get(...)`, which returns `ProjectVersion` |
| JSON field | `job_version_id` | `project_version_id` |
| JSON field | `rollback_to_job_version_id` | `rollback_to_project_version_id` |

The JSON fields are in the answers of `deploys`, `deployments`, runs and artifact versions, and in
the SDK objects built from them. The `detail` of the audit events `RUN_STARTED`, `DEPLOYED`,
`DEPLOY_DRY_RUN`, `ROLLED_BACK`, `UNDEPLOYED` and `CODE_INTEGRITY_FAILED` uses
`project_version_id` for events written from this release on. Events written earlier keep the old
key (`job_version_id`), so a script that reads both has to accept both.

<!-- /old-name -->

### Jobs and the triggers that start them

A project can declare **jobs** in `lakana.yaml` and give them a cron schedule in a time zone; a
job also starts by hand or through the API. Nothing is required of a project that declares none.
See [Run a job on a schedule](../how-to/run-a-job-on-a-schedule.md).

- **Migration `0028`** adds columns to `runs` and to `deploy_runs` and two tables, `job_schedules`
  and `job_fires`. Run the migrations as always. A project version that declares jobs has manifest
  version 5. The packages now depend on `tzdata`, the one source of time zone rules, so a slim
  container needs no system time zone database.
- **A schedule needs a run principal.** After the first deploy that declares a schedule, bind a
  service account with `lakana deployments set-run-as`, or the job shows `no run principal` and
  does not run. The deploy warns about it.
- **A project with jobs needs workers of this release.** Upgrade every worker before the first
  deploy that declares `jobs:`. A worker of the earlier release refuses the runs of such a project
  version (manifest version 5), and while a worker of the earlier release leads the scheduler it
  neither fires schedules nor starts waiting starts, although the new server accepts the start
  and answers `202`.
- **New REST routes**, all under `/api/v1/environments/{env}`:
  `GET /jobs`, `POST /jobs/{job}/runs` (201 with the run, 202 with the waiting start),
  `POST /jobs/{job}/pause` and `POST /jobs/{job}/resume`. Starting needs `RUN`; pausing and
  resuming need `DEPLOY` in that environment; the list shows every job of the environment to
  anyone who may read it (`READ_ENVIRONMENT`).
- **New fields of a run** (`runs list|show`, `GET /runs`): `job` (null for an ad-hoc `lakana
  run`), `trigger` (`schedule`, `api` or `manual`; null for an ad-hoc run), `scheduled_for` and
  `missed`. Runs written before the upgrade have null, null, null and 0.
- **New audit events:** `JOB_PAUSED`, `JOB_RESUMED`, `JOB_START_WAITING` (a start parked behind
  the active run) and `JOB_START_DROPPED` (a start that could not begin; its `reason` says why).
  `RUN_STARTED` gains the fields `job`, `trigger`, `scheduled_for`, `missed` and `merged` in its
  `detail`. Events written earlier do not have them.
- **New deploy output:** `Jobs:` in the diff of a deploy and a dry run (`jobs_added`,
  `jobs_removed`, `jobs_changed` in `diff` of `--output json`) and the deploy warnings of
  [the how-to](../how-to/run-a-job-on-a-schedule.md#the-warnings-of-a-deploy).
- **New command:** `lakana job list|run|pause|resume`.

### Notifications for jobs

A job can name a webhook in `lakana.yaml` (`notify:`) and get a message when a run fails and when
the job works again. Nothing is required of a project that declares none. See
[Get told when a job fails](../how-to/get-told-when-a-job-fails.md).

- **Migration `0029`** adds the table `notification_deliveries`, the queue and the record of the
  messages. Run the migrations as always.
- **A project with `notify:` needs workers of this release.** Upgrade every worker before the
  first deploy that declares `notify:`. A project version in which a job has `notify:` has manifest
  version 6, and a worker of the earlier release refuses its runs.
- **The worker depends on `httpx`** (a new dependency, permissively licensed), which sends the
  messages. A container image has it; a worker installed with `pip` gets it with the package.
- **New settings, on the worker:** `LAKANA_WEBHOOK__ALLOW_HOSTS`, `LAKANA_WEBHOOK__PROXY` and
  `LAKANA_WEBHOOK__CA_BUNDLE`, and `LAKANA_WORKER__NOTIFICATIONS` (on by default). A worker without
  a usable `LAKANA_SECRETS__MASTER_KEY` sends no notifications. See
  [Webhook network settings](../how-to/projects.md#webhook-network-settings).
- **Without a setting, the worker sends only to a public `https` address on port 443.** If your
  chat server is inside your network, an operator has to allow its name.
- **New audit events:** `NOTIFICATION_FAILED` (a delivery gave up; its `detail` has the run, the
  job, the event and the reason) and `NOTIFICATION_TESTED` (someone ran `lakana job notify-test`).
- **New REST routes**, under `/api/v1/environments/{env}`: `POST /jobs/{job}/notify-test` (202;
  needs `DEPLOY`) and `GET /notifications/{id}` (needs `DEPLOY`, `RUN` or `READ_ENVIRONMENT` in
  the environment). Whoever may read a run sees its notifications in the run itself.
- **New field of a run** (`runs show`, `GET /runs/{id}`): `notifications`, a list with the delivery
  id, the event, the state, the attempts and a short result. It is empty for runs written before
  the upgrade.
- **New deploy warning:** a job that notifies through a secret that does not exist in the
  environment, or is revoked, still deploys; the warning names the job and the secret (see
  [the warnings of a deploy](../how-to/run-a-job-on-a-schedule.md#the-warnings-of-a-deploy)).
- **`notify` is part of a job's entry in the diff:** a deploy that adds, changes or removes a
  job's `notify:` lists the job under `jobs_changed` (`Jobs:` in the output of a deploy and a dry
  run).
- **An unreadable `LAKANA_WEBHOOK__CA_BUNDLE` stops the worker at start**, like the other CA
  bundles; fix the path before you start it.
- **`LAKANA_WEBHOOK__*` must be the same on every worker that delivers.** A worker without the
  allow-list refuses an internal address for good, so a delivery it claims first fails for that
  reason.
- **New commands:** `lakana job notify-test NAME` and `lakana job notification ID`.

### A cancel or a memory stop after a write sets the table back

A worker that stops itself for memory, or sees you cancel the run, after a task with checks wrote
its table no longer ends the task with the unchecked rows in the table. It hands the task back
(the wait reason `setting_back`), the next free worker sets the table back, and the task then
ends with the message of the stop or `The run was cancelled.` followed by what became of the
table. Before, only a worker that was lost got this. A run you cancel now ends only once a
pipeline worker has done it. See [A worker that is lost](../how-to/check-your-data.md#a-worker-that-is-lost).

### A task that ends because its run was cancelled says so in the run's words

A task that ends because you cancelled its run now reads `The run was cancelled.`, where it read
`The query was cancelled.` (the text for a query) or nothing at all, whether its worker stopped it,
the scheduler cancelled it while it waited, or a worker set its table back. A run that the
platform stopped because another task failed its code integrity check reads `The run was stopped
because another task failed its code integrity check.` instead, because nobody cancelled it. The
sentence about the table (`The table was set back to before this task ran.`) follows either text.
A Python task that is stopped this way reads the same, where it read `stopped: cancelled`. A
script that matches the old text on a task's message needs the new one. A query that you cancel
outside a run keeps `The query was cancelled.`

### An Iceberg commit cannot add a partition spec or sort order under a taken id

A commit through the Iceberg REST catalog that adds a partition spec or a sort order under an id
the table already has is now refused (422) for every caller, as a schema under a taken id already
was. Before, one whose content was identical went through. The refusal text says to load the table
again and repeat the change; loading needs `READ_TABLE`. A client that evolves the spec the normal
way (PyIceberg, for example) picks a new id, or makes the existing spec the default without adding it,
and is not affected. The refusal of removing schemas and partition specs now says why: Lakana keeps
every schema, partition spec and sort order a table has had. See
[the messages of a commit](../reference/checks.md#sdk-and-rest).

### A table that is kept after a check that could not run

Migration `0030` adds one column to `tables`; the migrations run before the server starts, as for
every upgrade. A table that carries the mark of a failed `keep` check and is built again, while its
`keep` check cannot run because it reads a table of another project that is kept, now moves its
mark to the new run, and `lakana table get` says that it holds data no check could run on. Before,
the mark kept naming the earlier run and check. Readers stay refused until the table passes its
checks. See [the rules of the mark](../reference/checks.md#the-rules).

### A set-back puts a kept table's earlier mark back

Migration `0031` adds two columns to `tables`; the migrations run before the server starts, as for
every upgrade. A kept table now remembers the marks it carried before its current one. When the
data the mark names is set back (a later run's `keep` failure moved the mark, and then that run
was cancelled before its failure was recorded, or its checks ran again and a `block` check failed),
the table takes the earlier mark back in the same step, when the table is set back to the data that
mark was a verdict on, and `lakana table get` names the run whose data the table holds again; if that
data had passed its checks, the table has no mark and tasks may read it. Before, the mark kept
naming the run whose data was gone. A table that was already kept when you upgrade has no
remembered marks: when its data is set back it gets a mark that says its checks are not known (see
the next section), and readers stay refused until the table passes its checks; build it again to
clear it. A script that reads `TABLE_ROLLED_BACK` events sees the new `kept_restored` key on such a
set-back. See [the rules of the mark](../reference/checks.md#the-rules).

### A set-back after another run's check no longer lets readers in

Migration `0032` adds one column to `tables`; the migrations run before the server starts, as for
every upgrade. A kept table whose mark was cleared by the check of another run, and whose data was
then set back to the earlier data that had failed its checks, used to carry no mark, so readers were
let in. Now a clear keeps the mark it removed, and a set-back to data that the table's current
verdict does not describe takes back the state that data had; when there is none (the history ran
out, or the table was marked before the history existed) the table gets a mark that reads `Set back
to data whose checks are not known`. Tasks are refused it like any mark, and the next passing checks
clear it: build the table again in the project that builds it. A script that reads
`TABLE_ROLLED_BACK` events sees `kept_unknown: true` on such a set-back, and `kept_not_restored` only
on a first build that kept its mark. See [the rules of the mark](../reference/checks.md#the-rules).

### A row filter is checked when it is created

No migration. Creating a row filter now refuses a column written with a qualifier
(`read_parquet.region`, `t.region`: write `region`) and a column the table does not have, as
creating a column mask already refused a column the table lacks. Names are compared without
regard to letter case, and a column written as a struct field (`address.country`) is refused with a
message of its own. Creating a column mask now refuses a qualified column in its expression the
same way. To read the table's columns, creating a filter that reads a column now needs
`READ_TABLE` on the table as well as `MANAGE_POLICY` (the `ADMIN` role has both). Create the table
with its columns first (`lakana table create NAMESPACE.TABLE --column NAME:TYPE`), then its filters and masks, then
write data or grant `READ_TABLE`: a table created with columns has a schema before any data, so
the filter can be created at once, and no row is readable unfiltered in between. A filter that
reads no column (`has_group('staff')`) is not affected. Filters and masks stored before the upgrade
are not looked at again: they keep applying as they did, and one that names a column the table no longer has still makes
every read of the table by its person fail instead of showing unfiltered rows. See
[Show some people only some rows](../how-to/filter-rows.md).

### Clearer messages when a read under a policy fails

No migration. Three raw database errors now read as Lakana's own sentences, and nothing is
newly refused or newly allowed:

1. **A table that gained a column leaves older data files without it.** Every read under a row
   filter or column mask on that table selects every column, so every such reader fails, whichever
   column the policy names. The message now says so ("The data files of `TABLE` do not all have
   the column `COLUMN` ...") and no longer lists the columns of one of the files, as the database
   does. It failed before too, never showing rows without the filter. A reader whose own SQL
   names the new column (`SELECT tier FROM ...`, or a subquery or view that leaves it out) gets
   the database's own line, without its list of candidates, and a hint that names the table and
   says its older files lack the column, the same whatever order the files are in.
2. **`lakana check rows`**: a check whose `SELECT` names a withheld column in another letter case
   or through a table alias (`o.note`) gets the sentence about the withheld column, as it did for
   the exact spelling; and a read that fails because maintenance removed the snapshot while the
   query was already reading it says the snapshot is gone. The query is asked again after a
   failure, and only an answer that the snapshot is gone changes the message.
3. A script that matched the database's text (`Referenced column ... not found`,
   `schema mismatch in glob`) on such a failure sees the new text instead.

See [Check your data](../how-to/check-your-data.md#look-at-the-rows-that-failed).

### The image runs Python 3.14

The Lakana image (`ghcr.io/chrschy/lakana`, used by the Compose bundle) moved from Python 3.13
to Python 3.14. A worker deploys a project with Python assets only when the project's
`requires-python` admits the worker's Python, so a project locked for 3.13 alone
(`requires-python = "==3.13.*"`, or `<3.14`) is refused with `uv.lock: requires-python '…' does
not admit the worker's Python 3.14.…`. Widen `requires-python` (the template of
`lakana project init` writes `>=3.11`), run `uv lock`, commit, and deploy again. Local mode
(`lakana serve`) runs on your own Python and is not affected.

### Delegation needs the ADMIN role

From this release only a principal with the `ADMIN` role on the instance can hand out access,
change groups and issue or revoke tokens. The three permissions behind this (`MANAGE_GRANT`,
`MANAGE_GROUP`, `MANAGE_TOKEN`) no longer work as single-action grants.

1. **Before you upgrade, make sure an administrator can sign in.** Run these commands as an
   administrator (the bundle's first administrator is `admin`, with the credential in the `team`
   profile). `lakana whoami` prints who your credential is. The JSON of `grant list` has one
   entry per grant: an administrator is a grant with `"role": "ADMIN"` and `"kind": "INSTANCE"`,
   for the principal in `principal_id` or, for a group, in `group_id`. The table of `grant list`
   shows these ids as `principal <id>` or `group <id>`, and the commands print no id next to a
   name, so to read it by name list the principals and groups, then ask for the grants of each:

<!-- source: docs/examples/getting-started/run.sh#delegation-check -->
```bash
lakana whoami
lakana principal list
lakana --output json grant list
lakana group list
```

<!-- not tested: needs the name of a principal and a group of your instance -->
```bash
lakana grant list --principal NAME
lakana grant list --group GROUP
lakana group members GROUP
lakana token list NAME
```

   The members of a group with the `ADMIN` role are administrators too. The last command shows a
   principal's tokens: the key, the name and when it was last used. It does not show
   whether a token has expired or was revoked, and lists revoked ones too. The sure test is to use
   the token: the administrator signs in with it (`lakana login`) and runs `lakana whoami` and
   `lakana grant list`. If both print their table, the token works; if the server rejects the
   credential, it does not. Do this for at least one administrator before you upgrade.
2. **What stops working.** A principal or group that holds `MANAGE_GRANT`, `MANAGE_GROUP` or
   `MANAGE_TOKEN` as a single action can no longer grant access, manage groups or issue tokens,
   and a new grant of one of them on its own is refused. A grant of one of them that exists
   already is kept and does nothing. The server names up to twenty of them in a warning in its
   log when it starts (the line begins `N grant(s) of MANAGE_GRANT, MANAGE_GROUP or
   MANAGE_TOKEN as a single action have no effect`), and `lakana grant list` shows `no effect`
   in a `note` column for each, with a line under the table that says what it means and, for the
   first five, a note (the action and who holds the grant) followed by the command that revokes
   it with its whole id, alone on its own line so that you can copy it as it is (`--output json`
   has every grant and the full note, `no effect: <ACTION> needs the ADMIN role`). With
   `LAKANA_LOG__LEVEL=ERROR` the warning is not printed; the listing marks the grants in every case.
3. **What to do with them.** Give the `ADMIN` role to whoever should keep the ability, or let an
   administrator do those tasks, and revoke the dead grants. `ID` is the grant's id, from the
   lines under the table of `grant list` or from its JSON; for a group, use `--group` instead of
   `--principal`:

<!-- not tested: needs the name of a principal and the id of a grant of your instance -->
```bash
lakana grant create --kind INSTANCE --principal NAME --role ADMIN
lakana grant revoke ID
```

4. **The rule is not retroactive.** What a holder of one of the three created before the upgrade
   stays: grants, group memberships and tokens. To review them, read the audit log, one command
   per event type (the option takes one value). In the JSON, `principal_id` is the principal that
   acted and `detail` names the target (who was given the grant, which group and member, which
   token's owner):

<!-- source: docs/examples/getting-started/run.sh#delegation-audit -->
```bash
lakana --output json audit list --event-type GRANT_CREATED --all
lakana --output json audit list --event-type GROUP_MEMBER_ADDED --all
lakana --output json audit list --event-type PAT_ISSUED --all
```

   To look at one holder only, add `--principal-id ID` with the `principal_id` that the JSON of
   `grant list` shows for that holder's grant.
5. **If no administrator can sign in any more**, there is a way back that needs access to the
   database. It is what the current code requires and nothing more. At every start the server
   restores its first administrator (the principal named by `LAKANA_BOOTSTRAP_ADMIN`, `admin`
   unless you set it for the server; the Compose bundle does not) when no active principal holds
   the `ADMIN` role at `INSTANCE` scope, and no API call can leave an instance without one. So
   the way back revokes that role from **every** principal and then restarts the server.

   **The consequence:** after it, only the restored administrator holds the `ADMIN` role, and it
   has to give the role to the others again. Take a backup first (see
   [Backups](../how-to/deploy-with-compose.md#backups)). With the Compose bundle, in the folder of
   the bundle, first write down who holds the role now:

<!-- not tested: reads the database of a real instance -->
```bash
docker compose exec postgres psql -U lakana lakana -c \
  "SELECT id, principal_id, group_id FROM grants WHERE role = 'ADMIN' AND kind = 'INSTANCE' AND revoked_at IS NULL"
```

   Then revoke them and restart the server. **Run the statement once.** Run again after the
   restart it would revoke the restored administrator's role too:

<!-- not tested: changes a real database, and needs an instance whose administrators are locked out -->
```bash
docker compose exec postgres psql -U lakana lakana -c \
  "UPDATE grants SET revoked_at = now() WHERE role = 'ADMIN' AND kind = 'INSTANCE' AND revoked_at IS NULL"
docker compose restart server
docker compose logs server
```

   The server creates the restored principal, or reactivates it, gives it the `ADMIN` role,
   revokes its earlier tokens and issues one new token. Its log then has one of two lines, both
   at the level WARNING (the name is the one you configured):

   - `Minted a bootstrap PAT for 'admin'`, followed by the new token. The server prints it this
     once. This is the line of an instance that was set up with the bundle.
   - `Restored the bootstrap PAT for 'admin' using the configured key id`, without a token. The
     server prints this when the database never held a token with the key id in
     `LAKANA_BOOTSTRAP_PAT_KEY_ID`. The token that works is then the one made of the two values
     in `.env`: `lakana_pat_<LAKANA_BOOTSTRAP_PAT_KEY_ID>_<LAKANA_BOOTSTRAP_PAT_SECRET>`.

   Before the restart, make sure the server's `LAKANA_LOG__LEVEL` is not `ERROR`: at that level
   neither line is printed, and a token the server made up is lost.

   Sign in with the token, as in
   [Revoking the bootstrap credential](../how-to/deploy-with-compose.md#revoking-the-bootstrap-credential),
   and give the `ADMIN` role back to the others.

   If you missed the line, read the server's log again with the last command above: the line is
   still there as long as the server's container was not created anew. A second restart does
   **not** issue another token, because an administrator exists again. If the line is gone, or
   was never printed, run the `UPDATE` once more and restart the server: it restores the
   principal again, with another new token. Deactivating the principals instead of revoking
   their grants is not described here.

### Package sources are checked

Before this release no package source in a project's `uv.lock` was checked. Now a lock may name
only these sources: the public index (PyPI), the folder `wheelhouse` of the project, the
operator's extra index (`LAKANA_RUN__INDEX_URL`) and the package indexes an administrator
registered. See [Use a private package index](../how-to/private-package-index.md).

1. **A deployed project whose lock names any other index stops at its next execution**, and a new
   deploy of it is refused. A dependency from git, from a single file URL or from another local
   folder is refused the same way. The task fails with a message that begins `uv.lock: takes …
   from the package index …, which is not registered on this instance` and, for a project deployed
   before the upgrade, contains the sentence `The package indexes registered on this instance no
   longer match this project version`.
2. **What to do.** Two steps, both described on the page
   [Use a private package index](../how-to/private-package-index.md). First an administrator
   registers the index with `lakana index add` (the credential is optional). Registering alone
   is enough only for a project whose `pyproject.toml` already declares the index as a
   `[[tool.uv.index]]` table with `explicit = true`: its next run goes on. A project that
   declares the index as a plain `[[tool.uv.index]]` table, with `default = true`, or through
   `index-url` or `extra-index-url` then fails with a message that begins `pyproject.toml: the
   index …` and goes on `is a registered package index and needs`, or, for the two settings,
   one that names the setting and goes on `names the registered package index`. Such a project
   declares the index as a
   `[[tool.uv.index]]` table with `explicit = true`, names the packages that come from it under
   `[tool.uv.sources]`, and is locked (`uv lock`) and deployed again. An index that serves
   **every** package, such as a mirror of PyPI, does not belong in the registry: the operator
   sets it as `LAKANA_RUN__INDEX_URL` for the worker, and a lock may then name it without
   `explicit`. A git or URL dependency has to move to an index: publish it there, register the
   index and run `uv lock` again.
3. **The worker builds with `uv sync --frozen --no-config`.** It reads the lock and the index
   definitions in `[[tool.uv.index]]`, and ignores a project's `[tool.uv]` settings and any
   `uv.toml`, a system-level one on the worker's host included. For example these stop applying
   on the worker: `no-build`, `no-binary-package`, `required-version`, `compile-bytecode`, and
   proxy and TLS settings (the operator's `LAKANA_RUN__HTTPS_PROXY` and `LAKANA_RUN__CA_BUNDLE`
   still apply). They still shape the lock when you run `uv lock` on your machine. A `uv.toml` at
   the root of a project is refused at deploy, as before.
4. **A project that uses only PyPI and `wheelhouse` needs nothing,** unless it relied on one of
   the settings of item 3 on the worker: then that setting no longer applies there.

### Queries run in a process of their own

The worker no longer runs a query inside its own process. It starts a process for each query, and
it can end that process. That gives the time limit, a cancel and the memory limit something to act
on. How to use it: [Stop a query](../how-to/stop-a-query.md).

1. **Run the migrations; `0021` adds one column to `query_runs`.** The column keeps a default, so
   an old server or worker that still writes a query run while you upgrade goes on working: you do
   not have to stop them first. Going back: `0021`'s downgrade turns every `CANCELLED` run into a
   `FAILED` one with the cause `cancelled`.
2. **A query run has a fifth status, `CANCELLED`, which is final.** A program that waits for a
   query must treat it as an end. The SDK and the CLI of this release do; an older `lakana`
   package keeps polling a cancelled query until its own timeout, so upgrade the clients with the
   server.
3. **Each query costs about a tenth of a second more than before** (80 ms measured for
   `SELECT 42`): the worker starts a process for it.
4. **The time limit is hard.** A query that used to run on after
   `LAKANA_QUERY_LIMITS__TIMEOUT_SECONDS` now ends, at the latest
   `LAKANA_QUERY_LIMITS__KILL_GRACE_SECONDS` (10, a new setting) later, with the cause `timeout`.
5. **A worker over its memory limit ends its largest consumer first** on Linux, a running query
   or a running Python task, whichever uses more memory. A query fails with the cause
   `out_of_memory` (with a new message); a task's attempt fails as transient with a new message
   and is retried within its attempts. The worker stops itself only if that was not enough or
   nothing could be ended (a task still building its environment cannot). Nothing to set. On
   macOS the worker stops as before.
6. **A worker that shuts down gives a running ad-hoc query**
   `LAKANA_RUN__DRAIN_GRACE_SECONDS` (20) and then ends it; before, it waited for the query however
   long it took. The Compose bundle's `stop_grace_period` of 45 s covers both graces.
7. **At start-up the worker starts one query process as a check.** A worker whose environment
   cannot start it stops with a message that names the cause.
8. **Limits that stay.** DuckDB's own memory setting is still a target, not a bound. On macOS and Windows the process
   handling has the limits it has for Python tasks. On macOS Lakana needs Python 3.13 or
   newer: with an older one `lakana server start --local` stops before it starts anything, and a
   worker started by hand stops at start, each with a message that says so. A query may read about 120,000 data files: the
   request the query process gets is limited to 64 MiB, and a query over it is refused before it
   starts, with a message that says to write the table again with `replace` so that its data lands in
   fewer, larger files.
9. **Ctrl-C in `lakana sql` and `lakana query wait` now cancels the query.** Before, the query ran
   on. For `lakana sql --into`, a Ctrl-C now stops the write: the table keeps the snapshot it
   had.
10. **Two audit entries are new.** A cancel request that the server accepts is audited as
    `QUERY_CANCELLED`. A cancel that is refused (the query is somebody else's, or does not exist)
    is recorded as `AUTHORIZATION_DENIED` with the reason `not_submitter`.

### Queries and deploys cannot get stuck

Before this release a query or a deploy whose worker was killed stayed `RUNNING` for ever, and a
cancel of it did nothing. Now every worker registers and writes a heartbeat to the database, and
the platform ends what a lost worker held. How it looks to a user:
[A worker that is lost](../how-to/stop-a-query.md#a-worker-that-is-lost). Why a query waits:
[Why is my query or task waiting?](../how-to/why-is-it-waiting.md).

1. **Run the migrations; `0022` adds the table `workers`, a column `worker_id` on `query_runs` and
   on `deploy_runs`, and two indexes on `query_runs`.** Nothing is converted. Going back: the
   downgrade drops them, and a run that was ended with the cause `worker_lost` keeps it, which an
   earlier release shows as a cause it does not know.
2. **Stop every worker of the old release before you migrate, and start the new ones after it.** A
   worker of the earlier release does not register and records no worker id on its runs, so the
   new scheduler ends its queries and its deploys once they started `LAKANA_RUN__LEASE_SECONDS`
   (60 s) ago. Mixed versions are not supported; this is the rule "Upgrade the server and every
   worker together" of [What every upgrade asks](#what-every-upgrade-asks). The steps for a
   Compose instance are in [Upgrading](../how-to/deploy-with-compose.md#upgrading).
3. **A query or a deploy that is `RUNNING` when you upgrade has no worker recorded.** The scheduler
   ends it once it started a lease ago. Every run that an earlier release left `RUNNING` for ever
   is therefore ended within about a minute after the new workers are up: a query shows `FAILED`
   with the cause `worker_lost`, a deploy shows `failed` and activated nothing. Nothing is run
   again by itself: run the query or deploy again.
4. **From now on a lost worker's work ends after about a minute.** A worker counts as lost when its
   last heartbeat is `LAKANA_RUN__LEASE_SECONDS` (60 s) old; it writes one every
   `LAKANA_RUN__RENEW_SECONDS` (20 s). The scheduler looks every
   `LAKANA_RUN__SCHEDULER_INTERVAL_SECONDS` (1 s) and ends what the lost worker held, with the
   cause `worker_lost` and a message that says to run it again. A scheduler that just took over, or
   was away for more than half a lease, first waits `LAKANA_RUN__RENEW_SECONDS` plus 5 s so that
   the workers can beat. A cancel of a query whose worker is gone is answered at once. A write
   that had already been committed ends `SUCCEEDED`, so that running it again does not write it
   twice; it shows fewer details than a write the worker finished itself (no watermark, no schema
   difference, no lineage summary). The write of a pipeline's SQL task is treated the same way,
   and the task with it: when the worker is lost after the write was committed, the task ends
   `succeeded` and is not run again, whichever attempt it is on and also while the run is being
   cancelled (a task whose table has [checks](../how-to/check-your-data.md) is the exception, see
   [Data quality checks](#data-quality-checks)). Its message says that the worker was lost afterwards; it has no watermark or
   schema change either, and its duration runs until the platform found the commit.
5. **Three settings have to fit together.** `LAKANA_RUN__LEASE_SECONDS` must have the same value
   for the server and for every worker. `LAKANA_RUN__RENEW_SECONDS` must be below it; a worker
   writes a warning to its log at start when it is more than half of it, because one late heartbeat
   would then make the worker count as lost. `LAKANA_RUN__SCHEDULER_INTERVAL_SECONDS` is the
   interval above. There is no new setting.
6. **The REST API and the SDK have four additions.** A queued query's status, and a task of a run
   that waits, carry `waiting_for` (a `code` and a `message`; `null` when nothing is waited for),
   and a failed run can have the cause `worker_lost`. Two list endpoints are new,
   `GET /api/v1/environments/{env}/queries` and `GET /api/v1/workers`, with the SDK methods
   `client.queries.list` and `client.workers.list`. An older `lakana` package ignores
   `waiting_for` and does not know the cause: it shows the message like for any cause it does not
   know.
7. **New commands.** `lakana query list` lists the queued and running queries of an environment
   (`--all` adds those that ended in the last 24 hours), and `lakana worker list` the workers and
   whether each is alive (it needs `MANAGE_ENVIRONMENT` on the instance). `lakana sql` and
   `lakana query wait` print `Waiting: <reason>` on standard error when a query has been queued
   for 3 seconds; they print nothing about it with `--output json`. See
   [Stop a query](../how-to/stop-a-query.md#find-a-query) and
   [Why is my query or task waiting?](../how-to/why-is-it-waiting.md).
8. **Limits that stay.** On a local instance, after the
   machine slept, a running query is ended if its worker sends no heartbeat within about half a
   minute after waking: run it again. For up to a minute after a worker died, `lakana worker list`
   and the reasons for waiting still count it as alive. The minute holds when another worker can
   take over the scheduler's role. Every database connection asks PostgreSQL to probe its client
   and to drop the connection after about two thirds of `LAKANA_RUN__LEASE_SECONDS` once its machine
   no longer answers, which is expected to free the role of a worker that lost power or its
   network. Before this release nothing
   asked, and PostgreSQL could keep such a connection for hours. A PostgreSQL that refuses these
   settings is logged once at the start of the worker or server and Lakana keeps running; then the
   old limit applies. `lakana scheduler status` shows which worker holds the role and when it last reported; if that worker is gone and the role stays with it for much longer than a minute, ending its database session in PostgreSQL (`pg_terminate_backend`), or restarting PostgreSQL, frees it; restarting the other workers does not.

### Workers for one kind of work, and a limit per user

A worker can now take only ad-hoc queries, or only pipeline work, and one user can run only so
many ad-hoc queries at once. How to use it:
[Separate workers for ad-hoc queries and pipelines](../how-to/deploy-with-compose.md#separate-workers-for-ad-hoc-queries-and-pipelines)
and [Why is my query or task waiting?](../how-to/why-is-it-waiting.md#a-queued-query).

1. **Run the migrations; `0023` adds the column `takes` to the table of workers.** Nothing is
   converted: an existing worker reads `all`, and takes every kind of work, as before.
2. **With one or two workers nothing changes.** That includes the Compose bundle's default of
   two. With **three or more workers that take ad-hoc queries**, one user or service account runs
   at most all but one of them at once by default: with three workers that take ad-hoc queries, one
   user can have two queries running at once; with four, three. A further query of
   that user waits and says `user_limit` while a worker is free; it starts when one of theirs
   ends. A script or a BI tool that runs queries in parallel under one account is such a user.
   To get the earlier behaviour back, set `LAKANA_QUERY_LIMITS__RUNNING_PER_PRINCIPAL` to the
   number of workers, or to any number of 1 or more of your own. Set the same value on the server
   and on every worker.
3. **Two new settings.** `LAKANA_WORKER__TAKES` is `all` (the default), `interactive` or
   `pipeline`. `LAKANA_QUERY_LIMITS__RUNNING_PER_PRINCIPAL` is `auto` (the default) or a whole
   number of 1 or more.
4. **The Compose bundle has two optional services**, `worker-interactive` and `worker-pipeline`,
   started by `LAKANA_WORKERS_INTERACTIVE` and `LAKANA_WORKERS_PIPELINE` in `.env` (both `0` by
   default). A bundle generated by an earlier release does not have them: generate a new one with
   `lakana bundle init` into an empty folder and copy the two services, or edit yours. The steps
   are in [Upgrading](../how-to/deploy-with-compose.md#upgrading). The bundle now needs Docker
   Compose 2.24 or later (it was 2.23): a service with no replica makes `docker compose up --wait`
   fail on 2.23.
5. **Upgrade the server and every worker together,** as
   [What every upgrade asks](#what-every-upgrade-asks) says. A worker of the earlier release
   does not register (see the note above): `lakana worker list` does not show it and the
   automatic limit does not count it. Stop every old worker before you migrate.
6. **The REST API, the SDK and the CLI gain** the field `takes` on a worker (`GET
   /api/v1/workers`, `client.workers.list`, `lakana worker list`) and two wait reasons,
   `no_worker_of_kind` (with `other_workers`) and `user_limit` (with `running` and `limit`). An
   older `lakana` package does not know the two codes and shows their sentence.
7. **Limits that stay.**
   - A queued deploy shows no reason why it waits, also when no worker takes pipeline work
     ([issue 92](https://github.com/chrschy/lakana/issues/92)). Look at the `takes` column of
     `lakana worker list`.
   - The limit per user is exact on PostgreSQL's default isolation level, which Lakana does not
     change.
   - Each worker applies its own value of the limit. Give the server and every worker the same
     one.
   - A worker still runs one query at a time. Several queries per worker are not part of this
     release ([issue 81](https://github.com/chrschy/lakana/issues/81)).

### Data quality checks

A project can now keep checks under `checks/`: [Check your data](../how-to/check-your-data.md).
What it asks of you:

1. **Run the migrations; `0024`, `0025` and `0026` are the three.** `lakana-server migrate` applies them as
   always, the Compose bundle's `migrate` service first, and a local instance when it starts.
   Every new column is empty or has a default, so an existing run, task or table reads as "no
   checks", and a table reads as one that keeps no data after a failed check (`0025`, see
   [Keep the data of a failed check](../how-to/check-your-data.md#block-keep-or-warn)). `0026` adds
   an empty column to the query runs; a query that was run before reads as showing no check's rows.
2. **A project without a `checks/` directory runs unchanged.** `lakana init` now writes one example
   check next to its example asset. If you delete the example asset, delete the check too, or the
   deploy is refused (see [Checks](../reference/checks.md#what-a-deploy-refuses)).
3. **A project with checks needs workers of this release.** Its project version has the manifest
   version 4. A worker of the earlier release refuses it with its message that says to update the
   worker, so upgrade the workers with the server, as every upgrade asks. A worker of the earlier
   release does not know `lakana check rows` either and would answer such a query from the table
   as it is now, so upgrade every worker before anyone uses the command.
   **The server too, before the first deploy of a project that has a `checks/` directory.** A run
   that a server of the earlier release starts for such a project cannot be checked: that server
   does not know checks. A worker of this release does not build such a table. Its task fails
   before anything is written, with "This run was started by a server that does not know checks,
   but the project has checks for `silver.orders`. Upgrade the server to the version of the
   workers, then run again." Upgrade the server, then run again.
4. **A task whose table has checks does not count as done when its worker is lost after the
   write.** Without checks, a write that was committed still ends the task `succeeded`
   ([Stop a query](../how-to/stop-a-query.md#a-worker-that-is-lost)). With checks, the task is
   taken again and runs only its checks, so a table that was written and not checked does not let
   the tables below it start.
5. **A commit that would leave a table with no current snapshot is refused,** also through the
   Iceberg REST catalog, because readers would see an empty table. So is a commit that publishes
   a table that a failed first build left not published by pointing at an existing snapshot. An
   engine that removed a table's `main` reference on purpose would now be refused; none is known.
   The message says to drop the table, or to write the rows it should hold.
6. **`lakana check rows` shows the rows a failed check found, for seven days.** A set-back keeps
   the failed state of the table under a tag named `lakana-check-failed-` and the run's
   identifier, and `lakana table maintain` removes the tag and its snapshot once the snapshot is
   more than seven days old, whatever `--keep-days` says. Look at the rows you need before then
   (see [Look at the rows that failed](../how-to/check-your-data.md#look-at-the-rows-that-failed));
   the number of rows stays in `lakana check list`. The report of `lakana table maintain` lists the
   removed tags as `tags_removed`.
7. **Nothing is converted.** A table is checked only by the checks you write for it.

### The log escapes control characters

Both processes now write control characters in a log record as text (an escape as `\x1b`, a
carriage return as `\x0d`; a tab becomes four spaces), so that a name, a path or a library's
message cannot forge a line or recolour a terminal. What changes for you:

1. **Every line after the first of a multi-line record, such as a traceback, starts with four
   spaces and `| `.** A search or an alert rule that matched a traceback line by its start (for
   example `^Traceback`) needs to allow the prefix. A line that does not start with a space is
   the start of a record.
2. **Uvicorn's level names have no colour on a terminal any more.**
3. **A record longer than 1 MiB of text is cut**, with a count of what was left out.

The details are in the paragraph "Control characters in the log" of
[Deploy with Docker Compose](../how-to/deploy-with-compose.md).
