# Getting started

At the end of this tutorial you will have a Lakana instance running on your own machine, two
tables built by a project you deployed, and one query answered from those tables. You do not need
Docker or a database server.

Lakana is pre-beta: the API and the data layout may change between 0.x releases, so do not put
production data in it yet. It has no web interface yet: everything in this tutorial is done in a
terminal with the CLI, and the interface is planned after the public beta.

You need Python 3.11 or newer (on macOS: Python 3.13 or newer) and git. With an older Python on macOS, `lakana server start --local` stops before it starts anything and says so. Once Lakana is installed, plan on ten minutes.

## Install

Lakana is one Python package, published on PyPI as a pre-beta (0.x). The `server` extra adds what
a local instance needs:

<!-- not tested: installs from PyPI -->
```bash
pip install "lakana[server]"
```

To work on Lakana itself, or to try the development version, install from a checkout of the
repository instead. You need [`uv`](https://docs.astral.sh/uv/), a Python package manager:

<!-- not tested: installs from a checkout -->
```bash
git clone https://github.com/chrschy/lakana.git
cd lakana
uv sync --all-packages
source .venv/bin/activate
```

The last line puts the `lakana` command on your path, for this terminal only. Run it again in
every new terminal you open below, from inside the checkout. A checkout is a development build:
at the end of its output, `lakana init` prints a note about pinned versions for CI. You can
ignore it in this tutorial.

## Start a local instance

A **local instance** is a complete Lakana on your machine: the server, a worker that runs your
queries and builds, and a place to keep tables. Start it:

<!-- source: docs/examples/getting-started/start.sh#start -->
```bash
lakana server start --local
```

The instance stays in the foreground and prints a log line for each request it handles. When it
is ready, it prints a line like this one:

<!-- output -->
```text
Lakana is running at http://127.0.0.1:8765 — profile 'local', environment 'dev'. Try: lakana sql "SELECT 42"
```

The other lines are normal and need no action. They include one line per request and a line
about a restored token. The ready line may have scrolled up; look for it. To see fewer of them,
start with `LAKANA_LOG__LEVEL=WARNING` in front of the command; local mode passes it to the
server and the worker.

The worker has a memory limit, 4 GB by default (`LAKANA_WORKER__MEMORY_LIMIT`; `off` turns it
off). It covers the worker and the tasks and queries it started. On Linux, a worker that goes over it ends the
largest running query or Python task first (that one fails, the worker and the rest go on); if the
worker is still over the limit after that, it stops and is started again (local mode does that, at most three times in five minutes, and prints one line), and the query or task that was running is marked failed. The worker says which limit applies
in one line at start. On Linux it measures the worker and its tasks; on macOS only the worker
process, and on Windows nothing, and then the line says the limit is not enforced. You can ignore
that line. In local mode a very fast query can still use up the machine's memory before the worker
notices; the Compose bundle adds a limit that the kernel enforces.

Leave this terminal as it is. **Open a second terminal** for the rest of the tutorial, and
activate the environment there too if you installed from a checkout.

To see that it worked, ask for the version. The command prints the version of the `lakana`
command and, because an instance is running, the version of the instance:

<!-- source: docs/examples/getting-started/run.sh#version -->
```bash
lakana --version
```

The versions, and the port in the second line, differ on your machine:

<!-- output -->
```text
lakana 0.8.1.dev6+g6af3c5d2a
instance http://127.0.0.1:8765: 0.8.1.dev6+g6af3c5d2a
```

!!! note
    If the start fails with "Port 8765 is in use.", another program uses that port. Stop that
    program and start again, or pass another port with `--port`; the commands below then find
    your instance on it. If it fails with "Lakana is already running", an instance you
    started earlier still runs; its message names the address. Use it, or stop it with Ctrl-C
    in its terminal and start again.

An **environment** is a separate place where a project is deployed and its tables live. A local
instance has one, called `dev`. Every command below that needs an environment names it with
`--env dev`.

## Create a project

A **project** is a git repository of SQL and Python files. Lakana reads it from git, so every
table you build can be traced to a commit.

A local instance only accepts projects under your home directory. The first command goes
there, so that you do not create the project in `/tmp`. (A project elsewhere is refused when you
register it, with a message that tells you to move it under your home directory.)

<!-- source: docs/examples/getting-started/run.sh#init -->
```bash
cd ~
lakana init analytics
cd analytics
```

`lakana init` lists what to do next. Yours shows your own folder, and a note about the
development build if you installed from a checkout:

<!-- output -->
```text
Created a Lakana project in /home/you/analytics.
Next:
  cd /home/you/analytics
  git init && git add -A && git commit -m 'Initial project'
  lakana repo add analytics --kind local --url /home/you/analytics
  lakana deploy --env dev
  lakana run --env dev
Local repositories must lie under LAKANA_REPO_ROOTS (local mode allows your home directory).
If 'git commit' says it does not know who you are, set a name and email first: git config --global user.name 'Your Name' && git config --global user.email you@example.com
Note: lakana 0.8.1.dev6+g6af3c5d2a is a development build, so the CI install `lakana==0.8.1.dev6+g6af3c5d2a` can't be installed from the index. Ignore this until you use CI; then edit the pinned version in .github/workflows/lakana-deploy.yml and .gitlab-ci.yml.
```

You now have a folder `analytics` with a `lakana.yaml` file, which holds the project's settings.
`lakana init` also wrote files for CI and for Python assets; this tutorial leaves them as they
are. It wrote a sample asset, too, `assets/bronze/example.sql`, and a check for it in `checks/`.
Remove both, so that your project holds only what you wrote. (The check has to go with the asset:
it names a table that your project would no longer build, and a deploy refuses such a check.)

<!-- source: docs/examples/getting-started/run.sh#remove-sample -->
```bash
rm -r assets/bronze/example.sql checks
```

## Write two assets

An **asset** is one file that describes one table. Lakana runs the file and stores the result as
the table. Its path decides the table's name: `assets/bronze/readings.sql` becomes the table
`bronze.readings`. Here `bronze` is the namespace, a folder of tables, and `readings` is the name.

Many teams sort tables into layers. **Bronze** tables hold data as it comes in. **Silver**
tables hold data that has been cleaned or summed up from bronze. Lakana does not require this;
this tutorial follows it because it is a common way to keep order.

Create the file `assets/bronze/readings.sql` with this content. It holds four temperature
readings from two weather stations, written by hand so that you need no data file.

<!-- source: docs/examples/getting-started/project/assets/bronze/readings.sql -->
```sql
/* lakana
description: Four temperature readings from two weather stations, written by hand.
columns:
  station_id: The station that took the reading.
  temp_c: The temperature in degrees Celsius.
*/
SELECT 'A1' AS station_id, 4.0 AS temp_c
UNION ALL SELECT 'A1', 6.0
UNION ALL SELECT 'B2', 1.0
UNION ALL SELECT 'B2', 3.0
```

The comment at the top is the asset's documentation: what the table holds, and what each column
means. The project's settings require a description for every asset, and Lakana refuses a deploy
without one. The rest of the file is a single `SELECT` query.

Next, create the folder `assets/silver` and the file `assets/silver/station_temperatures.sql`.
It reads `bronze.readings` and averages the temperature of each station.

<!-- source: docs/examples/getting-started/project/assets/silver/station_temperatures.sql -->
```sql
/* lakana
description: The average temperature of each station.
columns:
  station_id: The station that took the readings.
  avg_temp_c: The average temperature in degrees Celsius.
*/
SELECT station_id, AVG(temp_c) AS avg_temp_c
FROM bronze.readings
GROUP BY station_id
```

Because this asset reads `bronze.readings`, Lakana knows it must build `bronze.readings` first.
You never state the order yourself.

## Add a check

A **check** is a file that says what must be true of a table. It is one `SELECT` that returns the
rows that are wrong; a table with no wrong rows passes. Its path names the table it checks:
`checks/silver/station_temperatures/plausible_range.sql` is a check of `silver.station_temperatures`.
Create the folders and the file. It looks for a station whose average temperature is impossible,
which means a faulty sensor:

<!-- source: docs/examples/getting-started/project/checks/silver/station_temperatures/plausible_range.sql -->
```sql
/* lakana
description: An average below -90 or above 60 degrees is a sensor fault.
*/
SELECT station_id, avg_temp_c
FROM silver.station_temperatures
WHERE avg_temp_c < -90 OR avg_temp_c > 60
```

Every run tests a table after it builds it. If this check returned a row, the run would fail and
the table would be set back to what it was before. With your two stations it returns none. The
how-to guide [Check your data](../how-to/check-your-data.md) shows what a failing check looks like.

## Commit

Git can only commit if it knows who you are. If you have used git on this machine before, it
does, and you can skip to the next block. If not, `git commit` stops with a message that asks for
your name and email; `lakana init` printed a line about this too ("If 'git commit' says it does
not know who you are ..."). Set them once, with your own values:

<!-- source: docs/examples/getting-started/run.sh#git-identity -->
```bash
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

Now turn the folder into a git repository and commit your files:

<!-- source: docs/examples/getting-started/run.sh#commit -->
```bash
git init
git add -A
git commit -m "First assets"
```

A deploy reads a commit, not the files in your folder, so a change that is not committed is not
deployed. Git may also print a long hint about the name of the first branch. It is harmless.

## Deploy

To **deploy** is to give Lakana a commit of a project for one environment. Lakana checks the
project, and if the checks pass, records it as the version that runs there. The first command
tells Lakana where the repository is. The second deploys the latest commit to `dev`.

<!-- source: docs/examples/getting-started/run.sh#deploy -->
```bash
lakana repo add analytics --kind local --url "$PWD"
lakana deploy --env dev
```

You will see `Registered repository analytics`, then the deploy: its identifier, the commit it
deployed, a table of what changed and, last, a hint that names the next step. Yours looks like
this, with a different identifier and commit:

<!-- output -->
```text
Queued deploy 71ddefdc-6a57-4c5b-bac3-cbacc2fb4759
Deployed analytics in dev at ca36c93fe2337b324df3ab8d03cac9b9d1ed1420
┏━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ change ┃ table                       ┃
┡━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ added  │ bronze.readings             │
│ added  │ silver.station_temperatures │
└────────┴─────────────────────────────┘
Checks:
  silver.station_temperatures: plausible_range
Hint: run 'lakana run --env dev' to run what you just deployed.
```

Both tables are listed as added, but no table exists yet. A deploy only records what to
build. Under `Checks:` it lists the check it found, and the hint names the next step.

## Run

A **run** builds the tables of the deployed version, in the order their dependencies need. It
is the command the hint named:

<!-- source: docs/examples/getting-started/run.sh#run -->
```bash
lakana run --env dev
```

The command prints one line each time an asset changes state and waits until the run is done.
It ends with a summary like this one. The run identifier above it differs on your machine, and
so may the durations:

<!-- output -->
```text
Run e6c654de-926d-4941-896d-909ead7cd45d succeeded
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━┳━━━━━━┳━━━━━━━┓
┃ asset                       ┃ status    ┃ attempt ┃ duration ┃ rows ┃ error ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━╇━━━━━━╇━━━━━━━┩
│ bronze.readings             │ succeeded │ 1/3     │ 0.0s     │ 4    │       │
│ silver.station_temperatures │ succeeded │ 1/3     │ 1.0s     │ 2    │       │
└─────────────────────────────┴───────────┴─────────┴──────────┴──────┴───────┘

silver.station_temperatures   passed   1 of 1 checks passed
  plausible_range             passed
```

Both assets succeeded. `bronze.readings` has four rows, one for each reading, and
`silver.station_temperatures` has two, one for each station. The last two lines are the check:
after `silver.station_temperatures` was built, `plausible_range` ran against it and passed.

## Query

Ask the silver table for the average temperature of each station:

<!-- source: docs/examples/getting-started/run.sh#query -->
```bash
lakana sql "SELECT station_id, avg_temp_c FROM silver.station_temperatures ORDER BY station_id"
```

You will see this result:

<!-- output -->
```text
┏━━━━━━━━━━━━┳━━━━━━━━━━━━┓
┃ station_id ┃ avg_temp_c ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━━┩
│ A1         │ 5.0        │
│ B2         │ 2.0        │
└────────────┴────────────┘
2 rows
```

Station A1 read 4 and 6 degrees, which average 5. Station B2 read 1 and 3, which average 2.
A column may carry the name of its table or an alias, as in any SQL: `station_temperatures.station_id`,
`silver.station_temperatures.station_id` or `t.station_id` after `FROM silver.station_temperatures AS t`.
CI runs every command on this page on every change. It checks the query result above and the
lines of the other outputs that stay the same: the tables of the deploy, and the status and row
counts of the run. Identifiers, commits, versions and durations differ on every machine and are
not checked.

## Stop the instance

Go to the first terminal and press Ctrl-C. The instance shuts down. Your tables stay where they
are, and `lakana server start --local` brings the instance back with them.

## Where next

- [Check your data](../how-to/check-your-data.md) writes more checks, shows a failing one and what
  it does to a table.
- [Projects and deploys](../how-to/projects.md) covers asset front-matter, `lakana.yaml`, Python
  assets, dry-run deploys, rollback, and selecting what to run.
- [Deploying with Compose (team mode)](../how-to/deploy-with-compose.md) moves from your machine to an
  instance a small team shares.
- [Scope and philosophy](../explanation/scope.md) says what Lakana is for, and what it is not for.
