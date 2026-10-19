# Runs, queries and deploys lists

The three lists that show what happened in an environment: the filters of each, the order, the
cursor that pages through it, who sees what, the commands, the SDK and the routes. For the steps,
see [Find a run, a query or a deploy](../how-to/find-a-run.md).

## The lists

| List | Command | Needs | Order |
|---|---|---|---|
| Runs | `lakana runs list` | `READ_ENVIRONMENT` | creation time, newest first; equal times by id |
| Queries | `lakana query list` | `SUBMIT_QUERY`; `MANAGE_ENVIRONMENT` shows everyone's | running, then queued, then (with `--all`, or an ended status) ended ones |
| Deploys | `lakana deploys list` | `READ_ENVIRONMENT` | creation time, newest first; equal times by id |

Every filter narrows the list; filters combine with AND. A filter is never a way to see more than
the list shows without it.

## Filters

| Filter | Option and parameter | Values | Lists |
|---|---|---|---|
| Repository | `--repo`, `repository` | a repository name; an unknown name is `404` | runs, deploys |
| Status | `--status`, `status` | runs: `queued`, `running`, `succeeded`, `failed`, `cancelled`. Queries: the same words in any letter case (the list shows them in capitals). Repeat the option or separate the values with commas: `failed,cancelled` | runs, queries |
| Trigger | `--trigger`, `trigger` | `schedule`, `api`, `manual`; repeat or separate with commas. A run started with `lakana run` has none and matches no trigger | runs |
| Job | `--job`, `job` | the name of a job | runs |
| Since | `--since`, `since` | an ISO 8601 time with a time zone, for example `2026-10-18T09:30:00Z`; the run was created at or after it | runs |
| Submitter | `--submitter`, `submitter` | a principal's name; none of that name's queries is shown to someone who may not see them | queries |

`--since` on the command line also takes a date (`2026-10-18`) or a time without a time zone, which
is read in the time zone of your computer. The route needs the time zone: `since=2026-10-18T09:30:00`
is refused with `422`.

A query status that has ended (`succeeded`, `failed`, `cancelled`) lists the queries that ended in
the last 24 hours, with or without `--all`. The list never shows older ones.

## Paging

`--limit` (`limit`) is the most rows per page: 1 to 200, 100 by default. When more rows follow, the
route answers with the header `X-Next-Cursor`, and the command prints the line that shows the next
page:

<!-- output -->
```text
More results: lakana runs list --env dev --status failed --limit 50 --cursor eyJrIjogInJ1bnMiLCAibyI6ICIyMDI2LTEwLTE4VDA5OjMwOjEyLjM0NTAwMCswMDowMCIsICJpIjogIjVjMGI3ZDRlLTJmMWEtNGI4ZS05ZDYzLTBhMWU3YzRmOWIyMSJ9
```

With `--output json` the line goes to stderr, so the JSON on stdout stays one list. The cursor is
opaque: pass it back unchanged, with the same filters and the same limit.

A page continues strictly after the last row of the previous one. A run, query or deploy that
arrives while you page is never shown twice, and none that was there is skipped. The one exception
is a query that changes from queued to running between two pages, which moves to another part of
the order of queries. The cursor belongs to its list: the cursor of the runs list is refused by the
queries list, with `422` and a message that names both lists. Start again without `cursor`.

## Refusals

| Cause | Answer |
|---|---|
| a status, trigger or `limit` that is not allowed | `422`; the message names the parameter and the allowed values |
| `since` without a time zone, or not a time | `422`; the message shows how to write it |
| a cursor that was not issued, or came from another list | `422` |
| an unknown repository | `404` |
| a caller who may not read the environment | `403` |

## SDK and REST

| What | SDK | Route |
|---|---|---|
| Runs, one page of runs | `client.runs.list(env, ...)`; `client.runs.list_page(env, ...)` also returns the cursor | `GET /api/v1/environments/{env}/runs?repository=&status=&trigger=&job=&since=&limit=&cursor=` |
| Every run that matches | `client.runs.iter(env, ...)` | the same route, page by page |
| Queries | `client.queries.list(env, ...)`; `client.queries.list_page(env, ...)` | `GET /api/v1/environments/{env}/queries?include_ended=&status=&submitter=&limit=&cursor=` |
| Deploys | `client.deploys.list(env, ...)`; `client.deploys.list_page(env, ...)` | `GET /api/v1/environments/{env}/deploys?repository=&limit=&cursor=` |

The routes answer with a JSON list, as they did before they could be paged, and the cursor of the
next page is in the `X-Next-Cursor` header (absent on the last page). `list` returns the rows of
one page; `list_page` returns a page with `items` and `next_cursor`. The SDK takes a status or
trigger as one word or a list of words, and `since` as a `datetime` with a time zone.

```python
from datetime import UTC, datetime

import lakana

client = lakana.Client()
page = client.runs.list_page(
    "dev", status=["failed", "cancelled"], since=datetime(2026, 10, 18, tzinfo=UTC), limit=50
)
for run in page.items:
    print(run.id, run.status, run.job)
if page.next_cursor is not None:
    more = client.runs.list_page(
        "dev",
        status=["failed", "cancelled"],
        since=datetime(2026, 10, 18, tzinfo=UTC),
        limit=50,
        cursor=page.next_cursor,
    )
```
