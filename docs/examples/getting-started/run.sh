#!/usr/bin/env bash
set -euo pipefail
# The commands of the "Getting started" tutorial, in order. The page shows the regions;
# the lines between regions are the checks. Run by scripts/run_docs_examples.sh, which
# starts the local instance (start.sh) and sets EXAMPLE_DIR and WORK. It starts in WORK, a
# folder of the private home the runner sets, as a reader starts in some folder of theirs.
cd "$WORK"

# The page quotes the output of the deploy and the run. The commands of those two regions are
# run as they are shown; their output is written to a file between capture_start and
# capture_end, printed again afterwards, and checked for the lines that do not change from
# one machine to the next (identifiers, commits and durations do).
captured=""
capturing=""
capture_start() {
  rm -f "$captured"
  captured=$(mktemp)
  capturing=yes
  exec 3>&1 4>&2 >"$captured" 2>&1
}
capture_end() {
  exec >&3 2>&4 3>&- 4>&-
  capturing=""
  cat "$captured"
}
# If a command fails inside a capture, still show what it printed. Always remove the file.
trap '[ -z "$capturing" ] || { exec >&3 2>&4; cat "$captured"; }; rm -f "$captured"' EXIT

# docs:start version
lakana --version
# docs:end version
# The instance is up, so both lines are printed: the CLI's version and the instance's.
lakana --version | grep -q '^instance http://'

capture_start
# docs:start init
cd ~
lakana init analytics
cd analytics
# docs:end init
capture_end
grep -Eq '^Created a Lakana project in .*analytics\.$' "$captured"
grep -Eq "^  lakana run --env dev$" "$captured"
grep -Fq "If 'git commit' says it does not know who you are" "$captured"
test -f lakana.yaml

# docs:start remove-sample
rm -r assets/bronze/example.sql checks
# docs:end remove-sample
test ! -e assets/bronze/example.sql
test ! -e checks

# The tutorial shows each of these files by its own path; here they are copied in.
cp -R "$EXAMPLE_DIR/project/." .
test "$(find assets -name '*.sql' | wc -l)" -eq 2
test "$(find checks -name '*.sql' | wc -l)" -eq 1

# docs:start git-identity
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
# docs:end git-identity
test "$(git config --global user.email)" = "you@example.com"

# docs:start commit
git init
git add -A
git commit -m "First assets"
# docs:end commit

capture_start
# docs:start deploy
lakana repo add analytics --kind local --url "$PWD"
lakana deploy --env dev
# docs:end deploy
capture_end
grep -q '^Registered repository analytics' "$captured"
grep -Fxq "Hint: run 'lakana run --env dev' to run what you just deployed." "$captured"
grep -Eq '^Deployed analytics in dev at [0-9a-f]{40}$' "$captured"
grep -Eq '^│ added +│ bronze\.readings +│$' "$captured"
grep -Eq '^│ added +│ silver\.station_temperatures +│$' "$captured"
grep -Fxq 'Checks:' "$captured"
grep -Fxq '  silver.station_temperatures: plausible_range' "$captured"

capture_start
# docs:start run
lakana run --env dev
# docs:end run
capture_end
grep -Eq '^Run [0-9a-f-]{36} succeeded$' "$captured"
grep -Eq '^│ bronze\.readings +│ succeeded +│ 1/3 +│ [0-9.]+s +│ 4 +│ +│$' "$captured"
grep -Eq '^│ silver\.station_temperatures +│ succeeded +│ 1/3 +│ [0-9.]+s +│ 2 +│ +│$' "$captured"
grep -Eq '^silver\.station_temperatures +passed +1 of 1 checks passed$' "$captured"
grep -Eq '^  plausible_range +passed$' "$captured"

# docs:start query
lakana sql "SELECT station_id, avg_temp_c FROM silver.station_temperatures ORDER BY station_id"
# docs:end query
out=$(lakana --output json sql "SELECT station_id, avg_temp_c FROM silver.station_temperatures ORDER BY station_id")
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [{"station_id":"A1","avg_temp_c":5.0},{"station_id":"B2","avg_temp_c":2.0}], sys.argv[1]' "$out"

# The how-to "Stop a query" shows how to find a query, and "Deploy with Docker Compose" how to see
# the workers. Every query of this instance has ended by now, so the first listing needs --all;
# the second lists the one worker the local instance started.
# docs:start query-list
lakana query list --env dev --all
# docs:end query-list
out=$(lakana --output json query list --env dev --all)
python3 -c 'import json,sys; q=json.loads(sys.argv[1]); assert isinstance(q, list) and q and all(x["status"] == "SUCCEEDED" and not {"sql_text","error_message","lineage"} & set(x) for x in q), q' "$out"
out=$(lakana --output json query list --env dev)
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [], sys.argv[1]' "$out"

# docs:start worker-list
lakana worker list
# docs:end worker-list
out=$(lakana --output json worker list)
python3 -c 'import json,sys; w=json.loads(sys.argv[1]); assert len(w) == 1 and w[0]["status"] == "alive" and w[0]["takes"] == "all" and w[0]["query_run_id"] is None, w' "$out"

# The how-to "Why is my query or task waiting?" names the three commands an operator looks at.
# docs:start scheduler-status
lakana scheduler status
# docs:end scheduler-status
out=$(lakana --output json scheduler status)
python3 -c 'import json,sys; s=json.loads(sys.argv[1]); assert set(s) == {"holder", "heartbeat_at", "active_runs", "pending", "ready", "running"} and s["active_runs"] == 0, s' "$out"

# The how-to "Use a private package index" shows how an index and its secret are registered. That
# never contacts the index, and a local instance has a secrets master key, so the commands run
# as the page shows them (the index's address is the page's own example).
cd "$WORK"
printf '%s\n' '{"username": "ci-reader", "password": "the-password-or-token"}' > credential.json
# docs:start private-index-register
lakana index add team --url https://pkgs.example.org/simple --credential-secret team-index
lakana secret create team-index --env dev --values-stdin < credential.json
# docs:end private-index-register
out=$(lakana --output json index show team)
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["url"] == "https://pkgs.example.org/simple" and d["credential_secret"] == "team-index", d' "$out"
# The listing is read whole before it is searched: `grep -q` stops at the first match, and a
# command still writing into the closed pipe would fail the script under `pipefail`.
out=$(lakana secret list --env dev)
grep -q 'team-index' <<<"$out"

# docs:start private-index-rotate
lakana secret rotate team-index --env dev --values-stdin < credential.json
# docs:end private-index-rotate
out=$(lakana secret list --env dev)
grep -q 'team-index' <<<"$out"

# docs:start private-index-clear
lakana index update team --clear credential_secret
# docs:end private-index-clear
out=$(lakana --output json index show team)
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["credential_secret"] is None, d' "$out"

# The upgrade note "Delegation needs the ADMIN role" tells an operator how to find who holds the
# ADMIN role and what each administrator created. A local instance has its bootstrap
# administrator (`admin`), so the commands run as the page shows them.
# docs:start delegation-check
lakana whoami
lakana principal list
lakana --output json grant list
lakana group list
# docs:end delegation-check
out=$(lakana --output json grant list)
python3 -c 'import json,sys; g=json.loads(sys.argv[1]); assert any(x["role"] == "ADMIN" and x["kind"] == "INSTANCE" and x["principal_id"] for x in g), g' "$out"

# docs:start delegation-audit
lakana --output json audit list --event-type GRANT_CREATED --all
lakana --output json audit list --event-type GROUP_MEMBER_ADDED --all
lakana --output json audit list --event-type PAT_ISSUED --all
# docs:end delegation-audit
out=$(lakana --output json audit list --event-type GRANT_CREATED --all)
python3 -c 'import json,sys; e=json.loads(sys.argv[1]); assert e and all(x["event_type"] == "GRANT_CREATED" and x["principal_id"] for x in e), e' "$out"
