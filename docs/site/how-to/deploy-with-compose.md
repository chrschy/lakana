# Deploying with Compose (team mode)

`lakana bundle init` generates a Docker Compose bundle that runs a complete Lakana instance —
Postgres for the catalog, SeaweedFS for object storage, the server, one or more workers, and
a one-shot setup job — on a single machine or VM, for a small team sharing one instance.
This is the "team" deployment path; for a single user on their own laptop, see local mode
(`lakana server start --local`, `packages/lakana/README.md`).

## Prerequisites

- **Docker Engine** with **Compose ≥ 2.24** (`docker compose version`).
- `pip install lakana` — the `lakana` CLI generates the bundle and is also the client you use
  against it afterwards. Lakana is pre-beta: the API and the data layout may change between 0.x
  releases.
- The image `ghcr.io/chrschy/lakana` is public; pulling it needs no login.

## Quickstart

<!-- not tested: moved as is; sorted in the second pull request -->
```bash
lakana bundle init
cd lakana-deploy && docker compose up -d --wait
lakana sql "SELECT 42"
```

`lakana bundle init` writes `compose.yaml`, `.env` and an empty `repos/` (for local git
repositories, see below) into `./lakana-deploy` (override with a positional `DIRECTORY`
argument), generates fresh secrets, and saves a profile named `team`
(see "Profile name" below) so the `lakana` CLI can talk to the new instance right away.
`docker compose up -d --wait` blocks until every one-shot service (`migrate`, `bucket`,
`setup`) has actually finished, not just started, and fails if any of them exits non-zero —
`worker` depends on `setup` completing successfully, the same way `server`/`worker` already
depend on `migrate`, so `--wait` itself is the gate; there's no second command to run after
it, and none should be added (a separate `docker compose wait setup` step here used to race
`setup`'s own exit on a restart, where it sends two quick `GET`s and exits in under a
second).

## Local repositories

`lakana bundle init` creates `repos/` next to `compose.yaml`; the server and the workers see it
read-only as `/repos`, and `LAKANA_REPO_ROOTS` is `["/repos"]`. Clone or `lakana init` a project
there and register it with `lakana repo add NAME --kind local --url /repos/NAME`. Only committed
state is deployed. Remote repositories (`https`, `ssh`) need no mount.

## What `init` generates

`compose.yaml` (see "The services" below), an empty `repos/` directory (mode `0755`) that
`server` and `worker` mount read-only as `/repos` with `LAKANA_REPO_ROOTS='["/repos"]'`, and
`.env` (mode `0600`, since it holds every secret this deployment has):

| Key | Purpose |
|---|---|
| `LAKANA_IMAGE` | The Lakana image reference (`ghcr.io/chrschy/lakana:<version>` by default — see "Upgrading" and "`--image`" below). |
| `LAKANA_BIND` | The host address the server's port is published on (see "Exposing it" below). |
| `LAKANA_PORT` | The host port the server is published on. |
| `LAKANA_WORKERS` | Worker replica count, used by `deploy: {replicas: ${LAKANA_WORKERS}}`. |
| `LAKANA_WORKERS_INTERACTIVE` | Replicas of `worker-interactive`, workers that take ad-hoc queries only (`0`: none). A bundle whose `.env` predates the key starts none. See "Separate workers for ad-hoc queries and pipelines" below. |
| `LAKANA_WORKERS_PIPELINE` | Replicas of `worker-pipeline`, workers that take pipeline work only: deploys, tasks, maintenance (`0`: none). A bundle whose `.env` predates the key starts none. |
| `LAKANA_WORKER_MEMORY` | The memory limit of each worker container, of all three kinds (`4g`). The worker stops itself at 90 percent of it. See "Memory limits" below. |
| `POSTGRES_PASSWORD` | The `lakana` Postgres user's password (also the catalog's connection string). |
| `LAKANA_S3_ACCESS_KEY` / `LAKANA_S3_SECRET_KEY` | SeaweedFS's S3-compatible credentials, used by both SeaweedFS's own identity config and the `setup` job that binds them to the `dev` environment. |
| `LAKANA_SECRETS__MASTER_KEY` | The envelope-encryption master key (`lakana_core.secrets`) — wraps every secret's own per-secret data key. Losing it makes every stored secret unrecoverable. |
| `LAKANA_BOOTSTRAP_PAT_KEY_ID` / `LAKANA_BOOTSTRAP_PAT_SECRET` | The bootstrap admin credential (`lakana_pat_<key id>_<secret>`), saved into the `team` profile so the CLI can authenticate immediately. |

**Why `init` refuses to overwrite an existing bundle:** re-running it into a directory that
already has a `compose.yaml` or `.env` would generate a *new* `LAKANA_SECRETS__MASTER_KEY`,
silently locking you out of every secret already encrypted under the old one. `init` checks
both files (a dangling symlink counts as existing) before writing anything, and refuses
(exit 2) rather than risk that. If a run fails partway through — writing `.env` or
`compose.yaml`, or saving the profile afterwards — it removes whatever it already created,
including the directory if it created that too, so a retry starts clean instead of tripping
this same refusal over debris from a run that never produced a working deployment. A
filesystem error unrelated to a pre-existing bundle (the directory can't be created or
written to) exits 3 instead, naming the path.

**Profile name.** The saved profile's name comes from the CLI's global `--profile` option
(default `team`), not from a `bundle init`-specific flag — `lakana --profile NAME bundle init`
names it `NAME` instead. `init` refuses (exit 2) if a profile of that name already exists,
checked *after* the bundle-existence check above, so whichever conflict is actually relevant
to the target directory is the one you see.

**`--image`.** A caller-supplied `--image` is validated against the characters safe in
`.env` and a Compose file (letters, digits, and `._/:@-`) — anything else, including quotes,
`$`, `#`, or whitespace, is refused (exit 2) before it can corrupt either generated file. A
development or local build of the CLI (a version `hatch-vcs` derives as a dev snapshot, not a
tagged release) has no matching published image, so `--image` is required for it; a release
build defaults to `ghcr.io/chrschy/lakana:<its own version>`.

### Secrets settings

| Variable | Default | Meaning |
|---|---|---|
| `LAKANA_SECRETS__MASTER_KEY` | none | Generated by `init` (see the table above). |
| `LAKANA_SECRETS__ACCEPT_FORMAT_1` | `false` | `true` or `false`. Whether secrets stored in the old format, written by a release before the stored format was bound to its row, are read. Leave it `false`. Turn it on only while you set old secrets again after an upgrade, on the server and on every worker, and remove it afterwards: [Manage secrets](manage-secrets.md#secrets-stored-in-the-old-format) and [Upgrade to the release with artifacts](upgrade-to-artifacts.md#secrets-stored-before-this-release). |

The bundle needs the setting for none of its own secrets: its `setup` service sets the storage secret
`seaweedfs-s3` again at the next start if it is still in the old format. You act only for secrets you
created yourself.

To turn it on, add the line to the shared block of `compose.yaml` (the `server` and the `worker` both
read it) and run the `docker compose` command of the [Quickstart](#quickstart) again:

```yaml
x-lakana-env: &lakana-env
  # ... the lines that are already here stay ...
  LAKANA_SECRETS__ACCEPT_FORMAT_1: "true"
```

## The services

| Service | Image | Role | Starts after |
|---|---|---|---|
| `postgres` | `postgres:16` | Catalog metadata. | — |
| `seaweedfs-core` | `chrislusf/seaweedfs:4.47` | SeaweedFS's master, volume server and filer, holding the `seaweed` volume. Only on the internal `seaweed-internal` network (see "Known gaps"). | — |
| `seaweedfs` | `chrislusf/seaweedfs:4.47` | The S3 gateway, the only way to the object storage: `http://seaweedfs:8333`. | `seaweedfs-core` healthy |
| `bucket` | `chrislusf/seaweedfs:4.47` | One-shot: creates the `lakana` bucket if it doesn't already exist. | `seaweedfs` healthy |
| `migrate` | `${LAKANA_IMAGE}` | One-shot: applies Alembic migrations (`lakana-server migrate`) and exits. | `postgres` healthy |
| `server` | `${LAKANA_IMAGE}` | The FastAPI control plane and Iceberg REST catalog; publishes `${LAKANA_BIND}:${LAKANA_PORT}:8000`. | `migrate` completed |
| `worker` | `${LAKANA_IMAGE}` | The DuckDB query executor and dequeue loop; scaled to `LAKANA_WORKERS` replicas; takes every kind of work. | `migrate` completed |
| `worker-interactive` | `${LAKANA_IMAGE}` | The same worker with `LAKANA_WORKER__TAKES=interactive`, scaled to `LAKANA_WORKERS_INTERACTIVE` replicas (`0` by default, so no container starts). | `migrate` completed |
| `worker-pipeline` | `${LAKANA_IMAGE}` | The same worker with `LAKANA_WORKER__TAKES=pipeline`, scaled to `LAKANA_WORKERS_PIPELINE` replicas (`0` by default). | `migrate` completed |
| `setup` | `${LAKANA_IMAGE}` | One-shot: creates the `dev` environment, its storage secret, and the binding between them, entirely over the REST API (see "`setup`" below). | `server` healthy and `bucket` completed |

The server and every worker connect to `postgres` directly, and should keep doing so if you point
them at another PostgreSQL. Each connection asks PostgreSQL to probe the machine behind it, so
that a worker that lost power or its network is noticed (see "Scaling workers"). Behind a
connection pooler the probes test the pooler, not the worker, and a pooler in transaction mode
also breaks the scheduler's role itself, which is a session lock. The request does nothing on a
unix socket (harmless: both ends are on one machine), and on a PostgreSQL host that is not Linux
only the keepalive settings decide, because `tcp_user_timeout` has no effect there. PostgreSQL
before version 12 refuses `tcp_user_timeout`; the warning in the log of the worker or server then
names it.

### `setup`

`setup` (`python -m lakana.cli.deploy.setup`) runs once per `docker compose up`, using the
bootstrap admin credential and the SeaweedFS S3 keys from `.env`, and does everything through
the public REST API via the SDK — it has no direct database or storage access of its own
(rule 2: the REST API is the only way in). It retries up to 30 times, 2 s apart — a bit
under a minute of retrying in total — while the server is still settling (a fresh Postgres
and a fresh server container can both take a moment after their own healthchecks pass), then
fails the container if the server never comes up.

It is idempotent: a restart re-runs it and it creates nothing a second time. If `dev` already
exists with a *different* storage root, or its storage secret is already bound to some other
name, `setup` treats it as **operator-managed** and leaves it completely untouched — no
secret created, no rebind attempted — printing a line saying so instead. This is what lets an
operator repoint `dev` at their own storage after the first run without `setup` fighting them
on every subsequent `docker compose up`.

### Python assets

The worker runs Python assets as subprocesses that talk to the server over REST, so it needs the
server's address: the bundle sets `LAKANA_RUN__SERVER_URL=http://server:8000` on the `worker`
service (not on the server). The image ships `uv`, which builds one venv per `uv.lock` and `pyproject.toml` pair; the venvs
live in the named volume `worker-cache`, mounted at `/home/lakana/.cache/lakana-worker`, shared by
all worker replicas and kept across restarts. `docker compose down -v` removes it; the next task
rebuilds what it needs.

The volume does not clean itself completely. The workers remove the directory a task leaves
behind when a worker is killed mid-task (`LAKANA_RUN__SWEEP_INTERVAL`, default every hour; the same
setting paces the artifact store's sweep, the sweep of code archives that failed deploys left and the
sweep of deploy checkouts, and `0` turns all off, so nothing collects attempt
directories, artifact leftovers or those) and keep the newest `LAKANA_RUN__VENV_KEEP` venvs, but uv's own download cache
(`uv/` in the volume) is never trimmed. To reclaim it, stop the workers and run
`docker compose run --rm --no-deps --entrypoint sh worker -c 'rm -rf /home/lakana/.cache/lakana-worker/uv'`.

The cache directory must be private to the worker: owned by uid 10001 and mode `0700`, or every
Python task fails with "not private". The image creates it that way and a new named volume
inherits that, so the bundle needs no setup. If you replace the volume with a bind mount or set
`LAKANA_RUN__VENV_CACHE` yourself, create the directory first with
`chown 10001:10001 DIR && chmod 0700 DIR`.

The venv build downloads packages: the worker needs outbound HTTPS to the hosts the project's
`uv.lock` names (PyPI by default), or `LAKANA_RUN__INDEX_URL`, `LAKANA_RUN__HTTPS_PROXY`,
`LAKANA_RUN__NO_PROXY` and `LAKANA_RUN__CA_BUNDLE` set on the `worker` service by hand (same rules
as the `LAKANA_GIT__*` settings below; put a credential in the URL's `user:token@` part: a query string is refused). Without network access a Python task fails at its
environment build, and the task's log has uv's error. Whether a worker can run Python assets is
logged when it starts (`Python assets: enabled` or `disabled: <reason>. To enable it: <fix>`).

`LAKANA_TASK_TOKENS__MAX_TTL_SECONDS` caps the lifetime of every task token, and the server and the
worker both read it. If you change it, set it to the same value on the `server` and the `worker`
service: a cap lowered on the server only is not applied to the tokens the worker mints.

## Storage for artifacts

Artifacts (files such as trained models that Python assets store, see
[Artifacts](../reference/artifacts.md)) live in the environment's object storage, under
`artifacts/` in the storage root. That directory is reserved: do not put anything of your own
there, and never point two Lakana deployments at one storage root, because each would treat the
other's artifacts as leftovers.

**Bytes pass through the server.** The server streams a file to storage in pieces of 4 MiB and
never holds a whole file in memory, so its memory use does not depend on file size. Its network does:
every uploaded and downloaded file crosses the server. Plan the bandwidth between the workers,
the people and the server for the artifacts you store.

### Settings

Set these where the server and the worker read them. In the bundle, the lines under `x-lakana-env`
in `compose.yaml` reach the `migrate` job, the `server`, the `worker` and the two optional worker services
`worker-interactive` and `worker-pipeline` (see the example below).

| Variable | Default | Meaning |
|---|---|---|
| `LAKANA_ARTIFACTS__MAX_FILE_BYTES` | `2147483648` (2 GiB) | Largest single file of an artifact version (above 0). |
| `LAKANA_ARTIFACTS__MAX_BYTES` | `10737418240` (10 GiB) | Largest total size of one version (above 0). |
| `LAKANA_ARTIFACTS__MAX_FILES` | `10000` | Most files in one version (above 0). |
| `LAKANA_ARTIFACTS__UPLOAD_TTL_SECONDS` | `3600` | How long an upload may stay open, and how long its commit can be repeated (above 0). The server and the worker must read the same value, and their clocks must agree far more closely than this value. |
| `LAKANA_APPEND__MAX_CONCURRENT` | `4` | Files (and table appends) the server receives at once, per server process. More is answered 503, and the SDK asks again. |
| `LAKANA_APPEND__READ_TIMEOUT_SECONDS` | `300` | An upload that sends no byte for this long is refused with 408. |
| `LAKANA_REQUEST_BODY__MAX_BYTES` | `1048576` (1 MiB) | Largest body of an ordinary (JSON) request, above 0. A larger one is answered 413 before the server decodes it or checks the caller: a declared length over the limit at once, any other body as soon as the bytes received pass it. Table data and artifact files are not counted: they have the limits above. |
| `LAKANA_QUERY_LIMITS__MAX_STATEMENT_CHARS` | `100000` | Longest SQL text accepted: a submitted query or write, a row-filter or column-mask expression, and a SQL asset at deploy (above 0). A longer one is refused with a message that names this setting. The server and the worker both read it. |
| `LAKANA_QUERY_LIMITS__TIMEOUT_SECONDS` | `900` | Longest one query may run (above 0); for a write, the time to store the result counts. At the limit the worker asks the query to stop, and the query fails with the cause `timeout` and a message that names this setting. A person can end a query sooner: [Stop a query](stop-a-query.md). |
| `LAKANA_QUERY_LIMITS__KILL_GRACE_SECONDS` | `10` | Seconds between asking a query to stop (its time limit, a cancel, a worker that shuts down) and ending its process (0 or more, a finite number). A query that had to be ended after its time limit or a cancel request says so in its message. |
| `LAKANA_QUERY_LIMITS__RUNNING_PER_PRINCIPAL` | `auto` | The most ad-hoc queries one user or service account runs at once, counted over every environment. A further query waits in the queue and is not refused; it starts when fewer than the limit of that person's queries are running, and other people's queries are not held back meanwhile. Pipeline work (deploys, tasks and the queries they run, maintenance) is never counted. With `auto`, one user may run ad-hoc queries on every worker that takes interactive work while there are one or two such workers, and on all of them but one from three on. A whole number of 1 or more replaces the rule: `1` with two workers keeps one free for others, and a number at or above the number of workers means no limit. `auto`, in any letter case, or a whole number; anything else (0, a negative or fractional number, other text) stops the process at start with a message that names this setting. A query running on a worker that was lost still counts until the scheduler ends it, about a minute. The server and every worker read it, so set it in the shared `x-lakana-env` block and nowhere else. |
| `LAKANA_DENIAL_AUDIT__PER_PRINCIPAL_PER_MINUTE` | `30` | `AUTHORIZATION_DENIED` rows the audit log records per principal per minute, in each server and each worker (above 0). A refusal past it is still refused, but not recorded: the next row recorded for that principal has `suppressed` in its detail, the number not recorded. The first refusal of each action is always recorded. Every refusal that is not recorded is still written to the log of that server or worker (`docker compose logs`), one INFO line from `lakana_core.audit.authorization` that starts `AUTHORIZATION_DENIED not recorded in the audit table` and names the principal, the action and the object; filter on it if the log grows. So the audit table stays bounded, and which object was probed after the limit is reached is in the log, not in the table. When a server or worker stops cleanly, the counts not yet recorded are written as one summary row per principal, for at most 100 principals and about 3 seconds so that a slow database cannot delay the stop; a warning says how many principals' counts were left out. |
| `LAKANA_LOG__LEVEL` | `INFO` | How much the server and the worker log: `DEBUG`, `INFO`, `WARNING` or `ERROR`, in any letter case; anything else stops the process at start with a message that names the setting. Lakana's own lines and the server's start-up and error lines follow it, and uvicorn's access log (one line per request) is printed at `INFO` and `DEBUG` only. The progress of a migration (alembic's "Running upgrade" lines, from `lakana-server migrate`) follows the level too. Other libraries log from `WARNING` at every level. Above `INFO`, the denials over `LAKANA_DENIAL_AUDIT__PER_PRINCIPAL_PER_MINUTE` leave no line in the log, only the summary row in the audit table. Set it for the server and the worker alike (the `x-lakana-env` block does). |
| `LAKANA_RUN__SWEEP_INTERVAL` | `3600` | Seconds between the artifact and code-archive sweeps of the worker that holds the scheduler role, and between the attempt-directory and deploy-checkout sweeps of every worker. `0` turns them off. |
| `LAKANA_WORKER__TAKES` | `all` | What a worker takes: `all`, `interactive` or `pipeline`, in any letter case. Anything else stops the worker at start with `LAKANA_WORKER__TAKES must be all, interactive or pipeline (got 'X').` Only the worker reads it, so it does not belong in the shared `x-lakana-env` block: set it in the environment of the workers it is meant for. What the values mean, and when to use them: [Separate workers for ad-hoc queries and pipelines](#separate-workers-for-ad-hoc-queries-and-pipelines). |
| `LAKANA_WORKER__NOTIFICATIONS` | `true` | Whether this worker delivers job notifications (webhooks). `false` stops it from sending any; another worker must deliver, or messages wait as `queued`. A worker with no usable `LAKANA_SECRETS__MASTER_KEY` delivers none either and logs a warning at start. Only the worker reads it. What a message holds and how to test it: [Get told when a job fails](get-told-when-a-job-fails.md). |
| `LAKANA_WEBHOOK__ALLOW_HOSTS`, `LAKANA_WEBHOOK__PROXY`, `LAKANA_WEBHOOK__CA_BUNDLE` | none | Where a worker may send a webhook: host names that skip the address check (fully qualified, names whose DNS you control only), a proxy to tunnel through (deny private, loopback, link-local and `100.64.0.0/10` destinations in its access rules), and a CA file that replaces trust. Set them on the worker only. Meaning and limits: [Webhook network settings](projects.md#webhook-network-settings). |

The clients learn the three size limits from the server, so people and tasks see the refusal before
they send a file. Add the settings you change to the shared block:

```yaml
x-lakana-env: &lakana-env
  # ... the lines that are already here stay ...
  LAKANA_ARTIFACTS__MAX_BYTES: "21474836480"
  LAKANA_ARTIFACTS__UPLOAD_TTL_SECONDS: "7200"
```

Then apply them by running the `docker compose` command of the [Quickstart](#quickstart) again, from
the bundle's directory.

### A reverse proxy in front of the server

A proxy must let these requests through, or users meet errors that name no Lakana setting. It
should also keep out what the server would refuse anyway, so that it never reaches the server:

- **Limit an ordinary request body to a few megabytes** on every path except the ones that
  stream files and table data (`.../tables/<name>/data` and `.../artifacts/...`). The server
  refuses a JSON body over `LAKANA_REQUEST_BODY__MAX_BYTES` (1 MiB) with 413, but only after the
  proxy has passed it on. A proxy that buffers a request in full takes that cost itself.
- **A request body as large as your largest file** (2 GiB by default). Many proxies refuse bodies
  of a megabyte or so unless you raise the limit.
- **No buffering of the request body** in memory or on disk, if the proxy can be told. A file is
  streamed, so buffering needs room for a whole file.
- **A long timeout for a drop.** `lakana artifact drop` removes files inside the request and can run
  for minutes (the client waits up to ten). A proxy that ends it sooner shows the user a 504 for a
  drop that still finishes.

### Interrupted uploads on an object store

If a server dies in the middle of a file, an S3-compatible store keeps the parts it received, and
a listing does not show them. Lakana cannot find or remove them. Set a rule on the bucket that
aborts incomplete multipart uploads. In S3 it is the lifecycle action `AbortIncompleteMultipartUpload`,
for example after one day:

```json
{
  "Rules": [
    {
      "ID": "abort-incomplete-uploads",
      "Status": "Enabled",
      "Filter": {"Prefix": ""},
      "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}
    }
  ]
}
```

Apply it with your store's console or the S3 call `PutBucketLifecycleConfiguration`. Whether a
store supports the rule depends on the store. This guide has not checked it for the SeaweedFS of the
bundle; read its documentation, and if it does not, accept that interrupted uploads leave parts
that use space until you remove them with the store's own tools. A local `file://` storage root
does not have this problem: an interrupted write leaves a `.part` file, which the sweep removes.

### The sweep

Once an hour (`LAKANA_RUN__SWEEP_INTERVAL`) the worker that holds the scheduler role removes what
a crash or an abandoned upload left: expired uploads, files no version needs, half-written files
and the manifests of deleted versions. One sweep runs for at most five minutes and the next one
continues, so a large backlog goes down over several runs. To drain one faster, set a smaller
interval for a while. One limit: a run starts each artifact's pass over its file records from the
beginning, and its time per artifact is fixed, so an artifact with more than about 200,000
distinct files is not finished by later runs either (the sweep's summary line says so when a run
stops at its time limit). The sweep keeps what a restore or a failover may have made the catalog forget
(a directory without a catalog entry, a manifest object of an unknown version and the files it
names) and says so in the worker's log: see "After a restore" below. A file with no record at all is
removed after seven days, or at once when the manifest of a deleted version names it. A file that a
version or an upload in progress names is kept, whatever its record says.

A deleted version's files are removed in the request that deletes it, up to about 200 files or
five seconds, and the sweep removes the rest. A drop can leave the empty directories `blobs` and
`manifests` behind, because the storage interface cannot remove a directory.

**The worker's log.** The worker prints INFO and above to its standard error, with the time, the
level and the logger name on each line; read it with `docker compose logs worker`. That includes
the start-up notices (whether Python assets are enabled, and that the sweeps are off when
`LAKANA_RUN__SWEEP_INTERVAL=0`) and the sweep's summary line. `LAKANA_LOG__LEVEL` sets the level
(see "See more or less in the log" below).

**The server's log.** The server prints the same way: `LAKANA_LOG__LEVEL` (`INFO` by default) and above to its standard error, with
the time, the level and the logger name on each line (`docker compose logs server`), next to
uvicorn's own lines. The INFO lines of Lakana itself are few: the line for a refused request that
the audit table does not record because its principal is over
`LAKANA_DENIAL_AUDIT__PER_PRINCIPAL_PER_MINUTE` (it starts `AUTHORIZATION_DENIED not recorded in the
audit table`, from `lakana_core.audit.authorization`), and a note that the removal of a deleted
artifact's files stopped at its time budget and the sweep does the rest. Filter on the logger name
to find them. Earlier versions printed only warnings from the server's own code, so those lines
were lost.

**Control characters in the log.** Both processes write control characters (an escape, a carriage
return as `\x0d`, a bell, the bidirectional marks, a lone surrogate) as text such as `\x1b`, in
every line, a library's and an exception's text included, uvicorn's access lines too, so a name or
a path that someone chose cannot recolour a terminal or hide text; a tab becomes four spaces. Where
a record has several lines (an error with its traceback), every line after the first starts with
four spaces and `| `, so a line that does not start with a space is always the start of a record
(the server's own start with a time, uvicorn's with `INFO:     ` and the like), never a line made
up inside another. A record longer than 1 MiB of text is cut and ends in `… (N more characters
left out)`. The colours of uvicorn's level names are off for the same reason.

**See more or less in the log.** Both processes read one setting, `LAKANA_LOG__LEVEL`: `INFO` by
default, `WARNING` or `ERROR` for less, `DEBUG` for more. Put it in the `x-lakana-env` block of
`compose.yaml` and run the `docker compose` command of the [Quickstart](#quickstart) again:

```yaml
x-lakana-env: &lakana-env
  # ... the lines that are already here stay ...
  LAKANA_LOG__LEVEL: "WARNING"
```

At `WARNING` and `ERROR` the server also stops printing one line per request, and above `INFO`
the denials over the audit rate leave no line in the log, only the summary row in the audit table:
keep `INFO` if you read that log to see what was probed. At `ERROR` the start-up warning about
grants that have no effect (see [Upgrade policy](../explanation/upgrades.md#delegation-needs-the-admin-role))
is not shown either; `lakana grant list` marks those grants in every case. The progress of a migration (each "Running upgrade A -> B" line while
`lakana-server migrate` runs) follows the level, so you see it at `INFO` and `DEBUG`. `DEBUG` adds
nothing else from libraries (an HTTP client's or a SQL driver's own lines stay off), so it stays
readable. A level that is not one
of the four stops the process at start and says so.

## Exposing it

By default `--bind 127.0.0.1` keeps the server reachable only from this machine. To expose
it on the network, pass a specific address or `--bind 0.0.0.0` for every interface —
**either way, the server speaks plain HTTP**, and `init` warns on stderr for any non-loopback
bind, wildcard included:

<!-- output -->
```
Warning: --bind 0.0.0.0 is not loopback; the server accepts plain HTTP connections from your network.
```

Put TLS in front of it with a reverse proxy instead of binding it to the network directly.

**A proxy that handles TLS by itself, in one block:**

<!-- not tested: moved as is; sorted in the second pull request -->
```
lakana.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

**A proxy with explicit limits:**

<!-- not tested: moved as is; sorted in the second pull request -->
```
server {
    server_name lakana.example.com;
    client_max_body_size 300m;       # sized for table appends
    proxy_read_timeout 300s;         # long-running queries
    location / {
        proxy_pass http://127.0.0.1:8000;
    }
}
```

Then pass `--public-url https://lakana.example.com` to `lakana bundle init` so the saved profile
(and the URL `init` prints for operators) points at the proxy, not the bind address. A
non-loopback `--public-url` with an `http://` scheme gets the same plain-HTTP warning.

### `--bind` and the profile URL

`--bind` accepts an IP address (IPv4 or IPv6) or the literal `localhost`. The profile URL
`init` saves (and prints, absent `--public-url`) depends on how it classifies the bind:

- **Loopback** (`127.0.0.1`, `::1`, `localhost`) or **wildcard** (`0.0.0.0`, `::`) — the
  profile URL is `http://127.0.0.1:<port>`, since that's how this same machine reaches the
  server either way.
- **A specific, non-loopback address** — the profile URL uses that address, e.g.
  `http://203.0.113.5:8000`; an IPv6 address is bracketed (`http://[2001:db8::1]:8000`).

`localhost` is written into `.env`'s `LAKANA_BIND` as `127.0.0.1` — Compose's `host_ip` in the
port mapping must be an IP literal, not the word `localhost`.

## Scaling workers

<!-- not tested: moved as is; sorted in the second pull request -->
```bash
docker compose up -d --scale worker=4
```

or edit `LAKANA_WORKERS` in `.env` and re-run `docker compose up -d`.

With the default `LAKANA_QUERY_LIMITS__RUNNING_PER_PRINCIPAL=auto`, a third worker that takes
interactive work switches on a reserve: one user then runs ad-hoc queries on two of the three
workers, so that others are not kept waiting. With one or two such workers there is no reserve, and
only a fixed `1` gives one; see the [settings](#settings). A query that waits because of the limit
says `user_limit`, with the number running and the limit; see
[Why is my query or task waiting?](why-is-it-waiting.md#a-queued-query).

`lakana worker list` (it needs `MANAGE_ENVIRONMENT` on the instance, as `lakana scheduler status`
does) shows the workers that registered:

<!-- source: docs/examples/getting-started/run.sh#worker-list -->
```bash
lakana worker list
```

<!-- output -->
```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━┳━━━━━━━┓
┃ id                                  ┃ last_seen ┃ status ┃ takes ┃ doing ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━╇━━━━━━━┩
│ analytics-box.local:237049:5d654503 │ 9s ago    │ alive  │ all   │ idle  │
└─────────────────────────────────────┴───────────┴────────┴───────┴───────┘
Not shown: started_at, version, host. Use --output json to see them.
```

`status` is worked out by the server, with the database's clock: `alive` while the worker's last
heartbeat is younger than `LAKANA_RUN__LEASE_SECONDS`, `lost` after that (the worker stopped, or
cannot reach the database). `last_seen` is the age of that heartbeat, shown by your machine's
clock, so a wrong local clock can show an odd age beside a correct `status`. The row of a lost
worker stays for an hour, so that you can see which one it was, and then disappears; the scheduler
has ended the queries and deploys it held long before (see below). `takes` is the work the
worker takes: `all`, unless `LAKANA_WORKER__TAKES` is set to `interactive` or `pipeline` (see
[Separate workers for ad-hoc queries and
pipelines](#separate-workers-for-ad-hoc-queries-and-pipelines)). `doing` is `idle`, or
`query ID`, `deploy ID` or `task ID` for the work the worker carries (the first 8 characters of
the run's id; a worker that supervises a Python task can also run a query, which then reads
`query 3f2c9d1e, task 7a41b0c2`). `--output json` always has the host, the version, the start
time and the whole ids; the table leaves the host, the version and the start time out when the
terminal is narrow and says so under it. In a terminal of 80 columns a long worker id wraps onto
a second line; from 100 columns on a row is one line. No worker listed means none has
registered: start one (`docker compose up -d`, or `lakana server start --local` on a
local instance).

Each worker also takes part in electing the run scheduler; stopping one
(`docker compose stop worker`) lets a running task finish for up to 20 s
(`LAKANA_RUN__DRAIN_GRACE_SECONDS`) before handing it to another worker, which is why the bundle
sets `stop_grace_period: 45s`. A Python asset's process then gets another 10 s
(`LAKANA_RUN__KILL_GRACE`) to exit and 2 s to drain its output, so releasing a Python task takes
about 32 s with the defaults; 45 s leaves margin. If you raise either setting, raise
`stop_grace_period` with it. A worker killed first loses nothing but time: the scheduler reclaims
the task when its lease expires.

The server and every worker must read the same `LAKANA_RUN__LEASE_SECONDS` (60) and
`LAKANA_RUN__RENEW_SECONDS` (20). If you change either, change it in the bundle's shared
`x-lakana-env` block, which the server and every worker use. Every worker
writes a heartbeat to the database every renew interval, and a worker counts as alive while its
heartbeat is younger than the lease. Keep the lease at least twice the renew interval; a worker
warns at start-up when it is not, because one late heartbeat would then make it count as lost.

A worker that is killed, loses power or has not written a heartbeat for the lease (a database that
is away for the lease minus the renew interval, 40 s with the defaults, can be enough) no
longer counts as alive, and the scheduler ends the query runs and the deploys it held within about a
lease (a minute with the defaults) and one scheduler step, and up to `LAKANA_RUN__RENEW_SECONDS`
plus 5 s more after the scheduler was itself stalled or has just taken over. That bound holds when another worker can take over the scheduler's role. Every connection to
PostgreSQL asks it to probe the client and drop the connection after about two thirds of
`LAKANA_RUN__LEASE_SECONDS` once its machine no longer answers, so the role of a worker that lost
power or its network on another machine than the database's is expected to be freed within about a
lease. If PostgreSQL refuses these
settings, the worker or server logs a warning once at the start and keeps running; then PostgreSQL may keep that connection for hours, and until it closes
nothing ends lost queries or reclaims tasks. `lakana scheduler status` shows which worker holds the role and when it last reported; if that worker is gone and the role stays with it for much longer than a minute, ending its database session in PostgreSQL (`pg_terminate_backend`), or restarting PostgreSQL, frees it; restarting the other workers does not. A read or a write that had not been committed
fails with the cause `worker_lost` and a message that says to run it again; a write that had
already been committed ends `SUCCEEDED`, so a user is not told to repeat it; the same holds for
the write of a pipeline's SQL task, and the task ends `succeeded` and is not retried (a task whose
table has [checks](check-your-data.md) is taken again instead, and only checks). A cancel of such a
query answers at once. A deploy, dry run or rollback it held ends `failed` with a message that
says nothing was activated and to deploy again, and a deploy that was only frozen and finishes
later changes nothing (see "Deploy, dry-run, roll back" in [Projects and deploys](projects.md)).
The reason the server must read the same lease as the workers: it decides
by that lease which workers are alive when someone cancels a query, so a server with a shorter
lease than the workers would cancel at once a query that a healthy worker is still running. The
details for the person who runs the query are in [Stop a query](stop-a-query.md).

Each query runs in a process of its own, which the worker starts and can end. That costs about a
tenth of a second per query (measured: 80 ms for `SELECT 42` on a 20-core machine), and it is why a
query past its time limit always ends. A query submitted on its own that is running when you stop a worker gets the same
`LAKANA_RUN__DRAIN_GRACE_SECONDS`, is then asked to stop and, `LAKANA_QUERY_LIMITS__KILL_GRACE_SECONDS`
later, ended; its run fails with the message "The query was stopped because the worker is shutting
down. Run it again." That includes the queries that a Python task sends through the API to load its
inputs: they are ordinary queries. The SQL task of a pipeline is released instead: after the grace
it runs again on another worker. Raise `stop_grace_period` with either setting. The query process gets the file list of every
table the query reads (about half a kilobyte per data file) and refuses a request over 64 MiB before
it starts; the message names the number of files and says to
write the table again with `replace` so that its data lands in fewer, larger files.

### Separate workers for ad-hoc queries and pipelines

Every worker takes every kind of work by default (`LAKANA_WORKER__TAKES=all`), and that is right
until the two kinds get in each other's way. The setting divides the work in two:

- **Interactive work:** the ad-hoc queries, writes and views that a person or a tool submits.
- **Pipeline work:** deploys, the tasks of pipelines and the queries they run (the inputs a Python
  task reads, a SQL task's own write), and table maintenance, also the maintenance a person starts
  by hand.

A worker with `LAKANA_WORKER__TAKES=interactive` takes interactive work only; one with
`pipeline` takes pipeline work only; `all` takes both. Start a second kind when people wait for
their ad-hoc queries while pipelines run, or when pipelines wait behind ad-hoc queries: run some
workers with `interactive` and the others with `pipeline`. Until then leave every worker on `all`.
Outside the Compose bundle, set the variable in the environment of each worker.

**In the Compose bundle** the two kinds are services of their own, `worker-interactive` and
`worker-pipeline`, defined like `worker` (same image, same memory limit, same restart policy) and
started with two numbers in `.env`. Both are `0` by default, so a bundle runs the workers it always
did, and the `worker` service keeps taking everything. For example, `LAKANA_WORKERS_INTERACTIVE=2`
and `LAKANA_WORKERS_PIPELINE=1` in `.env` start two workers for ad-hoc queries and one for
pipelines next to the `LAKANA_WORKERS` you have. Then run `docker compose up -d`. A count is a replica count; set a count back to `0` and run the
same command to stop that kind. **Lower a count to `0` with `docker compose up -d` and without
`--wait`:** with `--wait`, Compose 2.x (2.24 to at least 2.38) stops the containers and then fails
with `is missing dependency <service>`. Nothing is wrong with the bundle then; run
`docker compose up -d --wait` once more and it passes. To move a whole installation over, lower `LAKANA_WORKERS` as you
raise the other two: the default `worker` service may go to `0`, nothing in the bundle needs it,
but the instance then needs a worker for each kind it uses (see below). Each added worker may use
the memory limit `LAKANA_WORKER_MEMORY` sets for every worker (`4g` by default), so an operator who
adds two workers adds two limits to what the machine must have; see "Memory limits". Check the
result with `lakana worker list`: its `takes` column shows `all`, `interactive` or `pipeline` for
each worker that registered. A bundle generated by an earlier release has neither service; see
"Upgrading".

- **The scheduler role does not depend on it.** Every worker takes part in electing the scheduler,
  whatever it takes, and the worker that holds the role also ends the runs of lost workers of both
  kinds.
- **`lakana worker list` shows it** in the `takes` column, and a worker logs at start what it
  takes.
- **A Python task keeps its queries.** A pipeline worker that supervises a Python task serves the
  task's own input queries itself, so a task never waits for an interactive worker (it can wait
  behind a maintenance run that was queued earlier and is served by the same worker). It leaves
  ad-hoc queries to the interactive workers.

The one misconfiguration to avoid: only `interactive` workers on an instance that deploys or runs
pipelines. Nothing fails; deploys, tasks and maintenance wait until a worker that takes pipeline
work starts, and a deploy does not say why. Keep one worker or more on `all` or `pipeline`: in the bundle, `LAKANA_WORKERS` at 1 or more, or
`LAKANA_WORKERS_PIPELINE` at 1 or more. The same holds the other way round:
with only `pipeline` workers, ad-hoc queries wait until a worker that takes interactive work
starts.

### Seeing that a kind has no worker

Look at the `takes` column of `lakana worker list`: at least one alive worker must take each kind
you use. When it does not, the people who wait are told:

- **A query, a maintenance run and a task of a pipeline** show the reason `no_worker_of_kind`,
  for example `No worker that takes interactive work is running (2 workers take pipeline work
  only), so this query cannot start. Ask the operator to start a worker with
  LAKANA_WORKER__TAKES=interactive or all.` Start a worker with that setting.
- **A deploy** that stays `queued` while only `interactive` workers run is the sign to look for:
  start a worker that takes `pipeline` or `all`.

[Why is my query or task waiting?](why-is-it-waiting.md) lists the sentences and what each asks
of the person who reads it.

## Memory limits

A query can use more memory than DuckDB's own setting (`LAKANA_QUERY_LIMITS__MEMORY_LIMIT`, about
1 GB by default) says: some operations, and the code that turns a result into a table, use more.
Without a hard stop, one large query can fill the machine until the operating system kills the
worker, or the whole host stops responding. Each worker therefore has a memory limit, 4 GB by
default.

- **The container.** Each worker container gets a limit and no swap, from `LAKANA_WORKER_MEMORY`
  in `.env` (`4g`). If a worker goes beyond it, the kernel ends the worker, and Docker starts the container again
  (`restart: unless-stopped`). Use at least `1g`: a worker needs about 512 MB to start, and it stops
  itself at 90 percent of the container's limit.
- **The worker.** Inside the container the worker reads that limit and checks its own memory five times a
  second: the worker plus the tasks and queries it started (every query runs in a process of its
  own). Over the limit (90 percent of the container's), the worker measures each running query
  and each running Python task and **ends the largest one first**, at once, whichever kind it is.
  A query fails with the cause `out_of_memory` and the message below. A Python task fails that
  attempt with the second message below and is retried while it has attempts left (`retries + 1`
  in all). The worker and what runs beside the ended one go on. The worker then measures
  again (the ended process's memory is gone within a moment). Only if it is still
  over the limit, or nothing it can end was running, does the worker stop itself, and Docker
  starts it again.
  When the worker has to stop itself, whatever it was running ends with it, and is marked failed
  (a query, or the query run of a task, with the cause `out_of_memory`), so you see why instead
  of waiting for a lease to expire, when memory grows slowly enough for the worker to notice; a
  very fast query can still reach the container's limit first. A task has no cause field of its
  own; the cause is on its query run. A task is retried if it has attempts left (`retries + 1` in
  all), so one bad query cannot stop workers for ever. A query is not retried.

To change it, edit one line in `.env` and run `docker compose up -d`: set `LAKANA_WORKER_MEMORY`
to, for example, `8g`. A bundle made by an older version has no such line and uses 4 GB. The limit
is per replica, and `worker-interactive` and `worker-pipeline` use the same value:
`LAKANA_WORKERS`, `LAKANA_WORKERS_INTERACTIVE` and `LAKANA_WORKERS_PIPELINE` added together, times
`LAKANA_WORKER_MEMORY`, must fit the machine, with room left for Postgres, SeaweedFS and the
server. Two workers at `4g` need 8 GB for the workers alone, and two more of either kind need 8 GB
more.

When the worker ends a query it logs `Memory limit exceeded: the worker and its tasks use <n> MB,
the limit is 3686 MB, 90% of the container's 4096 MB; raise the container's memory limit (in the
Compose bundle: LAKANA_WORKER_MEMORY in .env). The largest consumer, a query (process <pid>), was
ended; the worker keeps running if that frees enough memory.` and that query fails with the cause
`out_of_memory` and the message `Query failed: this query made the worker exceed its memory limit
(3686 MB) and was stopped. Read or compute less (filter or aggregate earlier, or select fewer
columns), or ask the operator to raise the worker's memory limit (LAKANA_WORKER__MEMORY_LIMIT, or
LAKANA_WORKER_MEMORY in the Compose bundle's .env).`

When it ends a Python task, the line reads `... The largest consumer, a Python task (asset
<asset>, task run <id>, process <pid>), was ended; ...`, and the task's attempt fails with `this
task was ended because the worker exceeded its memory limit (3686 MB) and the task was the
largest consumer. Use less data in the task (narrow its inputs with an
upstream SQL asset), or ask the operator to raise the limit (LAKANA_WORKER__MEMORY_LIMIT, or
LAKANA_WORKER_MEMORY in the Compose bundle's .env).` The task is retried like any transient
failure, so a task that always needs more than the limit is ended again on every attempt until its
attempts run out; narrow its inputs or raise the limit. A task whose virtual environment is still
being built cannot be ended (the build cannot be interrupted); if the build is what grew and
nothing else can be ended, the worker stops itself.

When the worker stops itself it logs `Memory limit exceeded: the worker and its tasks use <n> MB,
the limit is 3686 MB, 90% of the container's 4096 MB; raise the container's memory limit (in the
Compose bundle: LAKANA_WORKER_MEMORY in .env). The worker stops now. What it was running is marked failed; a task is retried if it
has attempts left.` A Python task with checks that may have written its table does not end over
unchecked rows when the stop ends it: on its last attempt, or on any attempt when the blame is
certain, the next worker sets the table back first (see [A worker that is
lost](check-your-data.md#a-worker-that-is-lost)). A query that was running then fails with the cause `out_of_memory` and the message
`Query failed: the worker ran out of memory and was stopped (its limit is 3686 MB). Read or compute
less (filter or aggregate earlier, or select fewer columns), or ask the operator to raise the
worker's memory limit (LAKANA_WORKER__MEMORY_LIMIT, or LAKANA_WORKER_MEMORY in the Compose
bundle's .env).` A query that fails on DuckDB's own limit says `LAKANA_QUERY_LIMITS__MEMORY_LIMIT`
instead. A write that had already been committed when the worker stopped ends `SUCCEEDED`, because the
data is in the table and running it again would write it twice. A sort or aggregation over that limit fails the same way (`the query ran out of
memory and cannot spill to disk`, with the same advice): the bundle does not let DuckDB write
spill files to disk. What to do:

1. Narrow the query: filter or aggregate earlier, select fewer columns.
2. If the data is simply that large, raise `LAKANA_WORKER_MEMORY` in `.env`, or add memory to the
   machine.

If the kernel killed the container before the worker could stop itself, a task shows `lease
expired: the worker stopped renewing it. If it was stopped for using too much memory, see the
worker's memory limit in the documentation (LAKANA_WORKER__MEMORY_LIMIT, or LAKANA_WORKER_MEMORY in
the Compose bundle's .env).` `docker inspect` shows `OOMKilled` for a
container that Docker stopped for memory.

You can set `LAKANA_WORKER__MEMORY_LIMIT` in the worker's environment yourself (`4GB`, `512MB`,
`off`; at least 512 MB). It then replaces the 90 percent rule. Its sizes are binary, so `GB` and
`g` mean 1024 MB; DuckDB's `LAKANA_QUERY_LIMITS__MEMORY_LIMIT` is different: `GB` there is decimal
(`1GB` is 953.6 MiB) and `GiB` binary. A value DuckDB cannot read, such as `lots`, stops the
worker at start-up with `LAKANA_QUERY_LIMITS__MEMORY_LIMIT is not a size DuckDB accepts`; the same
holds for `LAKANA_QUERY_LIMITS__MAX_TEMP_DIRECTORY_SIZE`. Write a number and a unit, such as `1GB`
or `512MiB`.

Workers must not share a temp directory across containers: a starting worker removes the spill
directories whose owning process it cannot see.

Not covered:

- The worker's own memory (maintenance, a result being stored) cannot be ended apart from the
  worker. The worker ends the largest query or task that it can end, whatever its share. If the
  worker's own memory sits just under the limit, a small task or query beside it is ended, the
  total drops, and the worker goes on; each retry and each new query is then ended the same way,
  and the task ends `FAILED` with "the task was the largest consumer". Give the worker more
  memory (`LAKANA_WORKER_MEMORY` in `.env`); if its own memory keeps growing, restart the worker.
- The total counts every process in the worker's sessions, but a task's or query's size is its
  tree of child processes: a process a task detached by a double fork counts in the total and not
  in the task's size.
- A task that always needs more than the limit is ended and retried until its attempts run out.
  Give the worker more memory (`LAKANA_WORKER_MEMORY` in `.env`), or give the task less data
  (narrow its inputs with an upstream SQL asset).
- A task in its environment build phase (`uv sync`) cannot be ended; it is skipped when the
  worker chooses, and the worker stops if nothing else can be ended.
- A burst between two checks is not seen.
- On macOS the worker measures its own peak, which never falls, and always stops without ending a
  query or a task first.
- A deploy (git and the project's files) is not tied to a run, so it fails when its worker stops,
  without the message above.

## Running more than one bundle on a host

`compose.yaml`'s top-level `name: lakana` gives every bundle the same Compose project name.
That's fine for the single bundle this guide assumes, but two consequences follow directly
from it:

- **A second `bundle init` bundle on the same Docker host collides with the first.** Compose
  identifies a project's containers and volumes by project name, so `docker compose up` in
  the second bundle's directory takes over the first bundle's containers, and a later
  `docker compose down -v` there deletes the first bundle's `pgdata`/`seaweed` volumes.
- **The fix is `COMPOSE_PROJECT_NAME`, set before any `docker compose` command** — an
  environment variable takes precedence over `compose.yaml`'s own `name:`. Either
  `export COMPOSE_PROJECT_NAME=lakana-otherteam` once per shell, or add
  `COMPOSE_PROJECT_NAME=lakana-otherteam` to the bundle's own `.env` file, which Compose reads
  automatically from the bundle directory for exactly this purpose (as well as for its own
  `${VAR}` interpolation). Do this before the very first `up` for the second bundle —
  changing it afterwards makes Compose treat the existing containers as belonging to a
  different, new project.

## Upgrading

What every upgrade asks, and what a release may break, is in the
[Upgrade policy](../explanation/upgrades.md).

Upgrading to the release with artifacts needs a database migration, the server and every worker on
the new release, and every project with Python assets locked again for it: see
[Upgrade to the release with artifacts](upgrade-to-artifacts.md) for the order and the way back.

Two things to check before editing `.env`:

1. **Pull the new tag first, before editing anything.** Pulling first fails loudly if the tag
   does not exist, rather than mid-upgrade with the old container already stopped.

   <!-- not tested: moved as is; sorted in the second pull request -->
   ```bash
   docker pull ghcr.io/chrschy/lakana:<new version>
   ```

2. **`compose.yaml` is a static file `init` wrote once, with the CLI version that ran it —
   upgrading only the image tag does not pick up a `compose.yaml` change a newer `init`
   would generate** (a new service, a changed healthcheck, and so on). Compare your bundle's
   copy against a fresh one before assuming a tag bump is the whole upgrade:

   <!-- not tested: moved as is; sorted in the second pull request -->
   ```bash
   lakana --profile upgrade-check bundle init /tmp/lakana-upgrade-check --image ghcr.io/chrschy/lakana:<new version>
   diff lakana-deploy/compose.yaml /tmp/lakana-upgrade-check/compose.yaml
   rm -rf /tmp/lakana-upgrade-check
   lakana profile remove upgrade-check --yes
   ```

   `--profile upgrade-check` is required here: `init` refuses (exit 2) if a profile of the
   target name already exists, and the operator running this already has a `team` profile
   by definition — a bare `lakana bundle init /tmp/lakana-upgrade-check` would just fail with
   "Profile 'team' already exists". Only compare `compose.yaml` —
   `/tmp/lakana-upgrade-check/.env` holds unrelated, freshly generated secrets, not anything
   to merge in. Apply any structural `compose.yaml` difference to your own bundle by hand;
   `init` never writes into a directory that already has a bundle. The final `lakana profile
   remove` cleans up the scratch profile this check saved.

### Limits that start to apply after you upgrade

Three bounds are new. Each has a default that no ordinary use reaches, a message that names its
setting, and a setting to raise it; check that none of your clients is above one before you upgrade.

- **A JSON request body over 1 MiB is answered 413.** The answer says "The request body is larger than
  1048576 bytes, the most the server accepts for a JSON request" and names
  `LAKANA_REQUEST_BODY__MAX_BYTES`, a setting of the server. Table data and artifact files are not
  counted. A client sends less in one request, or you raise the setting.
- **SQL text over 100,000 characters is refused.** That is a submitted query or write, the expression of
  a row filter or a column mask, and a SQL asset of a project: it is refused when it is submitted, when
  the policy is created and at the next deploy. Assets that are already deployed keep running. The
  message says "... characters long; the most Lakana accepts is 100000" and names
  `LAKANA_QUERY_LIMITS__MAX_STATEMENT_CHARS`, which the server and the worker both read: raise it on
  both.
- **At most 30 `AUTHORIZATION_DENIED` audit rows are written per principal per minute**, in each server
  process and each worker (`LAKANA_DENIAL_AUDIT__PER_PRINCIPAL_PER_MINUTE`). A refusal past that is still
  refused, and is written to the log of that server or worker instead of the audit table, as an INFO line
  that starts `AUTHORIZATION_DENIED not recorded in the audit table`. If you read the audit table to
  find out who tried what, read that log as well once a principal is over the rate.

**The logs of the server and the worker have no lines of libraries at INFO.** The log level setting (`LAKANA_LOG__LEVEL`) keeps libraries at `WARNING`, so INFO lines such as the S3 client's note about where it found its credentials are gone, in the server and the worker alike; Lakana's own lines and the migration progress are as before.

**The server's log has more lines than before.** The server now prints the INFO lines of Lakana's own
code to its standard error, as the worker does (see "The server's log" above). Under an attack that
makes many refused requests, the log grows by one line for each refusal past the audit rate, where
the table does not; if a log collector bills or rotates by size, check its limits before you upgrade.

**One user's running queries are limited from three workers on.** The default of
`LAKANA_QUERY_LIMITS__RUNNING_PER_PRINCIPAL` is `auto`. With three or more workers that take ad-hoc
queries, one user or service account can have at most all but one of them busy at once (with three workers
that take ad-hoc queries, two queries running at once; with four, three), and a further query waits with the reason `user_limit` while a worker is free. With one or
two workers nothing changes, which includes the bundle's default of two. A script or a BI tool that
runs queries in parallel under one account is such a user. To keep the earlier behaviour, set the
variable to the number of your workers; give the server and every worker the same value.

A package index or proxy URL whose path has a segment that looks like a token (a cloud project id, for
example) is not refused; the server and the worker log a warning at start-up that names the setting.
It is there so that a credential in a path is not forgotten: only a URL's userinfo is redacted from logs.

**Bundles generated before the worker kinds** have the `worker` service only, and the two counts in
`.env` do nothing. Generate a new `compose.yaml` with `lakana bundle init` into an empty directory
and copy its `worker-interactive` and `worker-pipeline` services next to your `worker` (they merge
the definition of `worker`, so give it the anchors `&worker` and, on its `environment`,
`&worker-env` as the new file does), then add `LAKANA_WORKERS_INTERACTIVE` and
`LAKANA_WORKERS_PIPELINE` to `.env`.

**Bundles generated before M5b** have no `repos/` mount, so the server and workers refuse
every local repository ("LAKANA_REPO_ROOTS is empty"); remote (`https`, `ssh`) repositories work
without it. To use local repositories, create `repos/` next to `compose.yaml` and add both of
these to the `server` and the `worker` service by hand:

<!-- not tested: moved as is; sorted in the second pull request -->
```yaml
    environment:
      <<: *lakana-env
      LAKANA_REPO_ROOTS: '["/repos"]'
    volumes: ["./repos:/repos:ro"]
```

A pre-M5b `worker` has the bare alias `environment: *lakana-env`; change that line to
`environment: {<<: *lakana-env, LAKANA_REPO_ROOTS: '["/repos"]'}` (a bare alias cannot take a
second key).

**Bundles generated before M5c2a** cannot run Python assets: add these to the `worker` service
(an older `stop_grace_period` of 30 s should become `45s` at the same time) and add
`worker-cache:`
under the top-level `volumes:`:

```yaml
    stop_grace_period: 45s
    environment:
      LAKANA_RUN__SERVER_URL: http://server:8000
    volumes:
      - worker-cache:/home/lakana/.cache/lakana-worker
```

Then edit `LAKANA_IMAGE` in `.env` to the new tag and:

<!-- not tested: moved as is; sorted in the second pull request -->
```bash
docker compose pull && docker compose up -d --wait
```

`migrate` runs first (its `depends_on: {postgres: {condition: service_healthy}}`, and
`server`/`worker` both depend on it completing successfully), so the schema is always
migrated before the new server or worker code runs against it.

## Backups

Quiesce the stack first (`docker compose stop` and `docker compose start` afterwards, or an
equivalent maintenance window) so the dump and the volume snapshot are consistent with each
other.

**Postgres (the catalog):**

<!-- not tested: moved as is; sorted in the second pull request -->
```bash
docker compose exec postgres pg_dump -U lakana lakana > lakana.sql
```

**SeaweedFS (object storage):**

<!-- not tested: moved as is; sorted in the second pull request -->
```bash
docker run --rm -v lakana_seaweed:/data -v "$PWD":/backup busybox tar czf /backup/seaweed.tgz /data
```

## After a restore

Restore the catalog (Postgres) and the object storage from backups of the **same moment**, as
under "Backups". Artifacts are where a mismatch shows: storage may hold artifact files and
manifests that the restored catalog has never heard of, or the catalog may list versions whose files
are gone. This section assumes that the catalog came from a consistent backup, a database dump or
point-in-time recovery.

**Before you restore, if you are unsure:** set `LAKANA_RUN__SWEEP_INTERVAL=0` on the workers so that
no sweep runs while the two disagree, and set it back when you have checked what follows. The same
holds after a failover that lost the last commits, and when you point a fresh database at an
existing bucket.

**What the sweep does when the catalog and the storage disagree.** It removes little, and it
says so in the worker's log when it keeps something back.

- A directory under `artifacts/` that has no catalog entry and no drop marker is left alone.
- A manifest object of a version the catalog does not know is never removed. The files it names
  are kept as long as it exists.
- A file that has no record in the catalog is removed once it is older than seven days, or at once
  when the manifest of a deleted version names it. A manifest object that the catalog does not know
  protects the files it names from both. So does a version that the catalog lists as not deleted,
  and so does an upload in progress.

So a file the catalog has forgotten is kept while a manifest object the catalog does not know
names it, while a listed version or an upload in progress names it, and while its directory has no
catalog entry and no marker. A file with no record, in a directory the catalog knows, that none of
these names, can go. If your bucket keeps old object versions, a file that
Lakana removed stays in the bucket until the bucket's own rule removes it.

**What the sweep keeps, and the log lines that say so**

The worker prints these lines to its standard error: `docker compose logs worker`. Each starts
with the time, the level and the logger name (`lakana_core.artifacts.sweep`). Only a sweep prints
them, so with `LAKANA_RUN__SWEEP_INTERVAL=0`, or before the first sweep has run, you will not see them.

1. `<n> artifact directories in environment <env> have no catalog entry and no drop marker; they are
   left alone. ...` Under `artifacts/` there are directories whose artifact the catalog does not
   have. The files are intact. A drop marks its directory `.dropped` before it deletes the row, so
   an unmarked directory is not a drop: it is an artifact that the restored catalog forgot (for
   example one created after the backup). Nothing was deleted. Nothing can read those files either.
   **Keep the directory.** Restore a newer catalog backup if you have one. Lakana has no command
   to bring such an artifact back yet. Deleting the directory by hand cannot be undone, and the
   catalog cannot tell you what is in it. Delete it only if you know from your own records that
   nobody needs any version stored there.
2. `The artifact sweep found manifest objects for version <n> of artifact '<name>' that the catalog
   does not know; the files they name are kept. ...` Storage holds `manifests/<n>.json` for a
   version that has no row in the catalog. Either a commit failed part-way (harmless), or the
   catalog lost a version. The files that manifest names are kept. The line comes again at every
   sweep until the object is removed or the catalog knows the version again. See "A manifest the catalog does
   not know" below. Without this line you can still find such objects: list
   `artifacts/<id>/manifests/` in storage; every `<n>.json` whose number `lakana --output json
   artifact versions list` does not show (it shows deleted versions too) is one.
3. `The artifact sweep left '<name>' whole for this run: manifest objects the catalog does not know
   could not all be read, or there are more than 20 of them; ...` The sweep checks at most 20 such
   objects of at most 16 MiB each. It removes nothing of that artifact until the cause is
   gone: remove the objects only where "A manifest the catalog does not know" says that removing is
   safe, or fix the unreadable one. If you are repairing versions whose files are gone, this line is
   **stop sign 2** in "Versions whose files are gone".
4. `The artifact sweep left '<name>' whole: a stored manifest of a live version or open upload is
   damaged. ...` Nobody can download that version. The log names the artifact, not the version: try
   a download of each of its versions, and the damaged one fails with `The stored manifest of
   version <n> of <name> is damaged; delete the version and run the asset again.` Delete it with
   `artifact versions delete`. The delete also removes the version's manifest object
   `manifests/<n>.json`, and the sweep then removes the files that no other version names. Copy
   that object and the files it names first if you want them.
5. `<n> files of artifact '<name>' are named by a version or an upload but have no record; kept.
   ...` A version the catalog lists, or an upload in progress, names files that have no record. A
   restore from a consistent backup cannot cause that, so look for what removed the records.
   Nothing was deleted.

**A manifest the catalog does not know.** Find the artifact's id: the `uuid` field in the
`--output json` output of the `artifact list` subcommand, without the hyphens, is the directory
name under `artifacts/` in the storage root. The object is
`artifacts/<id>/manifests/<n>.json`. A manifest object is a small JSON list of file paths, sizes
and hashes, and the SHA-256 of its bytes is the `manifest_sha256` that `artifact versions list`
shows for a version. Compute the SHA-256 of the object and compare it with the versions **of the
same artifact**. The table of `artifact versions list` shows only the first 12 characters of the
digest; for the whole digest use `lakana --output json artifact versions list <name> --env <env>`.

- **It equals the `manifest_sha256` of a version that the list shows as not deleted: removing the
  object is safe.** That version has exactly the files the object names, and it keeps them. Only
  the record of version `<n>` goes, and a later version can get the number `<n>` again.
- **It equals only the `manifest_sha256` of a deleted version, or of none: keep the object,
  unless it names a file that a broken version is missing (next bullet).** A deleted version does not
  make it safe. A task can store a version with the same content as one deleted before, and that
  new version needs the files. If the catalog forgot such a version, the object may be the only
  thing that protects its files: if you remove it, the next sweep removes those files, and that
  cannot be undone. Ask whether a task or a person stored or read version `<n>` after your backup
  was taken (their messages say `Stored <env>/<name> version <n>`). If so, Lakana has no command
  to bring the version back yet ([issue 68](https://github.com/chrschy/lakana/issues/68)).
- **It names a file that a broken version is missing: remove it, unless somebody needs its
  version.** A broken version is one whose download fails with `is missing from storage` (see
  "Versions whose files are gone"). Look up each `sha256` of the object as
  `artifacts/<id>/blobs/<sha256>`: it names a missing file when one of them is absent. An object
  with the same SHA-256 as a broken version names the same files, so it names the missing one too.
  While it exists it does harm: it keeps the catalog's record that the missing file is stored, so
  deleting the broken version frees no record and running the asset again does not send the file.
  What the object still protects is only its other files. **Before you remove it, copy the object
  and the other files it names out of storage.** Keep the copy outside the storage root: put back at
  `artifacts/<id>/manifests/<n>.json` it would pose as the object of the next version that gets the
  number `<n>`, and the next run reuses that number. Removing the object lets the next sweep remove
  those other files that nothing else names: at once when the manifest of a deleted version names
  them, otherwise once they are older than seven days, which they may already be. If somebody
  needs version `<n>`, do not remove the object: that is **stop sign 1** in "Versions whose
  files are gone".
- **Remove any other such object only if you are certain that no task or person stored or read
  version `<n>`** (the restored catalog does not show runs after the backup; check the worker's and
  your CI's logs): it is then the leftover of a commit that failed. The next sweep may remove the
  files that no remaining version needs. When you are not certain, keep it. Keeping costs a log line
  at every sweep. While the object exists, a request that deletes a version of that artifact frees
  no record; the next sweep removes the records and files that no version and no such object
  names.

A commit never reuses a version number that storage has seen, so you will see **gaps in the
numbers**. That is normal after a restore, and after a commit that failed.

**Versions whose files are gone.** If you restore the catalog to a moment before you deleted a
version (or before a run with `keep_versions` pruned it), the catalog lists that version again, but
its files were removed. A download then fails with
`A file of version <n> of <name> is missing from storage, and the catalog still lists it as stored,
so running the asset again would not store it. Delete this version and every other version that has
the file (`lakana artifact versions delete`), then run the asset again.` **Running the asset again
does not repair this** when its output is the same, because the catalog still believes the file is
stored and sends nothing.

Two cases need no steps and lose nothing:

- **If you have a backup of the object storage from before the delete,** copy the missing files back
  from it into `artifacts/<id>/blobs/`. Each file keeps its name, which is its SHA-256 (never copy
  over a file that exists). The server does not check the bytes of a stored file, so download the
  version afterwards with `lakana artifact download`, which checks every file against the manifest:
  when that succeeds, the version is back and nothing was deleted.
- **If you restored to a moment before you dropped the artifact,** its row is back and all its files
  are gone, so every version is broken. `artifact drop` removes what is left, and the next run of
  the asset creates the artifact again as a new one (the grants given on the artifact itself are
  gone, see [Delete an artifact version](delete-an-artifact-version.md#drop-the-whole-artifact)).

Otherwise follow the steps below. Every step is one action with a sign that it worked. There are
three stop signs, below the steps: places where no documented action keeps everything. At a stop
sign, stop: do not delete or remove anything more of that artifact, keep what the sign names, and
read what it says.

**First keep the asset from running, until step 5.** A run that commits in between stores a new
version that is broken again. A run starts when a person or your CI starts it (`lakana run`, `lakana job run`) or
when a job's schedule fires: ask them to wait, pause the CI job, and pause the schedules
(`lakana job pause`; see [Run a job on a schedule](run-a-job-on-a-schedule.md)). `lakana runs
list --env <env>` shows the runs: wait until none of the asset is queued or running.

1. **Find the broken versions.** List the artifact's versions, as JSON:

   <!-- not tested: needs a restored instance; the names are placeholders -->
   ```bash
   lakana --output json artifact versions list <name> --env <env>
   ```

   Note the artifact's `uuid` (the `<id>` in storage is the `uuid` without hyphens) and every
   version whose `deleted_at` is `null`. Try a download of each of them into an empty directory
   (see [Inspect and download versions](inspect-and-download-artifact-versions.md)):

   <!-- not tested: needs a restored instance; the names are placeholders -->
   ```bash
   lakana artifact download <name> <empty directory> --version <n> --env <env>
   ```

   **Sign:** a broken version fails with the message above. A version that shares the missing
   file fails the same way, and all of them must go. If none fails, there is nothing to repair.
   Then open `artifacts/<id>/manifests/<n>.json` of a broken version in storage: the files in it
   whose `blobs/<sha256>` is absent are the missing files.
2. **Look at the manifest objects the catalog does not know.** List `artifacts/<id>/manifests/` in
   storage. Every `<n>.json` whose number step 1's list does not show (it shows deleted versions
   too) is one. Decide each as "A manifest the catalog does not know" says. An object that names none
   of the missing files: keep it. One that names a missing file, when nobody needs its version:
   **copy it and its files out of storage first**, then remove it. One that names a missing file,
   when somebody needs its version: **stop sign 1**. **Sign:** the objects you removed are gone from
   the listing. If more than 20 such objects are left, or one is larger than 16 MiB or not valid
   JSON: **stop sign 2**, before step 3.
3. **Delete each broken version.** First copy out of the live storage the files of the version that
   you still want: the delete removes them, except those another version has, and that cannot be
   undone. Then delete the version:

   <!-- not tested: deletes a version of a restored instance; the names are placeholders -->
   ```bash
   lakana artifact versions delete <name> <n> --env <env> --yes
   ```

   That forgets the file's record, and it also removes the other files of that version, except
   those another version has. **Sign:** `Deleted version <n> of <env>/<name>.` and `deleted_at`
   set in the list. If a version that shares the file is not deleted, the file's record stays.
4. **Let one sweep run, only if step 2 left an unknown manifest object, or in the second round
   (step 5).** While one exists, the delete frees no record in its own request (its answer says
   `Freed no files` and `Kept … that Lakana's periodic clean-up decides about`); the sweep does. If
   you set `LAKANA_RUN__SWEEP_INTERVAL` to `0`, set it back to its normal value (3600) in
   `compose.yaml`, and apply it as under [Settings](#settings): `docker compose restart` does not
   read the file again, so run the `docker compose` command of the [Quickstart](#quickstart) again.
   If the value was not changed, restart the workers instead. The worker that holds the scheduler
   role runs a sweep at its first step. Then read the worker's log:

   <!-- not tested: reads the log of a running Compose stack -->
   ```bash
   docker compose logs worker
   ```

   **Sign:** a line `Artifact sweep: <n> expired uploads, <m> unneeded file records, ...` with
   `<m>` of 1 or more, **printed at any time after step 3**: the record is freed. The hourly sweep
   may have run before you got here, so look at the whole log since step 3, not only after the
   restart. A line with `0 unneeded file records` is not that sign. The sweep prints its summary
   only when it removed something, kept an artifact whole, found one damaged or failed on one: a
   sweep that found nothing to do prints nothing. If a
   sweep has run since step 3 and there is no such line, there was nothing left to free: go on to
   step 5, whose download is the check. If instead you see `The artifact sweep left '<name>'
   whole for this run: ...`: **stop sign 2**. If the line contains `stopped at its time limit`,
   the run stopped before it finished; a later run starts again, one interval later.
5. **Run the asset again and download the new version.** Run the asset the way you always do,
   then download the new version into an empty directory:

   <!-- not tested: needs a restored instance; the names are placeholders -->
   ```bash
   lakana artifact download <name> <empty directory> --version <new n> --env <env>
   ```

   **Sign:** `Downloaded <env>/<name> version <n> to <directory>`. That ends the repair: let your
   CI run again. If the download fails with the same message, do not run the asset again: the new
   version is broken too, and an upload in progress that names the file keeps its record until the
   upload expires (`LAKANA_ARTIFACTS__UPLOAD_TTL_SECONDS`, an hour by default; a run that began
   before step 3 leaves such an upload). Wait that long, then go back to step 1 for **the second
   round**, which includes step 4 even when step 2 left no object. If the second round fails the
   same way: **stop sign 3**.

**Stop sign 1: an object the catalog does not know names a missing file, and somebody needs its
version.** Keep the object, its files and every broken version of the artifact. While the object
exists the missing file's record stays, and the asset cannot store the file again. Lakana has no
command to bring such a version back yet ([issue 68](https://github.com/chrschy/lakana/issues/68)).
What keeps everything: copy the missing file back from a storage backup (first case above). If you
decide later that nobody needs the version, remove the object as step 2 says and go on at step 3.

**Stop sign 2: the sweep leaves the artifact whole, or step 2 found more than 20 objects or an
unreadable one.** Keep everything of the artifact: the sweep removes nothing of it and the asset
cannot get a working version. What keeps everything: copy the missing file back from a storage
backup (first case above). Or wait for [issue 68](https://github.com/chrschy/lakana/issues/68).
Or reduce the objects yourself, each removal being a decision of step 2 (copy first): get to at
most 20 objects that are all readable, then go on at step 3.

**Stop sign 3: the second round failed too.** Keep what is left and do not run the asset again:
each run stores another broken version. Report it in [issue
68](https://github.com/chrschy/lakana/issues/68) with the output of `lakana --output json
artifact versions list` and the worker's log lines of the sweep.

## Revoking the bootstrap credential

The credential `init` saves into the `team` profile (`lakana_pat_<key id>_<secret>`,
`LAKANA_BOOTSTRAP_PAT_KEY_ID`/`LAKANA_BOOTSTRAP_PAT_SECRET` in `.env`) is a normal PAT for the
bootstrap admin principal (named `admin` by default) — it stays valid until you revoke it.
Create a named admin to replace it, then revoke the bootstrap one. To add an ordinary user instead,
see [Give a second person access](give-a-second-person-access.md):

<!-- not tested: moved as is; sorted in the second pull request -->
```bash
lakana principal create alice --type user
lakana token issue alice alice-laptop                        # shown once — save it
lakana grant create --kind INSTANCE --principal alice --role ADMIN
lakana login --profile team --url http://127.0.0.1:8000       # overwrite the team profile's own token with alice's
lakana token revoke admin <LAKANA_BOOTSTRAP_PAT_KEY_ID> --yes
```

**`--yes` is required here, not optional, without a TTY.** `token revoke` asks for
confirmation interactively; scripted or non-interactive runs (a provisioning script, CI)
need `--yes` to skip the prompt (`packages/lakana/src/lakana/cli/tokens.py`).

**Every later `docker compose up` keeps working after this.** `setup` re-runs on every
`up`, still using the now-revoked bootstrap token from `.env` — it treats a 401 or 403 from
the server as the credential having been rejected on purpose (or, less commonly, `.env` not
matching this database's admin at all — see "`setup`" below), prints a single line, and
exits 0 rather than failing the container, so `up -d --wait` still succeeds.

**`--profile team` on `lakana login` is not optional here.** Without it, `login` writes a
*new* profile (named `default`, since none of the global `--profile`, `LAKANA_PROFILE`, or an
explicit `--profile` set a name) instead of updating `team` — the `team` profile keeps the
bootstrap token, every command in this guide (and every operator habit built on it) still
uses `team` by default, and revoking `admin`'s PAT in the last step then breaks all of them.
`login` takes its own `--profile` flag (`packages/lakana/src/lakana/cli/auth.py`), so it goes
after `login`, not before it.

Only a principal with the `ADMIN` role on the instance can hand out access, change groups and
issue or revoke tokens, their own tokens included. The three permissions behind this
(`MANAGE_GRANT`, `MANAGE_GROUP`, `MANAGE_TOKEN`) cannot be granted on their own: a request for one
is refused with `MANAGE_GRANT comes only with the ADMIN role on the instance; grant that role
instead, or nothing.` Anyone else who tries to use one gets `MANAGE_GRANT needs the ADMIN role on
the instance. Ask an administrator to do this, or to give you the role`, followed by the command
to run. A single-action grant of one of them that was made before this rule has no effect;
`lakana grant list` marks it `no effect` in a `note` column (`--output json` has the full note,
`no effect: <ACTION> needs the ADMIN role`). See
[Upgrade policy](../explanation/upgrades.md#delegation-needs-the-admin-role).

## Starting over

This is for a Compose deployment. For the instance on your own machine, stop it and run
`lakana server reset --local` (see "Getting started"); that command refuses a Compose or remote
instance.

<!-- not tested: moved as is; sorted in the second pull request -->
```bash
docker compose down -v
```

then delete the bundle directory. `down -v` removes the named volumes (`pgdata`, `seaweed`),
so this discards the catalog and all stored data — there is no undo short of a backup.

## The image

`${LAKANA_IMAGE}` is one image (`Dockerfile`, repo root) that runs the server, the worker, and
`setup`, differing only in the command Compose passes each:

- **Python 3.14** (`python:3.14-slim`). A project with Python assets must admit it in
  `requires-python`; the worker's virtual environments use this interpreter.
- **Runs as uid 10001** (`groupadd`/`useradd --system --uid 10001`), never root, with
  `HOME=/home/lakana`.
- **Ships `uv`** (`/usr/local/bin/uv`, MIT OR Apache-2.0), copied from the pinned
  `ghcr.io/astral-sh/uv` image by tag and digest, for the worker's Python-asset venvs. The
  worker's cache directory `/home/lakana/.cache/lakana-worker` exists, owned by uid 10001, mode
  `0700` (see "Python assets").
- **tini is PID 1** (`ENTRYPOINT ["/usr/bin/tini", "--"]`), so `docker stop` forwards
  `SIGTERM` immediately and reaps the actual process cleanly — no shell wrapper delaying the
  signal until the stop timeout expires.
- **DuckDB's `httpfs` extension is preinstalled** at build time
  (`duckdb.connect().execute('INSTALL httpfs')`), so the worker never downloads an extension
  the first time it runs a query.
- **Dependencies are locked.** The image is built from wheels pinned by `uv.lock`
  (`uv export --frozen ... -o dist/constraints.txt`, passed to `pip install -c
  constraints.txt` in the build), so an image build always resolves the exact versions the
  repo's lockfile pins, not whatever is newest on PyPI that day.
- **`HEALTHCHECK` probes `http://127.0.0.1:<LAKANA_PORT>/healthz`.** `LAKANA_HOST` (the address
  `lakana-server` itself binds to inside the container) must stay `0.0.0.0` or a loopback
  address — the image sets `LAKANA_HOST=0.0.0.0` by default — or the in-container healthcheck
  can no longer reach it and Compose will report the container unhealthy even though it's
  otherwise fine.
- **A read-only root filesystem** (for example running this image under Kubernetes instead of
  Compose, tracked as #34) needs `/var/tmp/lakana` (`TMPDIR`) and `/home/lakana` (`HOME`) mounted
  as writable volumes — everything else the image touches is read-only-compatible.

## Behind a proxy or with an internal CA

Workers fetch `https` repositories with git, which never sees the container's `HTTPS_PROXY`
or CA variables. Set `LAKANA_GIT__HTTPS_PROXY`, `LAKANA_GIT__NO_PROXY` and `LAKANA_GIT__CA_BUNDLE`
instead, by hand in `compose.yaml` on the `worker` service only (next to `<<: *lakana-env`; the
server and `migrate` need neither), and mount the CA file read-only next to the `repos` mount:

```yaml
    environment:
      LAKANA_GIT__HTTPS_PROXY: http://proxy.corp:3128
      LAKANA_GIT__NO_PROXY: gitlab.corp,.internal
      LAKANA_GIT__CA_BUNDLE: /certs/corp-ca.pem
    volumes: ["./repos:/repos:ro", "./certs/corp-ca.pem:/certs/corp-ca.pem:ro"]
```

The worker refuses to start if the CA file is not a readable file; the proxy must be an
`http(s)://` URL, with `user:password` percent-encoded if it has credentials.
Details and limits (SSH is unaffected): [`projects.md`](projects.md#git-network-settings), "Git network settings".

## Known gaps

- **A venv build is not interrupted by a shutdown (#48).** `docker stop` (and Compose
  restarts/`down`) sends `SIGTERM`: the worker stops claiming and lets a running task finish for
  up to 20 s (`LAKANA_RUN__DRAIN_GRACE_SECONDS`) before releasing it to another worker; the
  bundle sets `stop_grace_period: 45s`. A task whose venv is still being built is not stopped by
  that. If Compose sends `SIGKILL` first, or the worker is lost, the platform ends what it held
  after about a minute: queries and deploys `FAILED` with the cause `worker_lost` (a write that
  had already committed ends `SUCCEEDED`), tasks released or reclaimed. Nothing stays `RUNNING`
  for ever; see [A worker that is lost](stop-a-query.md#a-worker-that-is-lost) and
  [When a worker is lost](why-is-it-waiting.md#when-a-worker-is-lost).
- **SeaweedFS's internal HTTP APIs are closed to the worker (#14); what is and is not protected.**
  SeaweedFS's master (9333), volume server (8080) and filer have HTTP APIs that need no
  credentials. They run in `seaweedfs-core`, on the `seaweed-internal` network, which only
  SeaweedFS's own containers (`seaweedfs-core`, the `seaweedfs` S3 gateway and the one-shot
  `bucket`) join and which has no route out of the host. The server and every worker are not on it:
  from a worker, `seaweedfs-core` does not resolve, and `seaweedfs` answers over HTTP on port 8333 only, with
  S3 that needs the keys. The filer also runs with `-disableHttp`. **Not protected:** the gateway's gRPC port 18333 (it includes IAM) has no authentication and is reachable from the worker. User SQL cannot use it, because DuckDB's HTTP filesystem cannot speak gRPC, but a Python task, which runs as trusted code in the worker, could remove the storage identity and make storage unavailable until a restart. The isolation relies on Docker's own network isolation (the default); `iptables: false`, Podman or rootless setups may not provide it. A person
  with Docker access to the host can still reach SeaweedFS (`docker exec`), and the S3 keys in
  `.env` give full access to the `lakana` bucket, as always. The pinned SeaweedFS version is updated with Lakana releases.

## See also

`docs/agent/security.md` and `docs/security/threat-model.md` describe what `.env` and the
bootstrap credential protect in team mode, and what team mode explicitly does not defend
against.
