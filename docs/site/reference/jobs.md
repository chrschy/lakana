# Jobs

What `GET /jobs` and `lakana job list` show for a job, and in particular the triggers that start
it: the fields, each kind of trigger, the route, the commands and the SDK. For the steps, see
[Run a job on a schedule](../how-to/run-a-job-on-a-schedule.md).

## Triggers

Every job has a list `triggers`, the ways it starts. The list is read-only: a schedule is
declared in `lakana.yaml` and changed by a deploy, and pausing it is a separate call
(`lakana job pause`). Schedule comes first.

| `kind` | Present | Fields |
|---|---|---|
| `schedule` | when the job has a schedule in this environment | `cron`, `timezone` (IANA), `paused` (true or false), `next_run_at` (UTC; `null` while paused) |
| `on_demand` | always: every job can be started through the API, the SDK or the CLI | none |

`next_run_at` is the time the scheduler will fire next, the same value as the job's `next_run_at`;
a paused schedule fires at no time, so it is `null`.

Triggers may gain kinds in a later release, so **a client must ignore a `kind` it does not
know**. The SDK does so.

## REST

| Route | Needs | Answer |
|---|---|---|
| `GET /api/v1/environments/{env}/jobs?repository=` | `READ_ENVIRONMENT` | a list of jobs, by repository and name |
| `GET /api/v1/environments/{env}/jobs/{job}?repository=` | `READ_ENVIRONMENT` | one job, the same body as its entry in the list |

Both answer the fields `name`, `environment`, `repository`, `select`, `upstream`, `downstream`,
`cron`, `timezone`, `state`, `paused`, `next_run_at`, `next_run_local`, `waiting_for`,
`waits_for_run_id`, `last_run_id`, `last_run_status`, `last_run_trigger`, `last_run_at` and
`triggers`. `cron`, `timezone` and `state` are unchanged. The pause and resume routes answer
the same body.

`repository` is needed only when several repositories declare a job of the same name; without
it the answer is `409` and names them. A job that does not exist is `404` and lists the jobs that
do.

## Commands and SDK

| Command | Shows |
|---|---|
| `lakana job list [--env ENV] [--repo REPO]` | one row per job with a `triggers` column that holds each trigger on its own line, for example `schedule 0 6 * * * Europe/Berlin` and `on demand`; a paused schedule reads `schedule 0 6 * * * Europe/Berlin (paused)` |
| `lakana job show NAME [--env ENV] [--repo REPO]` | the job's state, selection and last run, and each trigger on its own line with the next run of a schedule |

With `--output json` both print the whole body, `triggers` included.

`client.jobs.list(environment)` and `client.jobs.get(environment, job, repository=None)` return
`Job` objects whose `triggers` is a tuple of `Trigger` (`kind`, `cron`, `timezone`, `paused`,
`next_run_at`; the fields a kind does not have are `None`). From a server that does not send
triggers the tuple is empty.
