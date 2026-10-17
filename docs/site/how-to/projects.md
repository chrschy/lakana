# Projects and deploys

A Lakana **project** is a git repository of SQL and Python assets. Deploying a commit validates
it and records an immutable **project version** for one environment; runs execute it via
`lakana run` (M5c1). Data quality checks are described in [Check your data](check-your-data.md);
jobs and their schedules are declared in `lakana.yaml` (see "Jobs" below).

## Start a project

<!-- not tested: moved as is; sorted in the second pull request -->
```bash
lakana init analytics          # lakana.yaml, a sample (three assets, one check), tests/,
                               # pyproject.toml, .github/workflows/lakana-deploy.yml,
                               # .gitlab-ci.yml
cd analytics && git init && git add -A && git commit -m "Initial project"
```

The sample is a source, `assets/bronze/readings.sql`, which generates its rows (nothing is
downloaded), two assets built from it (`assets/silver/station_temperatures.sql` and
`assets/gold/station_ranking.sql`) and one check, so a new project deploys, runs and can be queried
without writing a line; "Getting started" walks through it. It is written unless your default
profile (or `--url` or `LAKANA_URL`) points at an instance on another machine; `--sample` and
`--no-sample` decide it either way. The sample has no Python asset, because one needs the lock
described below. `lakana init` ends with the next steps: commit, register,
`lakana deploy --env dev`, `lakana run --env dev` and a query of the result. If git cannot find a name and email from the new directory (a fresh
machine), `init` adds one line with the two `git config` commands, worded as a condition: use it
only if `git commit` says it does not know who you are. A per-directory identity (git's
`includeIf`) is not visible until the repository exists, so you may see the line and not need it.
With an identity found, `init` says nothing about git.

The generated `pyproject.toml` does not depend on `lakana`: a SQL-only project needs no lock. The CI files pin
the version of the CLI that ran `lakana init` and install it from PyPI (see below).
To add a Python asset, follow the numbered steps in the comments of that `pyproject.toml`:
depend on `lakana[assets]` (see "Python assets", step 4), run `uv lock`, and commit
`pyproject.toml` and `uv.lock`. A deploy that has Python assets but no usable lock says what is missing. A complete
example is in "Python assets". Local mode (`lakana server start --local`) runs Python assets when
`uv` is on your `PATH`; the Compose bundle's image ships it ([`deploy-with-compose.md`](deploy-with-compose.md), "Python
assets"). If a worker cannot run them, its startup log says why and what to set.
When the CLI is a development or local build (`.dev`/`+` versions), `lakana init` ends with a note:
that version is not on any index, so edit the pin in both CI files before you use CI. You can
ignore the note until then.

## Assets

`assets/<namespace…>/<name>.sql` (a SQL asset) or `.py` (a Python asset, see "Python assets")
becomes the table `<namespace…>.<name>`; nested directories are nested namespaces. No path
component may contain a dot. Each SQL file holds exactly one query (`SELECT`, `WITH … SELECT` or a set operation). A table produced by another
asset of the same repository is a DAG edge; anything else is an external input and must exist in
the environment (readable by whoever deploys) or be produced by another repository's current
deployment.

Refused: symlinks, files over 1 MiB, SQL longer than 100,000 characters (`LAKANA_QUERY_LIMITS__MAX_STATEMENT_CHARS`), more than 2000 assets, a `.sql` and a `.py` file for the
same table, `__init__.py` under `assets/`, and paths that collide once case-folded or
Unicode-normalised (`Events.sql` next to `events.sql`, `model.sql` next to `Model.py`, or two
spellings of the same accented name) — they would be one table on a case-insensitive
filesystem. A `.py` file under `assets/` is a Python asset (see "Python assets"). A repository
with a `.py` file under `assets/` is also refused if it holds a symlink anywhere, or a
directory the worker cannot list, because a Python task unpacks the whole repository and
rejects links.

### Front-matter

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
retries: 3                    # run policy, see "Retries and timeouts"
retry_delay: 1m
timeout: 5m
columns:
  station_id: WMO station identifier.
*/
SELECT station_id, ts, temp_c FROM bronze.weather WHERE temp_c IS NOT NULL
```

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

## The `checks/` directory

`checks/<namespace…>/<table>/<name>.sql` is a **check** of one table: a `SELECT` that returns the
rows that are wrong. A deploy reads `checks/` with `assets/`. Every run tests the table after it
writes it, and a check that fails sets the table back and stops what is built from it. A project
without `checks/` runs as it always did. [Check your data](check-your-data.md) writes the first
check, shows the output of a failing one and gives five checks to copy; the exact rules are in
[Checks](../reference/checks.md).

## `lakana.yaml`

```yaml
repo: analytics              # lets the CLI omit the repository name
defaults:                    # applied to every asset unless its front-matter overrides
  owner: data-eng
  materialize: table
  retries: 2                 # run policy, see "Retries and timeouts"
  retry_delay: 30s
  timeout: 10m
requirements:
  fields: [description, owner]   # each asset must have them, from front-matter or defaults
  column_docs: false             # true: every column of an explicit SELECT documented
properties:                  # free-form strings, stored for later milestones
  team_channel: "#data"
checks:                      # see "The default of the checks"
  on_failure: warn           # block or warn; each check may state its own
jobs:                        # see "Jobs"
  daily_gold:
    select: ["gold.*"]
    schedule: { cron: "0 6 * * *", timezone: Europe/Berlin }
    notify: { webhook: team-alerts, format: slack }   # see "Notify a webhook"
environments:                # per-environment overrides, deep-merged over the base
  prod:
    requirements: { column_docs: true }
    defaults: { retries: 4, timeout: 15m }
    checks: { on_failure: block }
  dev:
    jobs:
      daily_gold: { schedule: null }   # no schedule in dev
```

Unknown keys are errors. An override for an environment that does not exist is allowed.

### Jobs

A **job** is a name for a selection of assets, with an optional schedule. It lives in
`lakana.yaml`, so it is reviewed, versioned and rolled back with the code, and a deploy swaps the
jobs together with the assets.

A schedule starts the job at the times its `cron:` names, in its time zone. The scheduler checks
about once a second, so a run usually starts within a few seconds of the time; a busy scheduler or
a lock held by a deploy delays it to a later check. Four rules to know:

- **A scheduled run needs a run principal.** It runs as the service account bound to the
  deployment (`lakana deployments set-run-as`), never as a person. Without one nothing starts, the
  job shows the state `no run principal`, and the times that pass are not caught up: after you
  bind one, the next time fires, not the ones before it.
- **Only one run is active in a project.** A time that comes while another run is active waits
  and starts when that run ends. Only one start waits per job: later times merge into it.
- **A time is consumed even if its start fails.** When the bound principal is inactive or the
  job cannot start, the time does not come back; the audit event `JOB_START_DROPPED` says why, and
  `lakana job list` shows `run principal unusable` while the binding is bad. A pause that lands
  just after a time was taken does not stop that one run.
- **There is no catch-up.** After downtime (every worker stopped, for example) a job starts once,
  for the latest time it missed, and the run says how many earlier times it stands in for.

`lakana job list --env ENV` shows each job's state and next run; "Why is it waiting?" explains the
states. [Run a job on a schedule](run-a-job-on-a-schedule.md) walks through it, and its section
[Cron and time zones](run-a-job-on-a-schedule.md#cron-and-time-zones) has the rules for daylight
saving time and for the two day fields.

```yaml
jobs:
  daily_gold:
    select: ["gold.*"]           # shell globs over table names, as `lakana run --select`
    upstream: false              # as `lakana run --upstream`; default false
    downstream: false            # as `lakana run --downstream`; default false
    schedule:
      cron: "0 6 * * *"
      timezone: Europe/Berlin    # an IANA zone name; UTC when left out
    notify:
      webhook: team-alerts       # the name of a secret in the environment
      format: slack              # json (default), slack or teams
  hourly_silver:
    select: ["silver.*"]
    schedule: { cron: "15 * * * *" }
  by_hand:
    select: ["bronze.*"]         # no schedule: started by hand or through the API
environments:
  dev:
    jobs:
      daily_gold: { schedule: null, notify: null }   # no schedule and no notification in dev
```

- `jobs:` is optional. A job without `schedule:` has no timer.
- `select:` takes at least one pattern, with the meaning it has for `lakana run --select` (see
  "Selection patterns"). A deploy refuses a pattern that matches no asset.
- `cron:` has exactly five fields: minute, hour, day of month, month and day of week. Each is `*`,
  a number, a range `a-b` or a comma list of these, with an optional step such as `*/15`. Months and
  weekdays may be three-letter names (`jan`, `mon-fri`). Weekdays count from 0 (Sunday) to 7, so 0
  and 7 are both Sunday and `fri-sun` is the same as `5-7`. The shorthands `@hourly`, `@daily`,
  `@weekly`, `@monthly` and `@yearly` are accepted. A sixth field for seconds is refused: a
  schedule fires at most once a minute. A deploy also refuses an expression that can never fire,
  such as `0 0 31 2 *` (there is no 31 February): change the day-of-month or the month field.
- Quote `cron:` in YAML. A value that contains `*` or starts with `@` is a YAML error unless it is
  in quotes: write `cron: "0 6 * * *"` and `cron: "@daily"`.
- `timezone:` is an IANA name such as `Europe/Berlin` or `America/New_York`. `UTC` is the default.
- A job name follows the rules of a segment of an asset name: no dot, no slash, not empty.
- An environment can change a job the file declares, with the same keys; the rest of the job is
  kept, and `schedule: null` or `notify: null` removes its schedule or its notification there.
  It cannot add a job.

Every problem comes back with the file, the line, the cause and the next step, for example
``lakana.yaml:5: jobs.daily_gold.schedule.cron: a cron expression has 5 fields (minute hour
day-of-month month day-of-week), got 6. A cron here has no seconds field: a schedule fires at most
once a minute. Example: '0 6 * * *' is every day at 06:00``, or
``lakana.yaml:3: job 'daily_gold': select 'platin.*' matches no asset. Patterns are shell globs
over table names such as 'gold.*'; change `select:` or add the asset.`` The selections are checked
against the assets when you deploy the environment they belong to.

A project version that declares jobs has manifest version 5; one in which a job has `notify:` has
version 6, so that a worker of an earlier release refuses a project version whose notifications it
would never send.

#### Notify a webhook

`notify:` names where a job reports a run that failed or recovered. It is optional; a job without
it tells nobody.

- `webhook:` is the **name of a secret** of the environment, never the URL: the URL is a
  credential, so it stays out of the repository. A name has 1 to 255 characters and no space,
  control character or `:`.
- The secret is a JSON object with the URL under `url` and, to sign the `json` format, a
  `signing_key`. Create it in each environment that should notify (see
  [Manage secrets](manage-secrets.md)):

<!-- source: docs/examples/get-told/run.sh#secret -->
```bash
printf '{"url": "https://hooks.example.com/services/T000/B000/XXXX"}' \
  | lakana secret create team-alerts --env dev --values-stdin
```

- `format:` is `json` (the default), `slack` or `teams`.
- `notify: null` under `environments:` turns the notification off in that environment; another
  `webhook:` or `format:` there changes only that key.
- A deploy refuses an unknown `format` and a `webhook` that is not a valid secret name, with the
  file, the line and the next step. It does **not** refuse a name with no secret: the deploy goes
  through and prints a warning that names the job, the secret, the environment and the command
  that creates it. A revoked secret gets its own warning: its name cannot be reused, so the warning
asks for a secret under another name in `notify.webhook`. A dry run words it as "would not notify".
  Only the name is looked up; the secret is not read.
- **Test it before a run fails.** `lakana job notify-test daily --env prod` sends a message with
  clearly fake run data to the job's webhook and waits up to 30 seconds. It prints `delivered`,
  or `failed:` with the reason (for example that the address is private, and how to allow it), or
  `still trying (attempt 2 of 5)` with the command that shows the result later. It needs the
  deploy right in the environment. A job without `notify:` in that environment is refused with
  the next step.
- `lakana runs show RUN --env prod` lists the notifications of a run with their state:
  `delivered`, `retrying (attempt 2 of 5)` or `failed: the server answered 404`. The guide
  [Get told when a job fails](get-told-when-a-job-fails.md) shows the formats, the signature and
  every reason.

### The default of the checks

`checks.on_failure` sets what a check does when it fails, for every check of the project that
does not state `on_failure` in its own `/* lakana */` comment. It takes `block` or `warn`. The
order is the check's own value, then this one, then `block`. Use `warn` while you add checks to a
pipeline that already runs, so that a check you got wrong does not stop it, and `block` for an
environment that must not publish bad data; the example above warns everywhere except in `prod`.
A deploy and a dry run tell you when the default applies, with a line after the list of checks, for example
``2 checks take `on_failure: warn` from lakana.yaml.`` `keep` is refused as a
default (``checks.on_failure: must be block or warn (got 'keep'). `keep` leaves failing data
where readers find it, so it can only be set on a check: put `on_failure: keep` in that check's
/* lakana */ block.``): it leaves failing data where readers find it, so you choose it check by
check. [Check your data](check-your-data.md#adding-checks-to-a-pipeline-that-already-runs) walks
through the order in which to turn checks on.

### Retries and timeouts

Both `defaults:` in `lakana.yaml` (and its per-environment overrides) and an asset's front-matter
accept three run settings:

```yaml
retries: 2          # retries after a failed attempt (default 2)
retry_delay: 30s    # first retry delay, doubled per attempt ±20 %, at most 10 minutes (default 30s)
timeout: 15m        # wall-clock limit of one attempt (default: the operator maximum)
```

A duration is a whole number of seconds or `<n>h`, `<n>m`, `<n>s` parts in that order (`90s`,
`5m`, `1h30m`). They are resolved at deploy time and stored in the project version, so a scheduled run
needs no flags. The operator caps them: `LAKANA_RUN__MAX_RETRIES` (default 5) and
`LAKANA_QUERY_LIMITS__TIMEOUT_SECONDS`; a deploy that asks for more fails validation, naming the file.
Lowering a cap later applies to the next run. A task's query that passes its limit is stopped for
good: it gets `LAKANA_QUERY_LIMITS__KILL_GRACE_SECONDS` (10 s) to stop by itself.

Independent of the operator caps, `retries` is at most 10,000 and `retry_delay` and `timeout` are
at most one year (31,536,000 s); a larger value fails validation.

For example, with the `lakana.yaml` above an asset without run settings gets 2 retries, 30 s then
60 s apart (±20 %), and 10 minutes per attempt in `dev`; in `prod` 4 retries and 15 minutes
(the default operator maximum). An asset
that calls a flaky upstream can override just what it needs in its front-matter:

<!-- not tested: moved as is; sorted in the second pull request -->
```sql
/* lakana
retries: 5
retry_delay: 2m     # 2 m, 4 m, 8 m, then capped at 10 m
*/
SELECT * FROM bronze.partner_feed
```

`retries: 0` disables retries; a timeout, a lost worker and storage or network errors are
retried, validation errors and permission refusals never are.

## Python assets

**SQL transforms, Python at the edges.** Joins, filters and aggregations belong in SQL assets.
The platform reads their dependencies and column lineage, runs them on DuckDB and enforces
limits and policies there. A Python asset is for what SQL cannot do at either edge of a
pipeline: ingestion (no inputs, returns rows from an API or a file format SQL cannot read) and
end use (scoring with a model, calling a library, training). If you can write it as a `SELECT`,
write it as a SQL asset.

A Python asset can produce a table (the default) or an **artifact**: a file or a directory of
files, such as a trained model, that Lakana stores in numbered versions and that another asset
reads. This page covers table assets. For artifacts, see
[Train a scikit-learn model and score with it](models-with-scikit-learn.md) and the
[reference](../reference/artifacts.md).

### A first example

This walk-through starts from a new project and ends with a table built by a Python asset. It
needs a running instance (local mode is enough) and nothing else: the example creates its own
input tables.

**1. Create the project** (see "Start a project"):

<!-- not tested: moved as is; sorted in the second pull request -->
```bash
lakana init analytics --no-sample && cd analytics
```

**2. Add two small SQL assets** that hold the data. They are ordinary SQL assets with literal
rows. `assets/bronze/readings.sql`:

<!-- not tested: moved as is; sorted in the second pull request -->
```sql
/* lakana
description: A few hand-written temperature readings.
*/
SELECT 'A1' AS station_id, TIMESTAMP '2026-01-01 00:00:00' AS ts, 1.0 AS temp_c
UNION ALL SELECT 'A1', TIMESTAMP '2026-01-05 00:00:00', 3.0
UNION ALL SELECT 'B2', TIMESTAMP '2026-01-06 00:00:00', 2.0
UNION ALL SELECT 'B2', TIMESTAMP '2026-01-07 00:00:00', 6.0
```

and `assets/bronze/stations.sql`:

<!-- not tested: moved as is; sorted in the second pull request -->
```sql
/* lakana
description: The stations that report.
*/
SELECT 'A1' AS station_id, 'North' AS region
UNION ALL SELECT 'B2', 'South'
```

**3. Add the Python asset.** `assets/gold/station_scores.py` reads both tables and writes
`gold.station_scores`:

```python
import polars as pl
from lakana import asset


@asset(
    inputs={"readings": "bronze.readings", "stations": "bronze.stations"},
    owner="data-eng",
    tags=["weather"],
    retries=2,
    timeout="10m",
    columns={
        "station_id": "Station identifier.",
        "region": "The station's region.",
        "score": "Temperature spread over the week, in degrees C.",
    },
)
def station_scores(readings: pl.DataFrame, stations: pl.DataFrame) -> pl.DataFrame:
    """Temperature spread per station over the last week of readings."""
    recent = readings.filter(pl.col("ts") > pl.col("ts").max() - pl.duration(days=7))
    scored = recent.group_by("station_id").agg(score=pl.col("temp_c").std())
    return scored.join(stations, on="station_id")
```

**4. Add the dependencies.** The generated `pyproject.toml` carries the dependency on `lakana`
in a comment, pinned to the version of the CLI that wrote the file. Delete `dependencies = []`
and remove the `# ` in front of the `dependencies` line. `lakana` and every other package come
from the public package index: `uv lock` and the worker's first build of the project's
environment need a network connection to it (the worker uses `LAKANA_RUN__HTTPS_PROXY` if you set
one, see "Operator settings"). With the line uncommented, the file reads:

```toml
[project]
name = "analytics"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["lakana[assets]==<version>"]

[dependency-groups]
dev = ["pytest>=8"]
```

The `<version>` that `lakana init` wrote is the version of the CLI that ran it. It must be the
version of the instance. To see which version the instance runs, run
`lakana --version`: it prints the CLI's version and the instance's (from `GET /api/v1/info`, which
needs a credential; in Python, `client.info().version`). It asks once and gives up after 3 s in all. If the
instance is down, too slow, answers like something else than Lakana, or no profile is configured, it says so and still exits 0. If the pin differs from
the instance's, the deploy refuses and names the version the instance runs (`uv.lock: has lakana X,
but this instance runs Y (…); update the dependency and run `uv lock``); change the pin to that
version.

A version of `lakana` that is not on PyPI, such as a build of a checkout, cannot be locked from
the public index. Build its wheel (`uv build --package lakana --wheel --out-dir wheelhouse`), put
it in a folder `wheelhouse` of the project and add a flat `uv` index for that folder as the only
source of `lakana`: a `[[tool.uv.index]]` table with `name = "lakana"`, `url = "wheelhouse"`,
`format = "flat"` and `explicit = true`, and `[tool.uv.sources]` with
`lakana = { index = "lakana" }`. Commit the folder with the lock.

`uv` is the Python package manager from Astral. Its lock file, `uv.lock`, pins every package and
its hash. You create it and commit it; Lakana never resolves versions for you:

<!-- not tested: moved as is; sorted in the second pull request -->
```bash
uv lock
```

**5. Commit, register and deploy.** A deploy reads a git commit, so commit everything first. An
admin registers the repository once ("Register the repository (admin)"). For a local path, that
is a repository under one of the instance's `LAKANA_REPO_ROOTS` (in local mode, your home
directory); for a hosted repository it is a pushed commit.

<!-- not tested: moved as is; sorted in the second pull request -->
```bash
git init && git add -A && git commit -m "Add station scores"
lakana repo add analytics --kind local --url "$PWD"      # once, admin
lakana deploy --env dev
```

**6. Run it and read the result:**

<!-- not tested: moved as is; sorted in the second pull request -->
```bash
lakana run --env dev
lakana sql "SELECT * FROM gold.station_scores" --env dev
```

The run builds `bronze.readings` and `bronze.stations` first, because the Python asset declares
them as inputs. The first run also builds the asset's environment, which takes a while.

### The rules of a Python asset

- **One file, one asset.** `assets/<namespace…>/<name>.py` is the table `<namespace…>.<name>`.
  The function's name does not matter. Every `.py` file under `assets/` is an asset. Put helper
  code at the repository root or under `src/`; both are importable at run time. A helper module
  that has the same name as an installed package or a standard library module cannot be
  imported under that name, because the repository is searched last.
- **Inputs.** `inputs` maps each parameter of the function to a table or an artifact. A parameter
  that names a table receives the whole table as a `polars.DataFrame` (a table in memory). A
  parameter that names an artifact receives a `pathlib.Path` to a read-only copy of its files (see
  the [reference](../reference/artifacts.md)). It is read as the run principal,
  so row filters and column masks apply, and it is bound by the query limits
  (`LAKANA_QUERY_LIMITS__MAX_RESULT_ROWS`, default one million rows). If an input is too large,
  narrow it first with a SQL asset (usually a `view`) and declare that as the input. An asset
  that reads its own table declares it as an input. The table must exist before such an asset
  runs: the first run of an asset that reads itself fails at once, without retries, with
  `<table> reads its own table as an input, but the table does not exist yet`. Create the table
  with a first version of the asset that does not read itself, run it, then deploy the
  self-reading version.
- **Decimals.** DuckDB types a literal such as `12.0` as `DECIMAL`, a type of exact decimal
  numbers. Your function receives such a column as floating-point numbers (a `Float64` column),
  also inside a list or a struct, so `value * 0.5` works. An asset that must not round, for
  example one that adds up money, asks for the exact values with `@asset(decimals="exact")`: the
  column then arrives as a `Decimal` column of `decimal.Decimal` values, which cannot be
  multiplied by a float. A float holds about 15 significant digits: a decimal value beyond 2^53
  (about 9 * 10^15) or with more than about 15 significant digits, a decimal identifier or the
  sum of a wide column for example, arrives rounded, without an error. Such a column needs
  `decimals="exact"`. What your function returns is stored as it is: a float column as a
  double, a decimal column as a decimal. Older Lakana releases always
  handed a Python asset `decimal.Decimal` values; an asset written for them that relies on
  that needs `decimals="exact"` after an upgrade.
- **Output.** The function returns a `polars.DataFrame`. It replaces the asset's table in one
  snapshot. Zero rows are fine. With `output="artifact"` it returns a `pathlib.Path` inside
  `lakana.output_dir()` instead, and each run stores a new version of the artifact (see the
  [reference](../reference/artifacts.md)).
- **Metadata.** `description` (default: the first paragraph of the docstring), `owner`, `tags`,
  `columns` (not for an artifact asset), `retries`, `retry_delay` and `timeout` work as in SQL front-matter, with the same
  defaults from `lakana.yaml` and the same operator maximums (see "Retries and timeouts").
- **Only literals.** Deploy reads the decorator without running your code, so every argument
  must be written out as a literal: strings, numbers, lists and dicts of strings. Variables,
  `**unpacking` and function calls are refused.
- **Lineage.** A Python asset has dependency lineage (its inputs) and no column lineage. A SQL
  asset can read a Python asset's table by name; the run orders them as usual.
- **Who sees a dropped table in `table lineage`.** Lineage lists only tables you may read. A table
  that was dropped stays in the history of the tables it fed, marked `dropped`, and is shown only
  to someone who may read its whole namespace (a grant on the namespace, the environment or the
  instance) and never to a task token. A table created later under the same name is a new table:
  a grant on it does not show the old one. It is counted in `hidden_nodes` instead. To let a
  colleague see the history, grant them read on the namespace.

### Polars 1 and Polars 2

`lakana[assets]` asks for `polars>=1.30` and sets no upper bound, so a project that runs `uv lock`
now gets **Polars 2** (released in October 2026). Lakana runs assets on both, and its own tests run on
2 and on 1.30. A project that was locked earlier keeps the Polars version in its `uv.lock` until it
runs `uv lock --upgrade-package polars`. What changes for the code of an asset when a project
moves from 1 to 2 (the full list is Polars' own
[upgrade guide](https://docs.pola.rs/releases/upgrade/2/)):

- **`LazyFrame.collect()` uses the streaming engine** and does not keep the row order after joins
  and group-bys unless you ask. If your asset depends on an order, sort explicitly
  (`.sort(...)`) before you return the frame, or use `maintain_order` where the method offers it.
- **Map columns arrive as `pl.Map(key, value)`.** In Polars 1 they were a list of structs. A table
  stores no map, so only an input that a view returns can hold one, and returning a map column is
  refused (`Column 'x' has an unsupported Arrow type: map<…>`): turn it into columns first.
- **Adding a signed and an unsigned integer column gives `Int128`**, not a float. A table cannot
  store `Int128` (or `UInt128`): the task fails with `column 'x' has type Int128, which cannot be
  stored in a table; cast it to Int64 …`. Cast the result to `pl.Int64`, or to a decimal.
- **An Arrow extension type Polars does not know loads as `pl.Extension`.** Take its stored values
  with `pl.col("x").ext.storage()` before you return it.
- **Some casts are stricter** and raise where Polars 1 converted silently. A task that fails with
  a Polars `ComputeError` about a cast after an upgrade needs the cast to say what should happen.

To stay on Polars 1, pin it in the project's own `pyproject.toml` and lock again:

```toml
dependencies = ["lakana[assets]==<version>", "polars>=1.30,<2"]
```

Run `uv lock`, commit `uv.lock` and deploy. Polars 1.30 is the oldest version Lakana supports. One convenience needs a newer one: passing a
Polars `Schema` to `client.tables.create(...)` needs Polars 1.37 or newer. On an older Polars
the call fails with a message that says so; pass `pyarrow.schema(...)` or a list of columns
instead.
Spilling to disk, the one thing Polars 2 does that can use the worker's disk, is described under
"Polars inside a task" in the section "Run" below.

### What Python assets do not do (yet)

- `materialize: table` only. `incremental` is not supported (#23), and `view` is refused too.
- Table inputs and a table output are whole DataFrames in memory. There is no streaming (#22), so
  the worker needs memory for your inputs, your output and your own work. An artifact is a file on
  disk and is not held in memory by Lakana.
- A changed output schema is a permanent failure: the asset cannot pass `evolve_schema`. Restore
  the columns and types the table has, or drop the table (`lakana table delete`) and run the
  asset again, so it is created with the new schema.
- No `async def`.
- `@asset(...)` must be the only decorator on the function. To wrap behaviour, do it inside the
  function.
- Local path and workspace dependencies in `uv.lock` are refused. Put helper code in the
  repository, or publish it as a package.
- No column lineage, no lazy input API, no resource classes.

### What a deploy checks

A deploy never runs your code. It reads each `.py` file with Python's `ast` module and the two
dependency files as TOML. Nothing is installed at deploy. Every refusal names the file and, where
there is one, the line, as `path:line: message`. Fix the cause and deploy again.

| The deploy says | Cause | Next step |
|---|---|---|
| `no function decorated with @asset; the decorator must be lakana.asset: …` | The file has no `@asset(...)` function, or the decorator is written another way (an alias, a re-export). | Write `from lakana import asset` and `@asset(...)`, or `import lakana` and `@lakana.asset(...)`. |
| `more than one @asset function; one file is one asset` | Two decorated functions. | Move one into its own file. |
| `write @asset(...) with parentheses` | `@asset` without `(...)`. | Write `@asset(inputs={...})`; use `@asset()` for none. |
| `the asset function must have @asset(...) as its only decorator` | Another decorator sits on the function. | Remove it, or call the wrapped code inside the function. |
| `argument 'x' must be a literal`, `unknown argument 'x'`, `argument 'x' must be a dict of strings` | The decorator has a variable, an unknown name or the wrong type. | Write the value out, and use only the arguments listed above. |
| `parameter 'x' has no entry in inputs`, `inputs names 'x', which is not a parameter` | The function's parameters and `inputs` differ. | Make them the same set of names. |
| `positional-only parameters are not supported`, `*args is not supported; …`, `**kwargs is not supported; …`, `a parameter must not have a default` | The signature has a shape the harness cannot fill. | Declare every input as a plain parameter. |
| `an asset function must not be async` | `async def`. | Use `def`. |
| `input 'x': …` | The input is not a valid table name. | Use `namespace.table`. |
| `table 'x' not found` | An input is not produced by this repository and does not exist in the environment, or the deploying principal may not read it. When two repositories share an artifact, this is also what the consumer's deploy says if it comes **before** the producer's first deploy. | Deploy the repository that produces it first, or fix the name. See "Two repositories, one artifact" below. |
| `` `models.x` is an artifact; only a Python asset can read it. `` | A SQL asset names an artifact in `FROM`. | Read it in a Python asset. |
| `` `x.y` already exists in dev as a table; drop it first, or choose another name. `` (or `a view`, `an external table`) | An artifact asset has the name of a table, view or external table. | Choose another file name, or drop the table with the `table delete` subcommand. |
| `` `x.y` already exists in dev as an artifact; drop it first (`lakana artifact drop x.y`), or choose another name. `` | A table asset has the name of an artifact. | Choose another name, or drop the artifact. |
| `parameter 'x' reads the artifact …, which the task stores in a directory of that name: rename the parameter …` | The parameter that reads an artifact is not letters, digits and underscores, or is longer than 128 characters. | Rename it in the function and in `inputs`. Two such parameters may not differ only in case. |
| `an artifact asset cannot read its own artifact (x.y); remove it from inputs` | An artifact asset lists itself as an input. | Remove it. |
| `argument 'output' must be 'table' or 'artifact', got …`, `argument 'keep_versions' must be an integer of at least 1`, `argument 'materialize' does not apply to an artifact asset; remove it` | A wrong value for an artifact option. | See the [reference](../reference/artifacts.md). |
| `` `output` applies only to a Python asset, not to a SQL file; declare the asset in a `.py` file with … `` | `output` or `keep_versions` in the front matter of a SQL file. | Declare the asset in a `.py` file, or remove the key. |
| `the file does not parse: …` | A syntax error. | Fix the line it names. |
| `incremental Python assets are not supported yet (#23)`, `a Python asset supports only materialize 'table', got …` | `materialize` is not `table`. | Remove the argument, or write a SQL asset. |
| `assets/a.sql and assets/a.py both produce table 'a'` | A SQL file and a Python file for one table. | Keep one. |
| `every .py file under assets/ is an asset; put helper code outside assets/ …` | `__init__.py` or a helper module under `assets/`. | Move helper code to the repository root or `src/`. |
| `symlinks are not allowed in a repository with Python assets` | A symlink anywhere in the repository. | Replace it with a copy of the file. |
| `a project with Python assets needs a pyproject.toml at the repository root` | No `pyproject.toml`. | Add one (see the example above). |
| `a project with Python assets needs a committed uv.lock; run `uv lock`` | No lock file. | Run `uv lock`, then commit it. |
| `does not contain lakana; add lakana[assets] …`, `does not contain polars; depend on lakana[assets] …` | The lock lacks the SDK or polars. | Depend on `lakana[assets]` and run `uv lock`. |
| `lakana is locked, but no task installs it: it is only in a dependency group or an extra …` | `lakana` is in `uv.lock` only through a dependency group (for example `uv add --dev`) or an extra, and a task installs neither. | Add `lakana[assets]` to `[project] dependencies`, run `uv lock`, commit and deploy again. |
| `has lakana X, but this instance runs Y (…); update the dependency and run `uv lock`` | The locked `lakana` does not match the instance. Before 1.0 the minor versions must match; from 1.0 the major versions. | Change the pin, run `uv lock`, commit. |
| `local path and workspace dependencies are not supported (…)` | The lock installs a package from a path in the repository. | Put the code in the repository (it is importable) or depend on a released package. |
| `uv.toml: is not supported in a project with Python assets …` | A `uv.toml` at the repository root. The worker builds with `uv sync --frozen --no-config`, which ignores it. | Put settings that shape the lock into `[tool.uv]` in `pyproject.toml` (they apply when you run `uv lock`), delete `uv.toml`, run `uv lock`, commit and deploy again. A task of an older deploy that has one fails with the same message until you redeploy. |
| `uv.lock: takes X from the package index URL, which is not registered on this instance …` | A package comes from an index nobody registered on this instance. | Ask an administrator to register the index (`lakana index add NAME --url URL`), or take the package from a registered index (`lakana index list`), then run `uv lock`. See [Use a private package index](private-package-index.md). |
| `uv.lock: takes X from the package index URL, which is not a registered package index, and this URL cannot be registered as written: …` | A package comes from an index URL that no administrator could register (`http` for a remote host, a user name, `%` or `..` in the path, a query). | Take the package from a registered index (`lakana index list`), or publish it to an index with a plain `https` URL; the message says which rule the URL breaks. |
| `uv.lock: takes X from a file outside the folder `wheelhouse` …` | A wheel or source distribution of `wheelhouse` has a path with `..`, a leading `/` or a backslash. | Put the file into `wheelhouse`, then run `uv lock`. |
| `uv.lock: takes X from the local folder F; the only folder a lock may name is `wheelhouse` …` | A package comes from a folder of the project other than `wheelhouse`. | Rename the folder or take the package from a package index, then run `uv lock`. |
| `uv.lock: takes X from a git repository, which is not supported …`, `… from a direct URL, which is not supported …` | A package comes from a git repository or a single file URL. | Publish the package to a package index, ask an administrator to register that index, then run `uv lock`. |
| `pyproject.toml: the index N (URL) is a registered package index and needs `explicit = true` …` | A registered index is declared without `explicit = true`, so `uv lock` could take any package from it, or take one of its packages from the public index. | Add `explicit = true` to its `[[tool.uv.index]]` table and one line per package under `[tool.uv.sources]` (`PACKAGE = { index = "N" }`), then run `uv lock`. |
| `pyproject.toml: the index named N points at URL, but the package index registered as M is URL2 …` | The project's index has the name of a registered index and another URL. Checked at a deploy only, and not for an index whose `url` is a folder, such as `wheelhouse`. | Use the registered URL, or give your index another name, then run `uv lock`. |
| `pyproject.toml: `extra-index-url` names the registered package index N …` (or `index-url`) | A registered index is named as `index-url` or `extra-index-url`. | Declare it as a `[[tool.uv.index]]` table with `explicit = true` instead, and name its packages under `[tool.uv.sources]`, then run `uv lock`. |
| `requires-python '…' does not admit the worker's Python X` | The project's Python range excludes the worker's Python. | Widen `requires-python`, then `uv lock`. |
| `this instance cannot run Python assets: …` | The worker has no `LAKANA_RUN__SERVER_URL`, or no `uv`. | Ask whoever runs the instance; see "Operator settings" below. |

### Two repositories, one artifact

When the producer of an artifact is in repository A and its consumer in repository B, the order
matters, because each deploy records what it sees:

1. A deploy of B **before** A has deployed fails with `table 'models.x' not found`.
2. After A's deploy and **before** A's first run, B deploys, but a run of B's consumer fails with
   `models.x has no version yet; run the asset that produces it`.
3. If the kind of the name changes later (A switches the asset between a table and an artifact),
   B's consumer fails with `models.x is a table, but this deploy recorded it as an artifact; ...` or
   `models.x is an artifact, but this deploy recorded it as a table; ...`. Redeploy B.

So deploy A first, run it once, then deploy B.

### What a task may read and write

Each attempt of a Python asset is a **task** that runs in its own process on a worker. The
worker gives the process a **task token**: a credential that is valid for this one attempt and
only while it runs. With it, your code can:

- read the tables and artifacts declared in `inputs` (and, when an input is a view, the tables that
  view reads: a view is not a security boundary here);
- replace its own table, or add a version to its own artifact if it produces one, whether or not
  the environment is protected (a token belongs to one environment);
- read the deploys and runs of its environment.

It can do nothing else: no other table or artifact, no `DROP`, no deleting an artifact version,
no listing of tables, no administrative action. It also cannot do more than the run principal could (see "Run principals"): grant that
principal `READ_TABLE` on the inputs (tables and artifacts) and `CREATE_TABLE` and `WRITE_TABLE` on
the output. The
token never appears on a command line, in the environment or in the task log.

The worker unpacks your repository fresh for every attempt into a private, read-only
directory, which is the process's working directory. The process gets a private temporary
directory and a small environment (`PATH`, `HOME`, `TMPDIR`, two `PYTHON*` settings, a Polars
spill directory and, when the worker has a memory limit, two more `POLARS_*` settings, see
"Polars inside a task"). Its
output and the output of the environment build are in `lakana runs logs`, up to 1 MiB per
attempt (see "Run").

### Isolation: one instance is one trust domain

**`LocalProcessRunner`, which runs Python assets, is not a sandbox.** A task runs as the
worker's operating-system user. It can read everything the worker can read: the worker's own
settings and credentials (its database and storage access among them), the files in its home
directory, and anything it can reach on the network. The venv cache is shared between tasks and
writable by them, so one task can change the environment another task will use. The scrubbed
environment, the private temporary directory and the task token limit accidents; they do not
stop code that means harm.

So one instance is one **trust domain**: everyone who may deploy Python code to it is trusted
with everything the worker can read. Deploy only code and dependencies you would run on the
worker itself, and run one instance per client or per group of people who do not trust each
other. Task isolation comes with `DockerRunner` (M7b, #38), which is planned before the public
beta (M7c); until then, treat a Python asset like code you run on the worker's machine
yourself.

### Failure, retry and cancel

| Situation | What happens |
|---|---|
| Your function raises | The task fails with the exception's type and message; the traceback is in the task log. Retried under the asset's policy. |
| A SQL query with a syntax error | Fails with `SQL syntax error at line 1, column 7: …` (the reason, the position and the token it stopped at; the cause is `invalid_query`). Not retried: the same SQL fails again. Fix the query. |
| A SQL query nested too deeply (hundreds of parentheses) | Fails with `SQL syntax error: the query is nested too deeply. Simplify it …`; the cause is `invalid_query`. Not retried. |
| A view whose stored SQL no longer parses | Fails with `View 'gold.v' could not be read. Ask its owner to recreate it.` and shows none of the view's SQL. |
| A file that cannot be loaded (a syntax error, a module that is not installed, no `@asset` function) | Fails with a message that names the cause. Not retried: the same code fails again. |
| An input cannot be read | Fails with `reading input 'obs' (silver.weather) failed: …`. The server says why the input query failed, and the task is **not retried** when the cause repeats: the input table does not exist (dropped or renamed), the run principal may not read it, or the input is too large. Too large also says to narrow it with an upstream SQL asset (for example a view) and declare that as the input. Running out of memory (cause `out_of_memory`) is retried, and its message says to narrow the input with an upstream SQL asset or to ask the operator to raise the memory limit that the message names (`LAKANA_QUERY_LIMITS__MEMORY_LIMIT` for a query that failed on its own limit, `LAKANA_WORKER__MEMORY_LIMIT` when the whole worker was stopped). A sort or aggregation over DuckDB's limit is out of memory too, because DuckDB cannot spill to disk there. Any other failure (a timeout, a storage error, a query the engine rejects, an unknown cause) is retried until `retries` runs out. A 401, 403 or 404 from the API itself is not retried either. A server older than this behaviour sends no cause, and its failed input queries are retried as before. |
| The worker ended a task's query alone for using too much memory (Linux; cause `out_of_memory` on the query run) | The message says the query made the worker exceed its memory limit and names the setting to raise. A SQL task is **not retried**. A Python task whose input query was ended is handled like any input that ran out of memory: retried while it has attempts left, and its message says to narrow the input with an upstream SQL asset or to ask the operator to raise the limit. The worker and a Python task beside the query keep running. |
| The worker ended this task alone because it was the largest consumer over the worker's memory limit (Linux) | The attempt fails as a transient failure with `this task was ended because the worker exceeded its memory limit (4GB) and the task was the largest consumer. Use less data in the task (narrow its inputs with an upstream SQL asset), or ask the operator to raise the limit (LAKANA_WORKER__MEMORY_LIMIT, or LAKANA_WORKER_MEMORY in the Compose bundle's .env).` The task is **retried** while it has attempts left, even when it was the only task: the worker's own memory may have been part of the total. A task that always needs more than the limit fails every attempt. The worker and a query beside it keep running. A task still building its environment cannot be ended this way; if nothing else can be ended, the worker stops. |
| The worker stops for using too much memory (cause `out_of_memory` on the query run) | The message names the limit and the setting to raise. On Linux a running Python task is always ended first and retried (the row above), so this row reaches only a SQL task and a Python task still building its environment. Such a task is **not retried** when the worker was running only this task (and its own query runs): it would need the same memory again. A SQL task whose write had already been committed is `succeeded` instead (the row above; a task whose table has [checks](check-your-data.md) is taken again and only checks). On macOS every task is handled this way. When an unrelated query was running on the worker too, the blame is uncertain and the task is retried while it has attempts left. |
| A Python asset raises `MemoryError` | Fails with `MemoryError. The task ran out of memory: use less data …, or run it on a machine or in a container with more memory.` Not retried. The machine or container itself had no memory left: narrow the inputs with an upstream SQL asset, work in smaller pieces, or give the worker's machine or container more memory. |
| A Python asset's function raises `TypeError` | Fails with `TypeError: <the exception's text>` and a second line: `This error is almost always a mistake in the asset's code, so it is not retried. Fix the function, deploy, and run again.` Not retried: a wrong type is almost always a mistake in the code, and every attempt repeats it. If the cause can be transient (a library raising `TypeError` on a value an HTTP call did not return this time, for example), catch it in the function and raise another exception type, such as `RuntimeError`, which is retried. This holds for a `TypeError` (or a subclass) raised out of the function itself, not for one raised while the file is imported. Any other exception of the function (`ValueError`, `KeyError`, `NameError`, a failed network call, ...) is retried until `retries` runs out, because it can depend on the data or on the moment. |
| A return value that cannot become a table | Fails and is not retried: `the asset returned a polars LazyFrame; call .collect() …`, `… a pandas DataFrame; return pl.from_pandas(df)`, `… a DataFrame without columns`, or `column 'x' has type …, which cannot be stored in a table`. A column of type `Int128` or `UInt128` is refused by name, with the way out: `cast it to Int64 (pl.col('x').cast(pl.Int64)), or to a decimal such as pl.Decimal(38, 0) if the values do not fit into 64 bits` (Polars 2 gives `Int128` when you add a signed and an unsigned integer column). An `Extension` column (an Arrow extension type Polars does not know) is refused with `take its stored values with pl.col('x').ext.storage() and cast them to a type a table stores`. A table stores no list, struct or map either: such a column is refused as `Column 'x' has an unsupported Arrow type: …`. |
| The output schema changed | Fails and is not retried: `the asset's output no longer matches the schema of <table> …`, with the remedy. |
| The process exits or is killed without a result | `the task process exited with status N without a result` or `the task process was killed by signal N`. Retried. A kill by signal 9 is usually the operating system running out of memory, and signal 6 is an abort, as after a failed allocation in native code: the fix is less data in memory (narrow the inputs) or a worker with more memory, not another retry. |
| An artifact input has no version, or cannot be read | `models.x has no version yet; run the asset that produces it` (not retried), or `models.x cannot be read by the run's principal 'svc-run', or does not exist; grant read on it or check the name` (not retried). Download problems say `reading input 'model' (models.x, version 3) failed: …`; a version that was deleted since the task started is retried and resolved again, and a refusal such as a grant revoked after the task was prepared is not retried. A version whose stored manifest the client rejects (it does not match its own digest, or it is over a limit such as a path the client refuses) is not retried either: every attempt would meet the same record. Its message says so (`The stored manifest of version 3 does not match its digest, so this version is damaged on the server and downloading it again will not help. Tell the operator, and store the artifact again …`). A downloaded file that does not match its manifest, a broken connection and a 5xx are retried. |
| An artifact upload is refused as too large (413) | The task fails with the server's text, for example `The manifest request is larger than 16777216 bytes. It lists every file with its path and hash: store fewer files or give them shorter paths (the limit is fixed).` Not retried: the operator's limit is reached, and the same upload is refused again. Store fewer or smaller files. |
| A file the asset wrote cannot be read while the artifact is stored | `<path> could not be read: <the operating system's reason>. The upload was stopped and can be tried again once the file is readable.` Not retried: the task fails with the reason (a disk error, a file system that went away). A file that changed while it was sent says `<path> changed after it was scanned …` instead. |
| The kind of an input or of the asset's own name changed since the deploy | `models.x is a table, but this deploy recorded it as an artifact; redeploy so that the asset reads it as a table` (or `a view`, `an external table`), `models.x is an artifact, but this deploy recorded it as a table; redeploy so that the asset receives a path`, or, for an artifact asset whose own name is now a table, ``models.x is a table, not an artifact. Drop it (`lakana table delete models.x`) or give the asset another name and redeploy.`` (or `a view`, `an external table`). None is retried. Redeploy the consuming repository, or drop the table with the command the message gives. |
| Code of an asset stores or writes the wrong kind of output | `Task 'models.x' produces a table; it cannot add an artifact version. To store an artifact, declare the asset with output="artifact".` or `Task 'models.x' produces an artifact; it cannot write a table. Write the artifact's files into lakana.output_dir() instead.` Declare the output you mean, and return the path or the DataFrame. |
| An artifact has 16 uploads in progress | ``models.x has 16 uploads in progress, the most allowed at once. Commit one, withdraw one you began (`lakana artifact uploads list models.x` shows yours, `lakana artifact uploads abort models.x UPLOAD_ID` withdraws one), or wait for one to expire (they expire after 3600 seconds), then try again.`` The attempt is retried. A new attempt first deletes the open uploads of its own task run's earlier attempts, so only uploads of people and of other task runs can fill the places; wait for the retry, or withdraw an upload you began ([List and withdraw an open upload](withdraw-an-artifact-upload.md)). |
| The task's process is killed or times out after its version was stored | The task shows as failed or timed out and is retried; its version **stays live**, and a retry stores a newer one. With `keep_versions=1` the previous version is already deleted, so use at least 2 if you want the last good model to survive a bad run. |
| The worker is older than the platform that deployed the project | `the project version's manifest has version 5, but this worker understands versions 1 to 4; update the worker to the platform version that deployed it, or redeploy with this worker's version` (the numbers differ). Not retried. Update the worker. |
| An artifact asset returns the wrong thing | `an artifact asset must return a pathlib.Path inside lakana.output_dir() …`, `the asset returned …, which is not inside lakana.output_dir() …`, `… which does not exist …`, `… which passes through a symbolic link …`. Not retried. See the [reference](../reference/artifacts.md). |
| The task reports success but its table did not change | `the task reported success without writing its table`; not retried. An attempt counts as succeeded only when the platform finds the table changed. For an artifact asset the platform looks for a version stored by this attempt and says `the task reported success without storing a version of its artifact …`. |
| The environment build fails | `environment build failed: …`, with uv's output in the log. Retried. A build that runs out of time or space says so. |
| A package index refuses the credential of a registered index | ``environment build failed: The package index 'team' refused the credential in secret 'team-index' of environment 'dev'. …`` Not retried: the same credential is refused again. Set the secret again with the command in the message (`lakana secret rotate team-index --env dev --values-stdin`); the next run builds the environment with it, without a new deploy. See [Use a private package index](private-package-index.md). |
| The secret of a registered index is missing, has no usable `password`, or cannot be decrypted | ``environment build failed: the package index 'team' needs the secret 'team-index' in environment 'dev', which does not exist. …`` or ``environment build failed: the secret 'team-index' of environment 'dev', which the package index 'team' uses, has no usable "password". …``. Not retried. Create or set the secret with the command in the message. A deploy stops with the same text, and a run whose environment is already built does not read the secret. |
| A package has no wheel for this platform and Python, and a registered index with a credential is in use | `environment build failed: sdonly has no wheel for this platform and Python; publish a wheel to your index, or remove the dependency. …` Not retried. A project that uses a package index with a credential installs wheels only: no package's build script runs while the credential is in reach. |
| The asset reads its own table, which does not exist yet | `<table> reads its own table as an input, but the table does not exist yet …`. Not retried. Create the table with a version of the asset that does not read itself, then deploy the self-reading version. |
| The task's environment has no `lakana` | `the task's environment does not contain the lakana package …`. Not retried. A deploy normally refuses this earlier; it can still happen when `lakana` is only reachable through an extra that is not switched on, or is excluded on the worker's platform by a marker. Add `lakana[assets]` to `[project] dependencies`, run `uv lock`, commit and deploy again. |
| The venv cache is not private | `environment build failed: the venv cache directory is not private …`. Not retried; the operator fixes the directory. |
| Timeout | The process and its process group are killed: `timed out after 600s`. Retried. The timeout starts when your process starts; the environment build does not count, but the wait of your input queries does. |
| Another writer changed the table during the run | `<table> changed during the write; retry.` The platform retries once at once, without counting an attempt, so the function reads fresh inputs. A second conflict is an ordinary failed attempt. |
| Cancel (`lakana runs cancel`) | The process is killed and the task is cancelled, with the message `The run was cancelled.` (`The run was stopped because another task failed its code integrity check.` when the platform stopped the run for that reason). During an environment build, the cancel takes effect when the build ends. |
| The worker is stopped (a drain) or lost | The task is released to another worker; a stop is not counted as an attempt. A SQL task that is lost is tried again under its policy, except in the next two rows. |
| The worker is lost, or stops itself for its memory limit in a way that ends the task for good, **after a Python task with checks may have written its table**, on the task's last attempt, or in a run that you cancel (a worker that sees the cancel after the write hands the task back in the same way) (also a SQL task with checks whose write landed, and also when you cancel after the task already waits again for its checks or its next attempt) | The task does not end at once. It waits with the reason `setting_back`, and the next free worker only sets the table back to where it started, without running the task or its checks. Then the task is `failed` (or `cancelled`), and its message says what became of the table: see [A worker that is lost](check-your-data.md#a-worker-that-is-lost). A Python task without checks records no baseline, so a lost one leaves what it wrote. |
| The worker is lost, stopped for its memory limit, or fails to record the outcome, **after a SQL task's write was committed** (a task whose table has [checks](check-your-data.md) is the exception: it is taken again and only checks) | The task is `succeeded` and is **not** run again, also on its last attempt and also while the run is being cancelled (the data is in the table). When the worker was lost, its message is `The worker was lost after this task's write had been committed. The task counts as succeeded and was not run again.` (for a memory stop it says that, with the limit's setting); when the worker was alive and only recording the outcome failed, it may carry a message that says so, or none; its attempt is `succeeded` with the rows written, and the tasks below it go on. It has no watermark or schema change, because the worker never recorded them, and, when the worker was lost, its finish time is the moment the platform found the commit, so its duration includes the wait for the lease to run out. Only the write of the task's **current** attempt counts: a write that an earlier attempt committed and lost is not looked for. |
| The lock names a package index that is no longer registered | `… The package indexes registered on this instance no longer match this project version: register the index again …`. Not retried; it fails for good. Ask an administrator to register the index again (`lakana index list` shows what is registered) and start the run again: no deploy is needed. Or change the project as the message says and deploy again. |
| The project declares an index that is registered now in a way that is not allowed | `… A package index is registered on this instance that the project declares in a way that is not allowed: declare it as a `[[tool.uv.index]]` table with `explicit = true`, …`. Not retried. Registering the index again does not help: declare it with `explicit = true`, name its packages under `[tool.uv.sources]`, run `uv lock` and deploy again. |
| The lock no longer fits the instance (after a platform upgrade) | `… This project version was deployed for another platform version: update the lock and redeploy.` Not retried; retrying the run cannot fix it. Lock again and deploy, unless the lock's `lakana` version in the message is higher than the one the instance runs: then a worker of an older release picked the task up, and the fix is to upgrade that worker (see [Upgrade to the release with artifacts](upgrade-to-artifacts.md)). |

A task's token must outlive the task. If `LAKANA_TASK_TOKENS__MAX_TTL_SECONDS` is below the
asset's timeout plus the kill grace (`LAKANA_RUN__KILL_GRACE`, 10 s) plus 60 s, the task
fails at once and the message names the value to set.
Retries, retry delay and timeout follow "Retries and timeouts".

### Dependencies

A project with a Python asset has a `pyproject.toml` and a committed `uv.lock` at the repository
root. The lock must contain `lakana` at a version compatible with the instance (the same minor
version before 1.0, the same major version from 1.0) and `polars`; depending on `lakana[assets]`
gives you both. It must be one of the project's own `[project] dependencies`: a task installs no
dependency group and no extra, so `lakana` in `[dependency-groups]` is refused at deploy. `requires-python` must admit the worker's Python. Your repository's own code is
not installed: it is imported from the checkout.

The first task that needs an environment builds it from the lock (`uv sync --frozen`) and caches
it, keyed by the hash of the lock together with your `pyproject.toml`, the Python version and the
platform. Projects whose lock and `pyproject.toml` are both identical share the environment; a
change to either builds a new one.

Put a Python asset's dependencies in `[project] dependencies`. Packages in a dependency group
(`[dependency-groups]`, including `dev`, and `[tool.uv] default-groups`) are not installed for a
task. After a platform upgrade that changes the compatible `lakana`
version, update the lock and redeploy: tasks of the old deploy fail with a message that says so. To install a package of your own from an index with a password, see [Use a private package index](private-package-index.md).

What the build can run: it installs only what the lock names, but a package that is built from
source (a source distribution) runs its own build backend, as the
worker's user, with network access, and it can see the index and proxy credentials the worker
passes to the build. That is why a build that uses a registered index's credential builds nothing from source: uv runs with `--no-build`, and a package without a wheel fails the build with a message. The worker builds with `uv sync --frozen --no-config`: your `[tool.uv]`
settings shape the lock on your machine (when you run `uv lock`) and are ignored at build time on
the worker (a `uv.toml` is refused, see "What a deploy checks"; the worker still reads the lock and
the index definitions in `[[tool.uv.index]]`), and the lock decides which hosts the build
contacts. Prefer wheels, and review dependencies as you review code.

The lock may name only the public index, the folder `wheelhouse`, the operator's extra index and
registered package indexes; see "What a deploy checks" and [Use a private package index](private-package-index.md).

### Operator settings

| Variable | Default | Meaning |
|---|---|---|
| `LAKANA_RUN__SERVER_URL` | unset | The server's `http(s)://host[:port]` URL as a task's process reaches it, without credentials, query or fragment. Unset, the worker refuses Python assets (a deploy says so; the worker's log says so at startup). The Compose bundle and local mode set it. |
| `LAKANA_RUN__VENV_CACHE` | `<worker cache>/venvs` | Where venvs are kept. An absolute path. It must be private: owned by the worker's user, mode `0700`, not a symlink; otherwise tasks fail with "not private". |
| `LAKANA_RUN__VENV_KEEP` | `10` | Venvs kept, most recently used first (at least 1). A venv in use is never removed. |
| `LAKANA_RUN__VENV_BUILD_TIMEOUT` | `600` | Seconds one build may run (above 0). |
| `LAKANA_RUN__VENV_MAX_BYTES` | `5368709120` (5 GiB) | Largest size of one venv while it is built (above 0). |
| `LAKANA_RUN__KILL_GRACE` | `10` | Seconds between SIGTERM and SIGKILL when a task is stopped (0 or more). |
| `LAKANA_RUN__SWEEP_INTERVAL` | `3600` | Seconds between the worker's sweeps of attempt directories and deploy checkouts a dead worker left behind (the first runs at startup) and between the sweeps that the worker holding the scheduler role runs of the artifact store (expired uploads, files and manifest objects no version names) and of the code archives of failed deploys. `0` turns all four sweeps off (0 or more); then nothing collects those leftovers. |
| `LAKANA_RUN__INDEX_URL` | unset | An extra package index for the build (passed to uv as `UV_INDEX`). A credential goes in the URL's userinfo (`https://user:token@host/simple`, redacted from logs); a URL with a query string (`?token=...`) is refused at startup. Only userinfo is redacted from logs, **a token in the path is not**. The server and the worker do not refuse such a URL: when a path segment looks like a token (16 or more letters, digits and `-_.~%+`, with a digit or a capital letter among them; the form `https://host/<token>/simple/`), they start and log one warning at startup that names this setting and not the URL. If it is a token, move it into the userinfo; if it is a project or repository name, nothing needs doing. A token shorter than 16 characters in the path is not recognised, so there is no warning and it is not redacted: do not put one there. |
| `LAKANA_RUN__HTTPS_PROXY` | unset | Proxy for the build. A credential goes in the userinfo (`http://user:password@host:3128`); a URL with a query string is refused at startup; a path segment that looks like a token is warned about at startup, as for `LAKANA_RUN__INDEX_URL`. |
| `LAKANA_RUN__NO_PROXY` | unset | Hosts that bypass the proxy. |
| `LAKANA_RUN__CA_BUNDLE` | unset | An absolute path of a PEM file the build trusts. |

The last four follow the rules of the `LAKANA_GIT__*` settings (see "Git network settings"); the
index and the proxy may carry a password, and it is redacted from errors, logs and uv's output.
Set `LAKANA_RUN__*` where the worker reads them. Related settings are
`LAKANA_RUN__DRAIN_GRACE_SECONDS` and `LAKANA_TASK_TOKENS__MAX_TTL_SECONDS`. The latter caps the
lifetime of every task token and is read by the server and by the worker: set it to the same value
on both. A cap lowered on the server only is not applied to the tokens the worker mints. A task
fails at once if the cap is below its needs (see "Failure, retry and cancel" above).

The worker needs `uv` on its `PATH` (the Lakana image ships it) and logs at startup whether it
can run Python assets, and why not. Its cache directory is `$XDG_CACHE_HOME/lakana-worker`, else
`~/.cache/lakana-worker`:

- `venvs/` holds the venvs (at most `LAKANA_RUN__VENV_KEEP`), next to one pair of empty lock
  files (`<key>.lock`, `<key>.build.lock`) per environment ever built there; they are never
  removed by the worker, and you may delete the ones of environments that no longer exist while no
  worker is running. `uv/` holds uv's own download cache. Nothing limits or cleans the uv cache: it grows with
  every package version a project locks. To reclaim the space, stop the worker and delete the `uv/`
  directory (the next build downloads what it needs again), or run `uv cache clean` as the
  worker's user with `UV_CACHE_DIR` set to that directory. A process that a task detaches from its
  process group is not killed when the task ends.
- `attempts/` holds one directory per running attempt. It is removed when the attempt ends. After
  a hard kill of the worker, the leftover is removed by the worker's sweep: at start and then every
  `LAKANA_RUN__SWEEP_INTERVAL` seconds (the same setting paces the artifact store's sweep, and `0`
  turns both off) it deletes directories here that are named like an attempt
  directory (`attempt-` and eight characters), are real directories owned by the worker's user,
  have not changed for longer than a task can run (the asset timeout ceiling, the kill grace,
  60 s and the venv build timeout) and are not locked. A worker holds a lock on its attempt
  directory while the attempt runs (it ends with the worker process), so workers that share a
  cache directory never remove each other's running attempts. The sweep never follows a link and
  leaves anything else in the directory alone, with one log line per name. A directory whose own
  mode user code set to 000 is made accessible again (mode 0700) once it is old enough to sweep,
  then removed like any other. If that fails (for example the cache is on a filesystem that refuses
  the change), it is left in place and a warning names it once per worker start: remove it as the
  worker's user. The sweep runs only between tasks, so one long task delays it.
- Upgrade every worker before you deploy Python assets, and before you deploy an artifact asset
  with a newer platform version: an older worker fails a Python task permanently. A worker that
  knows manifest versions 1 to 4 and meets a newer one refuses it for good with `the project version's
  manifest has version 5, but this worker understands versions 1 to 4; update the worker to the
  platform version that deployed it, or redeploy with this worker's version` (the numbers differ).
  Upgrade the workers first, so that no worker is older than the platform. The order and the way
  back are in [Upgrade to the release with artifacts](upgrade-to-artifacts.md).
- The settings for artifacts (`LAKANA_ARTIFACTS__*`) are in
  [`deploy-with-compose.md`](deploy-with-compose.md), "Storage for artifacts".
- The Compose bundle's setup is in [`deploy-with-compose.md`](deploy-with-compose.md), "Python assets".

## Register the repository (admin)

<!-- not tested: moved as is; sorted in the second pull request -->
```bash
lakana secret create github-token --env prod --value token=ghp_…   # per environment
lakana secret create github-token --env dev  --value token=ghp_…
lakana repo add analytics --kind https --url https://github.com/acme/analytics.git \
    --credential-secret github-token --default-ref main
```

Create the secrets first. An administrator who registers the repository binds each secret of that
name to it, in every environment they manage. Someone who may only register repositories
(`MANAGE_REPOSITORY`) can name a secret only if it is already bound to that repository; see
[Manage secrets](manage-secrets.md#bind-a-secret-to-its-use).

- `https`: the secret's `token` is sent as the password with username `oauth2`, or with
  `--credential-username` (GitLab deploy tokens have their own).
- Behind a corporate proxy, or with an internal CA? See [Git network settings](#git-network-settings).
- `ssh`: the secret's `private_key` (OpenSSH format) and `--known-hosts-file` are required;
  unknown host keys are refused.
- `local`: an absolute path under `LAKANA_REPO_ROOTS` (a JSON list, e.g. `'["/srv/repos"]'`; empty
  refuses local repositories). Local mode allows your home directory; the Compose bundle mounts
  `./repos` at `/repos`. Only committed state is deployed. Treat the roots as **trusted content
  only**: a `.git` file, `objects/info/alternates` or `commondir` inside a repository can point
  outside the roots, so never put repositories you did not create under one.

`lakana repo remove NAME --yes` deletes a repository and its deploy history, but only while no
environment has a current deployment from it: undeploy it there first.

## Deploy, dry-run, roll back

<!-- not tested: moved as is; sorted in the second pull request -->
```bash
lakana deploy --env dev [--ref main] [--dry-run]
lakana rollback --env dev --to-previous      # or --to <full sha>
lakana deploys list --env dev
lakana deployments --env dev
lakana project-version show <id> --env dev
lakana orphans --env dev                     # produced earlier, by no current deploy
```

A deploy prints the resolved SHA and a diff, and when the project has checks, a list of them under
`Checks:`, and one `warning:` line on stderr for each table that is kept after a failed check and
whose asset the deploy removes (see [Check your data](check-your-data.md); `--output json` has
them in `warnings`). The diff has: added, removed (now orphaned), adopted (the table
existed but no deployment produced it), materialization, SQL (formatting changes ignored) and
metadata changes. Jobs are listed apart from the tables, under `Jobs:`, as added, removed or
changed; a changed job names what changed: `selection` (the patterns or
`upstream`/`downstream`), `cron` (a schedule added or removed counts), `time zone` or `notify` (a
notification added, removed or pointed at another secret or format). A dry run
shows the schedule changes a deploy would make before you merge them; `--output json` has them as
`jobs_added`, `jobs_removed` and `jobs_changed` in `diff`. A reader who may not read an adopted
table sees it counted (`hidden_adopted`), not named; `project-version show` likewise drops the DAG edges into an asset whose SQL is withheld
(`hidden_edges`). Redeploying a SHA reuses its project version. Two deploys racing on one environment:
one wins, the other fails and can be retried. Two repositories racing to produce the same new
table are serialised by a per-environment advisory lock at record time (PostgreSQL): the second
fails with `table ... is produced by repository ...`. On SQLite (local mode, one worker, deploys
inline) nothing is serialised and the check is not race-proof; no race arises there. A deploy that loses a same-SHA race removes the code
tarball it uploaded when the winner stored a different object; a deploy that fails for any other
reason after uploading (a pointer conflict, the time limit) leaves its digest-named tarball under
`_artifacts/code/`. It is small, and the worker that holds the scheduler role removes it once it is
more than seven days old, no project version in any environment names it, and no deploy is queued or
running in an environment that uses the same storage root. Every `LAKANA_RUN__SWEEP_INTERVAL`
seconds (3600 by default; `0` turns the sweep off, and then nothing removes these tarballs)
it makes one pass that starts no new deletion after a minute (a storage call that hangs is not
interrupted); the worker's log says how many tarballs and bytes it removed, and how many storage
roots it skipped because a deploy was running. The sweep keeps a tarball that a project version in any
environment names by its location, or by its commit and digest, and it keeps every other file
under `_artifacts/code/`. If a tarball that a project version names is missing, the next deploy of that
commit to the same environment writes it again, provided the archive it builds has the recorded
digest; the worker's log says whether it was restored or why not. `--ref` takes a branch, a tag, `HEAD` (the server's default branch) or a full 40-character SHA,
never `refs/pull/*`, `refs/changes/*` or any other namespace; a SHA deploys only if a branch or tag of the
repository contains it. A rollback never contacts the git host.

A deploy, dry run or rollback whose worker is lost (killed, stopped for memory, or cut off from
the database, so that its heartbeat is a lease old) ends `failed` within about a minute with the defaults,
with the message `The worker running this deploy was lost, so the deploy was ended. Nothing was
activated. Deploy again.` Run the command again. Nothing was published by the lost deploy: the
project version, the deployment pointer, the status and the audit event are written in one step that
only the worker the run belongs to can take, so a deploy that was merely frozen and finishes
after it was ended changes nothing and the run stays `failed`. A worker whose heartbeat is stale
does not stop a deploy that is still running by itself: the deploy ends when the scheduler ends
it, or when it tries to finish. A worker that is alive but could not write the final status of a
deploy (the database was away) ends that deploy itself the same way, right after the next query,
deploy or task it finishes and at least every few seconds while it is idle; nothing was published
then either. The code archive that a lost deploy uploaded stays under
`_artifacts/code/` like the one of any other failed deploy, until the sweep described above removes it.

A deploy fetches the commit into a `lakana-deploy-…` directory under the worker's temp directory
and removes it when it ends. If the worker is killed, the directory stays; a worker removes such
directories that no deploy is using and that are more than 24 hours old, when it starts and then
every `LAKANA_RUN__SWEEP_INTERVAL` (`0` turns this off).

An orphan is a table that still exists, that an earlier project version in the environment produced,
and that no repository's current deployment there produces. A table moved from one repository to
another is therefore not an orphan once the new owner has deployed it.

## Run

<!-- not tested: moved as is; sorted in the second pull request -->
```bash
lakana run --env dev                                   # the whole DAG, waits, exit 0/1
lakana run --env dev --select 'gold.*' --upstream      # gold and everything it reads
lakana run --env dev --select silver.clean --downstream
lakana run --env dev --no-wait                         # print the run id and return
lakana runs list --env dev [--repo analytics] [--limit N]
lakana runs show RUN_ID --env dev
lakana runs logs RUN_ID --env dev [--task gold.daily] [--follow]
lakana runs cancel RUN_ID --env dev
lakana runs resume RUN_ID --env dev                    # failed + upstream_failed + cancelled, and their downstream
```

A run executes the deployed project version, never your working tree: one task per asset in
dependency order, in parallel where the DAG allows. After a task has written its table, the
checks of that table run, and the task ends `failed` when a blocking check fails (see
[Check your data](check-your-data.md)).

`lakana run` prints the run id first (stderr), then one plain line per task status change, then a
summary (every task; at most 50 error texts, then `+N more failed tasks` — `--output json` has
them all). `lakana runs list` returns the newest 100 runs, or `--limit N` (1–200). Exit 0 on
success; 1 when the run fails, is cancelled or `--timeout` elapses (the run keeps going; the message names `lakana runs show RUN_ID --env ENV`); 7 when a run of the repository
is already active. `--no-wait` prints only the id.

A task that has not started says why: the task table of `lakana runs show` has a `waiting_for`
column, and the line of `lakana run` for a task ends with the same sentence (for example
`Ready, and all 2 workers are busy. Queued queries and deploys are served first.`). A task that
waits to try again prints the time of the next attempt instead of `retrying after attempt n/max`.
[Why is my query or task waiting?](why-is-it-waiting.md) lists every sentence and what to do about it.

### Selection patterns

`--select` (repeatable) takes case-sensitive shell globs (`*`, `?`, `[abc]`) over dotted table
names. `*` crosses namespace dots: `gold.*` selects everything under `gold`, including nested
namespaces such as `gold.finance.daily`. A bare `gold` matches nothing; use `gold.*`. A pattern
that matches no asset is an error. `--upstream` adds every ancestor of the matched assets,
`--downstream` every descendant. Upstream assets outside the selection are taken as they are; if
one has never been built, its downstream task fails with "upstream table '<name>' has not been
built". A selected asset that reads another selected asset only through a view outside the
selection waits for that asset, and becomes `upstream_failed` if it fails.

At most one run per repository and environment is active; a second `lakana run` exits 7 and names
the active run. A failed task makes every task downstream of it `upstream_failed`;
`lakana runs resume` reruns only those, as long as the deployment still points at the same
project version (after a redeploy, start a fresh run). Each task commits atomically, so a failed run
never leaves a half-written table, but readers can see a mix of old and new tables while a run
is in progress. A SQL task whose write committed just before its worker was lost (or crashed right
after its commit) is **not** run again: when its lease runs out the platform finds the committed
write and ends the task `succeeded`, with the rows the write stored and a message that says the
worker was lost afterwards (see "Failure, retry and cancel"). The same holds when the worker is
alive but could not record the outcome after the commit, also during a drain or a cancel. The exception
is a task whose table has [checks](check-your-data.md): the platform does not end it `succeeded`
before its checks have run. It is taken again, builds nothing, and only runs its checks. A task whose write did not commit
is tried again under its policy, and an attempt that has already been superseded never commits.
`replace` rebuilds the same table and `incremental` re-reads its watermark from the table, so a
rerun after a failure of another kind is safe too. A Python task is still run at least once, not
exactly once: if its worker is lost after the function wrote a table, the task is run again, so
write with `replace` or an incremental key. (A Python task with checks that is lost after its
last attempt's write has its table set back first; one without checks keeps what it wrote.)
Starting, cancelling and resuming need `RUN`; reading needs `READ_ENVIRONMENT` (a `RUN` holder
can read the runs it starts). `GET .../runs` takes `limit` (1 to 200).

A Python asset runs as a subprocess of the worker that claimed it. While that process runs, the
same worker keeps executing queries (and nothing else: no second task, no deploy), so the queries
that load the asset's inputs are served even when there is only one worker. The task's log
(`lakana runs logs`) holds, per attempt, the output of the environment build and of your code
(`print` and tracebacks included), up to 1 MiB, with the task's token redacted. What fails, what is
retried and what the messages mean: "Python assets", "Failure, retry and cancel".

`lakana scheduler status` (admin) shows which worker holds the scheduler role and the queue.
Set `LAKANA_RUN__*` and `LAKANA_QUERY_LIMITS__*` where both the server and the workers read them (the
Compose bundle's shared `x-lakana-env` block).

Two settings limit memory. `LAKANA_QUERY_LIMITS__MEMORY_LIMIT` (about 1 GB) is DuckDB's target for
one query; some operations go beyond it. `LAKANA_WORKER__MEMORY_LIMIT` (4 GB, `off` to disable,
at least 512 MB) is the stop for the whole worker: it covers the worker, its queries and the Python tasks
it started. On Linux, a worker over it first ends the largest running query or Python task: a
query fails with the cause `out_of_memory`, a task's attempt fails and is retried (see "Failure,
retry and cancel"); the worker and everything else go on. Only if the worker is still
over the limit after that, or nothing it can end was running, does it stop. It is then restarted, and the
query or task it was running is marked failed with the cause `out_of_memory` (see "Failure, retry
and cancel" for what happens to a task). On macOS the worker always stops, without ending a query first.
A sort or aggregation over DuckDB's limit fails as out of memory too (`the query ran out of memory and cannot spill to disk`, cause `out_of_memory`): DuckDB cannot spill to disk where the local file system is restricted, which is every deployment but local `file://` storage. Keep the first well below the second. Sizes in the second are binary (`GB` is 1024 MB); in
the first, `GB` is decimal. Linux measures the worker and its tasks, macOS only the worker process,
and Windows nothing. In the Compose bundle, set `LAKANA_WORKER_MEMORY` in `.env` instead
([`deploy-with-compose.md`](deploy-with-compose.md#memory-limits), "Memory limits"); local mode
reads the environment variable.

**Polars inside a task.** Polars 2 can write intermediate results to disk instead of holding them in
memory ("spilling"). Left alone, it decides from the memory of the machine, and into a directory
under `/var/tmp` that every task shares and nothing removes. Inside a task the machine's figure
would be wrong: Polars reads the machine's total memory, not the worker's limit or the
container's, so on a large host the stop of `LAKANA_WORKER__MEMORY_LIMIT` could come first. The
worker therefore asks Polars for these things when it starts a task:

- `POLARS_OOC_SPILL_DIR` is a directory of the task's own in its private temporary directory,
  deleted with the attempt. It is set always, also with the limit `off`.
- With a memory limit, `POLARS_OVERRIDE_TOTAL_MEMORY_MB` is that limit, so Polars takes it for the
  memory of the machine (its spilling starts from a fraction of that figure; Lakana does not
  change the fraction), and `POLARS_OOC_DISK_BUDGET_MB` is the same number of megabytes, which
  asks Polars to keep what it writes to disk within it.
- With the limit `off`, the two sizes are not set and Polars keeps its own defaults for them.

These are requests to Polars, which Lakana passes and does not enforce: what stops a task is
the worker's memory limit. The task's own Python objects and Arrow copies count toward the limit
but not toward Polars' budget, so a task that holds a large input as well can still reach the
limit: keep inputs narrow. Polars 1 does not spill in this way and ignores the settings.

| Setting | Default | Meaning |
|---|---|---|
| `LAKANA_RUN__MAX_RETRIES` | 5 | Most retries a project may ask for per asset; a deploy asking for more fails validation. |
| `LAKANA_RUN__SCHEDULER_INTERVAL_SECONDS` | 1 | How often the worker holding the scheduler role ticks (readiness, backoff, lost leases, cancellation, finalization). It also paces the sweep that ends the queries and deploys of lost workers, so keep it well below half of `LAKANA_RUN__LEASE_SECONDS`. |
| `LAKANA_RUN__LEASE_SECONDS` | 60 | How long a claimed task's lease lasts without renewal, and how old a worker's heartbeat may be before the worker counts as lost. A worker that stops renewing is declared lost and its task retried, unless its write had committed (then the task is `succeeded`); the query runs it held are ended (`FAILED` with the cause `worker_lost`, or `SUCCEEDED` for a write that had committed). The server and every worker must read the same value. Lease times are the database's clock, never a worker's. |
| `LAKANA_RUN__RENEW_SECONDS` | 20 | How often a worker renews its lease and writes its heartbeat; must be below the lease length. |
| `LAKANA_RUN__DRAIN_GRACE_SECONDS` | 20 | After SIGTERM a worker claims nothing new and lets its running task finish for this long, then releases it to another worker (not counted as an attempt). A Python asset's process is asked to stop at that point and gets `LAKANA_RUN__KILL_GRACE` (10 s) to exit. Keep the container's stop grace period above the two together plus a few seconds. |

## Git network settings

The worker runs git with a minimal environment, so its own `HTTPS_PROXY`, `NO_PROXY` and CA
variables are **not** seen by git. For `https` repositories, set these on the **worker only**
(`LAKANA_GIT__*`); the server and `migrate` need neither the variables nor the CA file. They load
the same settings and check only the format, the worker checks the file at startup and refuses
to start if it cannot read it.

| Setting | Meaning |
|---|---|
| `LAKANA_GIT__HTTPS_PROXY` | `http://` or `https://` proxy URL; git tunnels through it (`CONNECT`). Credentials go in the URL as percent-encoded `user:password` (`http://svc:p%40ss@proxy.corp:3128`); a username without a password, or an unencoded `@`, is refused. They are redacted from errors and logs. A proxy URL with a query string is refused: put the credential in the userinfo. A path segment that looks like a token is warned about at startup, never refused: only userinfo is redacted from logs. For an `https://` proxy the CA bundle is used for the proxy too. |
| `LAKANA_GIT__NO_PROXY` | Comma-separated hosts, domains or CIDRs that bypass the proxy (passed as git's `no_proxy`). |
| `LAKANA_GIT__CA_BUNDLE` | Absolute path of a PEM file of trusted CAs, for an internal GitLab with a private CA. It **replaces** the system store for git, so include every CA your repositories need. Must exist and be readable in the worker. |

Nothing else is inherited. SSH repositories ignore all three; `ProxyCommand` is not supported
(use an `https` repository, or a host reachable directly). A proxy password is visible to git
as command-scope config for the duration of a fetch, like the repository URL.

HTTPS repositories need git 2.47.2 or later (or a distribution build carrying the
CVE-2024-50349 fix): the worker answers only git's exact credential prompts, which older git
prints with the username unencoded. On an older git, a username with a reserved character such
as `gitlab+deploy-token-1` fails with an authentication error. The Lakana image ships a current
git; this matters for local mode, where your own git runs.

## Webhook network settings

The worker sends a webhook only to a **public** `https` address on port 443. It looks up the name
once, refuses the request if any address is private, loopback, link-local (including the cloud
metadata address `169.254.169.254`), shared (`100.64.0.0/10`), multicast, reserved or an IPv6
form of one of those, and then connects to the address it checked, so a changed DNS answer
cannot move the request. It follows no redirect (a redirect is a permanent failure) and ignores
`HTTPS_PROXY` and the other environment variables. The URL is never written to a log, a
delivery row or an error: a failure shows a fixed reason such as `timeout` or `TLS error`. An
address it refuses shows `the webhook host is a private or internal address`.

Set these on the **worker only** (`LAKANA_WEBHOOK__*`); the server and `migrate` load the same
settings and check only the format.

| Setting | Meaning |
|---|---|
| `LAKANA_WEBHOOK__ALLOW_HOSTS` | Comma-separated host names, or `*.domain` for every subdomain of a domain (not the domain itself), that skip the address check and may use `http` and any port, for a chat server inside your network (`chat.corp.example,*.hooks.corp.example`). Case and a trailing dot do not matter. An allowed name skips **every** address check, so list only names whose DNS you control and never a `*.domain` where others can create records. An IP address cannot be listed. |
| `LAKANA_WEBHOOK__PROXY` | `http://` or `https://` proxy URL; the webhook tunnels through it (`CONNECT`). The worker still checks the destination name, but the proxy resolves it again, so the **proxy is trusted** to refuse internal addresses: deny private, loopback, link-local and `100.64.0.0/10` destinations in its access rules (in squid, an `acl ... dst` list with `http_access deny`). The worker must still resolve the name itself. An `https` proxy is verified with the CA bundle below. Without this setting there is no proxy, whatever `HTTPS_PROXY` says. Credentials go in the URL as for `LAKANA_GIT__HTTPS_PROXY`. |
| `LAKANA_WEBHOOK__CA_BUNDLE` | Absolute path of a PEM file of trusted CAs, for an internal server with a private CA. It **replaces** the built-in store and never disables verification. Must exist and be readable in the worker. |

## Undeploy

<!-- not tested: moved as is; sorted in the second pull request -->
```bash
lakana undeploy analytics --env dev --yes
```

Removes the deployment pointer and the run principal binding (needs `DEPLOY`); refused while a run
of the repository is queued or running there. Data is untouched: the repository's tables become
orphans (`lakana orphans`), and its project versions stay as history.

An environment cannot be deleted while a repository is deployed in it: `lakana env delete ENV`
answers 409, naming the deployed repositories. Undeploy each one first, then delete.

## CI deploys to protected environments

"Only CI deploys to prod" is a grant: give `DEPLOY` on `prod` to a CI service principal and to no
one else. The `ADMIN` role holds every action, `DEPLOY` included, so the bootstrap admin and any
principal with `ADMIN` on the instance or on `prod` can still deploy to prod from a laptop; the
guarantee holds only while no human holds `ADMIN` there. Use **two** principals, because a pull
request runs the workflow file from its own branch: anyone who can push a branch can edit it, so
whatever token a PR job sees must be worthless on prod.

<!-- not tested: moved as is; sorted in the second pull request -->
```bash
lakana principal create ci-dev  --type service-account
lakana principal create ci-prod --type service-account
lakana token issue ci-dev github-dev                # CI secret LAKANA_TOKEN_DEV
lakana token issue ci-prod github-prod              # CI secret LAKANA_TOKEN (GitHub) or
                                                    # LAKANA_TOKEN_PROD (GitLab), prod only
lakana grant create --principal ci-dev  --action DEPLOY --kind ENVIRONMENT --env dev
lakana grant create --principal ci-prod --action DEPLOY --kind ENVIRONMENT --env prod
lakana grant create --principal ci-dev  --role VIEWER --kind ENVIRONMENT --env dev    # read inputs
lakana grant create --principal ci-prod --role VIEWER --kind ENVIRONMENT --env prod
```

The deploying principal must be able to read every external input, hence the VIEWER grants.
`DEPLOY` is per environment, not per repository: a principal holding it on `prod` can deploy any
registered repository there.

The generated CI files dry-run pull/merge requests against `dev` with `LAKANA_TOKEN_DEV` and deploy
`main` to `prod` with the prod token; both read `LAKANA_URL` from CI secrets. Every `lakana deploy`
in them passes `--timeout 1800`, so a deploy that has not ended after 30 minutes fails the job (exit 1)
instead of holding it until the runner's own limit.

The job that holds the prod token installs `lakana` from one index, not from whichever index pip
happens to know. Both CI files install with
`pip install --index-url "${LAKANA_INDEX_URL:-https://pypi.org/simple}" "lakana==<version>"`:
from PyPI when the variable `LAKANA_INDEX_URL` is unset or empty. To use a mirror, or a build that
is not published, set `LAKANA_INDEX_URL` to an index you control that hosts that version of
`lakana` and mirrors its dependencies. Never switch to `--extra-index-url` next to PyPI: pip
takes the name from whichever index offers it.

The GitHub workflow pins each action (`actions/checkout`, `actions/setup-python`) to a full commit
SHA with the release in a trailing `# vX.Y.Z` comment, since a mutable tag in the job that holds
the prod token is a supply-chain hole; bump them by hand or with Dependabot. The pinned actions run on node24 and need runner
2.327.1 or newer; older self-hosted and GitHub Enterprise Server runners fail on them.

- **GitHub:** put `LAKANA_URL` and `LAKANA_TOKEN_DEV` in repository secrets. Only for a mirror, put
  `LAKANA_INDEX_URL` in repository variables (if the URL carries credentials, make it a secret and
  change `vars.` to `secrets.` in the workflow). Create the `prod` environment, restrict it to the `main` branch (and
  optionally required reviewers), and store `LAKANA_TOKEN` as an *environment* secret there. The
  workflow has `contents: read` only and serialises deploys per target. Fork pull requests get no
  secrets, so their dry-run is skipped.
- **GitLab:** protect `main` and no other branch, then add `LAKANA_URL`,
  `LAKANA_TOKEN_DEV` (masked) and `LAKANA_TOKEN_PROD` as a variable that is both *masked* and
  *protected*; add `LAKANA_INDEX_URL` only for a mirror. GitLab exposes a protected variable to every pipeline on a protected branch or tag,
  including a merge request pipeline whose source branch is protected, so keep the protected set to
  `main`. Each job maps its own variable to `LAKANA_TOKEN`; never define a project variable named
  `LAKANA_TOKEN`, because project variables override a job's `variables:`. The prod job uses a
  `resource_group` so deploys do not race.

A DEPLOY-only principal can poll its own deploy run, so `lakana deploy` prints
`Queued deploy <id>` and then the result; it cannot read manifests, deploy history or orphans
unless it also holds read access to the environment. OIDC token exchange instead of a stored token
is tracked in #15.

## Run principals

In a protected environment a run writes as the service account bound to the deployment — never as
whoever triggered it:

<!-- not tested: moved as is; sorted in the second pull request -->
```bash
lakana deployments set-run-as analytics --env prod --principal etl-prod
lakana deployments --env prod            # shows run_as per repository
lakana deployments set-run-as analytics --env prod --clear
```

Binding needs `MANAGE_ENVIRONMENT`; the principal must be an active service account (an unknown
name and a user both answer 404 "not found or not a service account"). Clearing a deployment
that has no binding changes nothing and is not audited. The binding
belongs to the deployment and survives deploys and rollbacks; a running run keeps the principal it
started with. Grant the run principal what its assets need — `READ_TABLE` on its inputs,
`CREATE_NAMESPACE`, `CREATE_TABLE` and `WRITE_TABLE` where it writes — and nothing else. In an
unprotected environment a run writes as the bound principal if one is set, otherwise as the
caller.

## Protected environments: lockdown and break-glass

In a protected environment (`lakana env protect prod`) only runs of the deployed project version write.
Every other write — `lakana sql … --into`, `lakana table create|append|replace|delete`,
`lakana ns create|delete`, an SDK write, a PyIceberg commit, or one from another engine, through the Iceberg REST
catalog — is refused with 403 "Environment 'prod' is protected: only runs of the deployed
project version may write. See `lakana run`.", even when the caller's grants allow the write (a caller
without them gets the ordinary 403 or 404 first). Reads, grants, policies,
secrets, repositories, deployments and table maintenance are unaffected.

For an incident, a holder of `MANAGE_ENVIRONMENT` can make exactly one write anyway:

<!-- not tested: moved as is; sorted in the second pull request -->
```bash
lakana table append bronze.events fix.csv --env prod --break-glass --reason "INC-42: replay lost rows"
```

The reason (10–500 characters on one line, no control, invisible formatting or line-separator characters) is recorded in the audit log as `BREAK_GLASS_WRITE` with the
principal, operation and table; grants and policies still apply. The Iceberg REST catalog has no
break-glass. The next run of the deployed code may overwrite the fix: real fixes go through a
deploy.

