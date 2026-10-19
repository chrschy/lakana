# Is my instance healthy?

This guide is for the person who runs a Lakana instance. It lists what to look at to tell
whether the instance works, the command or address that answers each question, and what a bad
answer looks like. The commands are for the Compose bundle ([Deploying with Compose](deploy-with-compose.md));
`lakana worker list` and the others also work against any other instance you have an
administrator's access to.

After this guide you can check the services, the workers, the queue, the catalog database, the
object storage and the disk, you can read the one line a stuck or failed run gives you, you know
which lines of the logs matter, and you know what to collect before you ask a question in public.

## The short version

Run these in order. The first that looks wrong is where to read on.

1. `docker compose ps --all`: every service is `running` and `healthy`, and the one-off services
   (`migrate`, `bucket`, `setup`) have `Exited (0)`. ([The services](#the-services))
2. The address `/healthz` answers. ([The health route](#the-health-route))
3. `lakana worker list`: as many workers are `alive` as you started. ([The workers](#the-workers))
4. `lakana query list --env dev --status queued` does not grow. ([The queue](#the-queue))
5. The disks have room. ([Disk space](#disk-space))

Nothing here is a service level, and none of it replaces looking at what your own people notice:
a dashboard that is slow or a job that did not run is a signal too.

## The services

From the directory of the bundle:

<!-- not tested: asks Docker Compose about the running stack, and the example runner has no Docker -->
```bash
docker compose ps --all
```

What each line tells you:

| Service | Healthy means | It does not mean |
|---|---|---|
| `postgres` | The database accepts connections on its network address. | That the catalog inside it is intact, or that it has room left. |
| `seaweedfs-core` | The storage's master answers and its file service accepts connections. | That the volume has room left. |
| `seaweedfs` | The S3 gateway, the only way to the data, answers HTTP. | That the credentials and the `lakana` bucket work. |
| `server` | The server answers `/healthz` (next section). | That the database, the storage or any worker works. |
| `worker` (and `worker-interactive`, `worker-pipeline`) | The worker's process is alive. | That it has reached the database, registered, or takes work. |

The one-off services `migrate`, `bucket` and `setup` run once and end; `Exited (0)` is their
healthy state. One that exited with another number is the reason the services behind it did not
start: read its log with `docker compose logs migrate` (or `bucket`, `setup`). A service that
restarts again and again shows `Restarting`; its log says why.

## The health route

The server has one health route, `/healthz`. It needs no sign-in, so a script, a load balancer or
Docker can ask it:

<!-- source: docs/examples/getting-started/run.sh#healthz -->
```bash
curl -s "$URL/healthz"
```

`$URL` is the public address of the instance. It answers:

<!-- output -->
```text
{"status":"ok"}
```

That proves one thing: the server process is running and answers HTTP. It does not look at the
database, the storage or the workers, so a server that answers can still fail every query. The
image's own check asks the same route from inside the container every 10 seconds, which is what
the `healthy` of the `server` service in `docker compose ps` comes from. There is no second route
that checks the database or the storage; the rest of this page is how you check them.

A server that does not answer at all is a stopped or restarting container, a proxy or a firewall
in front of it, or a wrong address. `docker compose ps` and `docker compose logs server` tell
which.

## The workers

A worker is the process that runs queries and tasks. It reports in to the database every few
seconds. It is `alive` while its last report is recent, and `lost` once it has been silent for
60 seconds (`LAKANA_RUN__LEASE_SECONDS`). A lost worker's row stays for an hour, and what it was
running is ended by the worker that holds the scheduler role. The commands need the right to
manage environments on the instance, which an administrator has:

<!-- source: docs/examples/getting-started/run.sh#worker-list -->
```bash
lakana worker list
```

<!-- source: docs/examples/getting-started/run.sh#scheduler-status -->
```bash
lakana scheduler status
```

Healthy looks like this:

- **As many `alive` rows as you started.** `LAKANA_WORKERS` plus `LAKANA_WORKERS_INTERACTIVE`
  plus `LAKANA_WORKERS_PIPELINE` in the bundle's `.env`. Fewer means a worker did not start or
  cannot reach the database; a row with the status `lost` is one that stopped reporting.
- **Every kind of work you use has an `alive` worker.** The column `takes` says `all`,
  `interactive` or `pipeline`. See [Seeing that a kind has no worker](deploy-with-compose.md#seeing-that-a-kind-has-no-worker).
- **The workers take work.** The column `doing` shows `idle` or the query, deploy or task a worker
  runs. An `alive` worker that stays `idle` while queries wait is the case to look into: the
  reason of each waiting query says why (see [A stuck run](#a-stuck-run-and-why)).
- **One worker holds the scheduler role.** `lakana scheduler status` names it, and when it last
  reported. The role ends what lost workers held and runs the hourly sweep, so a holder that has
  not reported for much longer than a minute, or no holder, is a problem:
  [When a worker is lost](why-is-it-waiting.md#when-a-worker-is-lost) says what frees it.

An empty list prints `No worker is registered.` followed by what to do. With the Compose bundle
that is `docker compose logs worker` first.

## The queue

A healthy instance has queries and runs that start soon after they are submitted. Two listings
show what waits; run them now and again after a few minutes. A queue that is short and changes is
normal; one that only grows is not.

<!-- source: docs/examples/getting-started/run.sh#queue-length -->
```bash
lakana query list --env dev --status queued
```

<!-- source: docs/examples/getting-started/run.sh#open-runs -->
```bash
lakana runs list --env dev --status queued,running
```

Use the name of your own environment instead of `dev`. You see your own queries; with
`MANAGE_ENVIRONMENT` on the environment you see everyone's. The `waiting_for` column of the query
listing gives the reason each one waits. To see what failed in the last day, use this listing (on macOS, write `date -u -v-1d +%Y-%m-%dT%H:%M:%SZ`
instead of the `date` command shown):

<!-- source: docs/examples/getting-started/run.sh#find-failed-run -->
```bash
lakana runs list --env dev --status failed --since "$(date -u -d '1 day ago' +%Y-%m-%dT%H:%M:%SZ)"
```

More filters and paging: [Find a run, a query or a deploy](find-a-run.md).

## The catalog database and the object storage

Two things the instance cannot work without. Neither has a command of its own; you read them from
what else works.

**The catalog database.** `docker compose ps` shows `postgres` as `healthy` when it accepts
connections. A command that reads the catalog, such as `lakana worker list`, answers only when
the server reaches the database, so an answer proves it. When the database goes away:

- every request that needs it fails, and a new server container does not start (the `migrate`
  service runs first and exits with an error);
- each worker logs `The worker's heartbeat could not be written; it is tried again every 5 s.`
  and, after a minute without success, `No heartbeat was written for 60 s (LAKANA_RUN__LEASE_SECONDS): this worker claims nothing and stops the query it runs until one is written. Check that the database is reachable.`
  Its queries and deploys are then ended as lost. When the database is back, it logs
  `The worker's heartbeat is fresh again; it claims work again.`

**The object storage.** `seaweedfs` being `healthy` shows the gateway answers. Whether the
instance can read and write its data you only learn by doing it: run a query that reads a table
of yours. The tutorial's query is an example; use a table you have:

<!-- source: docs/examples/getting-started/run.sh#query -->
```bash
lakana sql "SELECT * FROM gold.station_ranking ORDER BY warmth_rank"
```

When the storage cannot be reached, a query that reads a table fails instead of returning rows,
and a request that stores or sends an artifact file answers that the storage could not be written
or read and that the operator should check whether it is unreachable. Read `docker compose logs
seaweedfs` and `docker compose logs seaweedfs-core`.

## Disk space

Lakana itself reports no disk use and sends no alert, so look at the disk yourself. The bundle
keeps its state in three named Docker volumes, all on the disk that holds Docker's data:

| Volume | Holds | What fills it |
|---|---|---|
| `pgdata` | The catalog (PostgreSQL). | Table, run and audit history. |
| `seaweed` | The data of every table and every artifact. | Your data. |
| `worker-cache` | At `/home/lakana/.cache/lakana-worker`: the Python environments of the projects and the download cache of `uv`, and the working directories of the tasks that run. | Every package version a project locks; **the download cache is never trimmed**. |

The worker containers also keep temporary files under `/var/tmp/lakana`, inside the container.
The sweep removes what a worker that was killed left behind, once an hour
(`LAKANA_RUN__SWEEP_INTERVAL`).

<!-- not tested: asks Docker for the disk use of a running stack, and the example runner has no Docker -->
```bash
docker system df -v
docker compose exec worker df -h /home/lakana/.cache/lakana-worker
```

The first command lists every volume with its size. The second shows how full the disk under
the workers' cache is. Also look at the disk of the machine itself with `df -h` on the host.
How to reclaim the download cache of the workers is in [Python assets](deploy-with-compose.md#python-assets).
The documentation gives no figure for how much space your data needs; measure on your own data.

## A stuck run and why

A query or a task that has not started says why, in one sentence, and a task that failed has a
code, a cause and a next step. Both are shown where you look at the run:

- **Waiting.** `lakana query status QUERY_ID --env dev` shows `waiting_for` for a queued query,
  and `lakana runs show RUN_ID --env dev` shows it for each task that waits. Most reasons pass by
  themselves. The ones that need you are `no_worker` and `no_worker_of_kind` (start a worker),
  and a reason that does not change for a long time. All of them, and what to do, are in
  [Why is my query or task waiting?](why-is-it-waiting.md).
- **Failed.** `lakana runs show RUN_ID --env dev` shows the code of each failed task and under the
  table the cause and `Next:`. The codes that point at the instance, rather than at a project, are
  these; all of them are in [Why did it fail?](../reference/why-did-it-fail.md):

| Code | What it says about the instance |
|---|---|
| `worker_lost` | The worker running the task stopped before it finished: it was killed, lost power or its network, or was stopped for memory. Look at the worker's log and memory limit. |
| `memory` | The task used more memory than the worker allows. Raise `LAKANA_WORKER_MEMORY` or make the task handle less data: [Memory limits](deploy-with-compose.md#memory-limits). |
| `no_runtime` | This worker cannot run this kind of task, for example because it has no Python runtime. The detail names what is missing. |
| `code_changed` | The deployed code is missing or no longer matches what was deployed. Deploy the project again; if it repeats, check the storage. |
| `internal` | The platform failed in a way it did not expect. The worker's log has the traceback. |

## Log lines that matter

Lakana's own records go to standard error, one per line with the time, the level and the logger
name; the web server's lines (startup and, if switched on, one per request) have a shorter form
without the time. `docker compose logs` shows both together. Read them with `docker compose logs server` and `docker compose logs worker`; the default
level, `INFO`, shows all of the lines below. How to change the level and what the lines of the
sweep mean: [The sweep](deploy-with-compose.md#the-sweep) and
[After a restore](deploy-with-compose.md#after-a-restore).

| Where | The line starts | What it means and what to do |
|---|---|---|
| worker | `Memory limit exceeded: the worker and its tasks use` | The worker passed 90 percent of its memory limit and ended its largest query or task, or stopped itself. Narrow the work or raise `LAKANA_WORKER_MEMORY`: [Memory limits](deploy-with-compose.md#memory-limits). |
| worker | `The worker's heartbeat could not be written` | The worker cannot reach the database (see above). `The worker's heartbeat is still failing` repeats it, and `No heartbeat was written for` says the worker has stopped claiming work. |
| worker | `Task run <id> is no longer ours; its outcome is dropped.` | The worker's claim on a task had run out when the task finished, so the worker dropped the result. Look for `worker_lost` on the run. |
| worker | `Lease renewal of task run <id> failed; retrying.` | The worker could not extend its claim on a task. If it keeps failing, the task is taken from it. |
| worker | `Polling iteration failed; continuing.` | The worker's loop that looks for work hit an error, followed by the error itself. Occasional lines are harmless; a stream of them is not. |
| worker | `Running task run <id> failed unexpectedly.`, `Deploy run <id> failed unexpectedly.` | The platform failed in a way it did not expect. The run shows the code `internal` and says to see the worker log; the traceback follows the line. |
| worker | `Python assets: disabled:` | At start-up: this worker cannot run Python tasks; the line ends with `To enable it:` and the fix. |
| server | `The grants that have no effect could not be listed at start-up (a database error)` | The database answered with an error while the server started. Start-up goes on; check the database. |
| server | A start-up that stops with a message that names a setting | A setting has a value the program does not accept. The message names it; correct it in `compose.yaml` or `.env`. |
| server | An access line that ends `500 Internal Server Error` or `503` | The server could not answer a request (a 503 asks the client to try again, for example when the storage could not be opened). The lines around it have the cause. |

## What to collect before you ask

Questions and bug reports go on a public page ([Support](../explanation/support.md)), where
anyone can read them, so collect what lets a stranger understand your case and nothing that lets
a stranger into your instance. In this order:

**1. The versions.** The CLI's and the instance's:

<!-- source: docs/examples/getting-started/run.sh#version -->
```bash
lakana --version
```

**2. The state of the services.** The output of `docker compose ps --all`, and the image you run:
the `LAKANA_IMAGE` line of `.env`.

**3. The failing run or query.** Its id and, for a run, the code from `lakana runs show`. Not the
SQL or the data if either is private.

**4. The relevant log lines**, with secrets removed. This writes the last hour of both logs to a
file and replaces two kinds of secret: an access token (it starts `lakana_pat_`; the server never
logs one itself, but a tool or a person may have pasted one), and a password in a connection
address:

<!-- not tested: reads the logs of a running Docker Compose stack, and the example runner has no Docker -->
```bash
docker compose logs --no-color --since 1h server worker \
  | sed -E -e 's/lakana_pat_[^[:space:]]+/lakana_pat_REDACTED/g' \
           -e 's#(://[^:/@[:space:]]+:)[^@[:space:]]+@#\1REDACTED@#g' > lakana-logs.txt
```

Open the file and read it before you post it: the filter knows two patterns, not your data. A
table name, a file path, a person's name or a SQL statement can be in a line, and a package index
address can carry a credential (`user:token@` in its address). Cut a line or replace a word when
in doubt.

Never post the `.env` file, the output of `docker compose config` (it prints the values of
`.env`), a token, the master key, or the storage keys. If you posted one by mistake, treat it as
public: replace the token ([Give a second person access](give-a-second-person-access.md#take-access-away)),
and for the master key and the storage keys start with [Manage secrets](manage-secrets.md).

Lakana is run by one maintainer, and a question gets an answer when time allows or not at all;
[Support](../explanation/support.md) says where to ask and what the project does and does not
provide. A report with these four things in it is one that can be understood without asking
you for more.

## See also

- [Run Lakana for other people](operate-for-others.md): the whole job of an operator, in order.
- [Why is my query or task waiting?](why-is-it-waiting.md) and [Why did it fail?](../reference/why-did-it-fail.md).
- [Deploying with Compose](deploy-with-compose.md): the services, the memory limit, the sweep and the logs.
- [The audit log](../explanation/security.md#what-the-audit-log-records): who did what.
