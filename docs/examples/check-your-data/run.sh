#!/usr/bin/env bash
set -euo pipefail
# The commands of the how-to "Check your data", in order. The page shows the regions; the
# lines between regions are the checks. Run by scripts/run_docs_examples.sh, which starts the
# local instance (start.sh) and sets EXAMPLE_DIR and WORK. It starts in WORK, a folder of the
# private home the runner sets.
cd "$WORK"
# The page shows tables as a terminal of 120 columns draws them; without a terminal the CLI
# assumes 80, which wraps every cell.
export COLUMNS=120

# The page quotes the output of several commands. Each is run as it is shown; its output is
# written to a file between capture_start and capture_end, printed again afterwards, and checked
# for the lines that do not change from one machine to the next (identifiers, commits and
# durations do).
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

# The id of the newest run of the environment `dev`.
latest_run() {
  lakana --output json runs list --env dev --limit 1 \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])'
}

# The run's status, and the state and message of one task, as JSON the checks below read.
task_of() {
  lakana --output json runs show "$1" --env dev \
    | python3 -c 'import json,sys; r=json.load(sys.stdin); t=[x for x in r["tasks"] if x["asset"]==sys.argv[1]][0]; print(json.dumps({"run": r["status"], "task": t["status"], "message": t.get("error_message") or ""}))' "$2"
}

# A project of two assets and three checks, as `lakana init` writes the folders.
cd ~
lakana init readings --no-sample >/dev/null
cd readings
cp -R "$EXAMPLE_DIR/project/." .
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
git init -q
git add -A
git commit -q -m "Readings and three checks"
lakana repo add readings --kind local --url "$PWD" >/dev/null

capture_start
# docs:start list-checks
find checks -name '*.sql' | sort
# docs:end list-checks
capture_end
test "$(wc -l <"$captured")" -eq 3
grep -Fxq 'checks/silver/station_temperatures/no_missing_station.sql' "$captured"

capture_start
# docs:start deploy-dry-run
lakana deploy --env dev --dry-run
# docs:end deploy-dry-run
capture_end
grep -Eq '^Dry run of readings in dev at [0-9a-f]{40}$' "$captured"
grep -Fxq 'Checks:' "$captured"
grep -Fxq '  silver.station_temperatures: has_rows, no_missing_station, plausible_range (warn)' "$captured"

lakana deploy --env dev >/dev/null

# The run fails, so the command's exit status is not zero: the region shows the command as
# a reader types it, and the lines around it let the script go on and test the status.
trap - ERR
set +e
capture_start
# docs:start run-failing
lakana run --env dev
# docs:end run-failing
status=$?
capture_end
set -e
trap 'echo "run.sh line ${LINENO}: ${BASH_COMMAND}" >&2' ERR
test "$status" -ne 0
grep -Eq '^Run [0-9a-f-]{36} failed$' "$captured"
grep -Eq '^│ bronze\.readings +│ succeeded +│' "$captured"
grep -Eq '^│ silver\.station_temperatures +│ failed +│' "$captured"
grep -Eq '^silver\.station_temperatures +FAILED +2 of 3 checks passed, not published$' "$captured"
grep -Eq '^  no_missing_station +FAILED +1 row$' "$captured"
grep -Eq '^  plausible_range +warned +1 row$' "$captured"
grep -Eq '^  has_rows +passed$' "$captured"

RUN_ID=$(latest_run)
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["run"] == "failed" and d["task"] == "failed", d; m=d["message"]; assert m.startswith("Check `silver.station_temperatures/no_missing_station` failed: 1 row (checks/silver/station_temperatures/no_missing_station.sql). The table is not published: it had no published data before this run. Nothing downstream was built. Look at the rows: `lakana check rows " + sys.argv[2] + " silver.station_temperatures/no_missing_station --env dev`. After a fix, deploy and run again; if the project did not change: `lakana runs resume "), m' "$(task_of "$RUN_ID" silver.station_temperatures)" "$RUN_ID"

capture_start
# docs:start show
lakana runs show "$RUN_ID" --env dev
# docs:end show
capture_end
grep -Eq "^│ id +│ $RUN_ID +│\$" "$captured"
grep -Eq '^│ status +│ failed +│$' "$captured"
grep -Eq '^silver\.station_temperatures +FAILED +2 of 3 checks passed, not published$' "$captured"

capture_start
# docs:start check-list
lakana check list --env dev --run "$RUN_ID"
# docs:end check-list
capture_end
grep -Eq '^│ [0-9a-f]{8} +│ silver\.station_temperatures +│ no_missing_station +│ failed +│ 1 +│' "$captured"

# The rows the failed check found. The table was not published, so they are in no table; they are
# shown from the snapshot the check read.
capture_start
# docs:start check-rows
lakana check rows "$RUN_ID" silver.station_temperatures/no_missing_station --env dev
# docs:end check-rows
capture_end
grep -Eq '^│ +│ 2\.0 +│$' "$captured"
grep -Fxq '1 rows' "$captured"

# The table exists and is not published: a read says why.
capture_start
# docs:start table-get
lakana table get silver.station_temperatures --env dev
# docs:end table-get
capture_end
grep -Eq '^│ state +│ not published +│$' "$captured"
grep -Fq '`silver.station_temperatures` has not been published: its first build failed the check `no_missing_station` (run' "$captured"
grep -Fq 'It is published by the next run in which its checks pass.' "$captured"
grep -Fq 'Look at the rows: `lakana check rows ' "$captured"
# A read of the table is refused, and says the same.
trap - ERR
set +e
capture_start
# docs:start read-unpublished
lakana sql "SELECT count(*) FROM silver.station_temperatures"
# docs:end read-unpublished
status=$?
capture_end
set -e
trap 'echo "run.sh line ${LINENO}: ${BASH_COMMAND}" >&2' ERR
test "$status" -ne 0
grep -Fq '`silver.station_temperatures` has not been published: its first build failed the check `no_missing_station` (run' "$captured"

# The rows that are wrong are in the data the table was built from.
capture_start
# docs:start find-rows
lakana sql "SELECT station_id, temp_c FROM bronze.readings WHERE station_id IS NULL"
# docs:end find-rows
capture_end
grep -Eq '^│ +│ 2\.0 +│$' "$captured"

capture_start
# docs:start run-logs
lakana runs logs "$RUN_ID" --env dev
# docs:end run-logs
capture_end
grep -Fxq '  rollback: unpublished' "$captured"
grep -Fxq '  check no_missing_station: failed, 1 row' "$captured"

# The fix: every reading gets a station. Commit it, deploy it and run again.
cp "$EXAMPLE_DIR/project-fixed/assets/bronze/readings.sql" assets/bronze/readings.sql
capture_start
# docs:start fix-deploy
git add -A
git commit -m "Give every reading a station"
lakana deploy --env dev
# docs:end fix-deploy
capture_end
grep -Eq '^Deployed readings in dev at [0-9a-f]{40}$' "$captured"

capture_start
# docs:start run-fixed
lakana run --env dev
# docs:end run-fixed
capture_end
grep -Eq '^Run [0-9a-f-]{36} succeeded$' "$captured"
grep -Eq '^silver\.station_temperatures +warned +3 of 3 checks passed$' "$captured"
grep -Eq '^  no_missing_station +passed$' "$captured"
grep -Eq '^  plausible_range +warned +1 row$' "$captured"
grep -Fq 'note: 1 check warned: `plausible_range` (1 row).' "$captured"

# A check that only warns leaves its rows in the published table: the check's own SELECT finds them.
capture_start
# docs:start warn-rows
lakana sql "SELECT station_id, avg_temp_c FROM silver.station_temperatures WHERE avg_temp_c < -90 OR avg_temp_c > 60"
# docs:end warn-rows
capture_end
grep -Eq '^│ C3 +│ 500\.5 +│$' "$captured"

capture_start
# docs:start table-get-published
lakana table get silver.station_temperatures --env dev
# docs:end table-get-published
capture_end
grep -Eq '^│ state +│ published +│$' "$captured"
out=$(lakana --output json sql "SELECT station_id, avg_temp_c FROM silver.station_temperatures ORDER BY station_id")
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [{"station_id":"A1","avg_temp_c":5.0},{"station_id":"B2","avg_temp_c":2.0},{"station_id":"C3","avg_temp_c":500.5}], sys.argv[1]' "$out"

# Break the data again: the table already holds the second run's rows, and they come back.
cp "$EXAMPLE_DIR/project/assets/bronze/readings.sql" assets/bronze/readings.sql
capture_start
# docs:start break-deploy
git add -A
git commit -m "Break the data again"
lakana deploy --env dev
# docs:end break-deploy
capture_end
trap - ERR
set +e
capture_start
# docs:start run-rolled-back
lakana run --env dev
# docs:end run-rolled-back
status=$?
capture_end
set -e
trap 'echo "run.sh line ${LINENO}: ${BASH_COMMAND}" >&2' ERR
test "$status" -ne 0
grep -Eq '^Run [0-9a-f-]{36} failed$' "$captured"
grep -Eq '^silver\.station_temperatures +FAILED +2 of 3 checks passed, rolled back$' "$captured"
grep -Fq 'The table was set back to what it was before this run, and' "$captured"
# The table still holds the second run's rows.
capture_start
# docs:start after-rollback
lakana sql "SELECT station_id, avg_temp_c FROM silver.station_temperatures ORDER BY station_id"
# docs:end after-rollback
capture_end
out=$(lakana --output json sql "SELECT station_id, avg_temp_c FROM silver.station_temperatures ORDER BY station_id")
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [{"station_id":"A1","avg_temp_c":5.0},{"station_id":"B2","avg_temp_c":2.0},{"station_id":"C3","avg_temp_c":500.5}], sys.argv[1]' "$out"
RUN3=$(latest_run)
# The table was set back, so the check's own SELECT finds nothing in it now; the rows the check
# found are still shown, as they were in that run.
capture_start
# docs:start check-rows-rolled-back
lakana check rows "$RUN3" silver.station_temperatures/no_missing_station --env dev
# docs:end check-rows-rolled-back
capture_end
grep -Eq '^│ +│ 2\.0 +│$' "$captured"
grep -Fxq '1 rows' "$captured"
out=$(lakana --output json check list --env dev --run "$RUN3")
python3 -c 'import json,sys; r={x["name"].split("/")[1]: (x["outcome"], x["rollback_outcome"]) for x in json.loads(sys.argv[1])}; assert r["no_missing_station"] == ("failed", "rolled_back"), r' "$out"

# The five patterns of the page, against tables that make each of them pass: the fixed readings,
# a table of stations that silver.station_temperatures joins, and eight checks in all.
cp "$EXAMPLE_DIR/project-fixed/assets/bronze/readings.sql" assets/bronze/readings.sql
cp -R "$EXAMPLE_DIR/patterns/." .
capture_start
# docs:start patterns-deploy
git add -A
git commit -m "Five more checks"
lakana deploy --env dev
# docs:end patterns-deploy
capture_end
grep -Fxq '  silver.station_temperatures: accepted_values, has_rows, no_missing_station, no_missing_value, not_empty, plausible_range (warn), station_is_known, unique_key' "$captured"
capture_start
# docs:start run-patterns
lakana run --env dev
# docs:end run-patterns
capture_end
grep -Eq '^Run [0-9a-f-]{36} succeeded$' "$captured"
grep -Eq '^silver\.station_temperatures +warned +8 of 8 checks passed$' "$captured"
RUN4=$(latest_run)
out=$(lakana --output json check list --env dev --run "$RUN4")
python3 -c 'import json,sys; r={x["name"].split("/")[1]: x["outcome"] for x in json.loads(sys.argv[1])}; assert r == {"accepted_values":"passed","has_rows":"passed","no_missing_station":"passed","no_missing_value":"passed","not_empty":"passed","plausible_range":"warned","station_is_known":"passed","unique_key":"passed"}, r' "$out"

# A check with `on_failure: keep`: the table keeps the data that failed, and nothing that reads it
# is built until its checks pass.
cp -R "$EXAMPLE_DIR/keep/." .
capture_start
# docs:start keep-deploy
git add -A
git commit -m "Keep the battery levels when they fail"
lakana deploy --env dev
# docs:end keep-deploy
capture_end
grep -Fxq '  bronze.sensor_log: battery_in_range (keep)' "$captured"

trap - ERR
set +e
capture_start
# docs:start keep-run
lakana run --select bronze.sensor_log --downstream --env dev
# docs:end keep-run
status=$?
capture_end
set -e
trap 'echo "run.sh line ${LINENO}: ${BASH_COMMAND}" >&2' ERR
test "$status" -ne 0
grep -Eq '^Run [0-9a-f-]{36} failed$' "$captured"
grep -Eq '^│ bronze\.sensor_log +│ failed +│' "$captured"
grep -Eq '^│ silver\.battery_report +│ upstream_failed +│' "$captured"
grep -Eq '^bronze\.sensor_log +FAILED +0 of 1 checks passed$' "$captured"
grep -Eq '^  battery_in_range +failed \(kept\) +1 row$' "$captured"
KEPT_RUN=$(latest_run)
python3 -c 'import json,sys; m=json.loads(sys.argv[1])["message"]; assert "The table keeps this run'"'"'s data (`on_failure: keep`), and nothing that reads it was built." in m, m' "$(task_of "$KEPT_RUN" bronze.sensor_log)"

# The table is kept with the data that failed, and a reader of the platform sees it.
capture_start
# docs:start keep-table-get
lakana table get bronze.sensor_log --env dev
# docs:end keep-table-get
capture_end
grep -Eq '^│ state +│ kept +│$' "$captured"
out=$(lakana --output json table get bronze.sensor_log --env dev)
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["state_message"] == "Kept after a failed check (`bronze.sensor_log/battery_in_range`, run " + sys.argv[2] + "): the table holds the data that failed. Tasks do not read it until its checks pass.", d' "$out" "$KEPT_RUN"

capture_start
# docs:start keep-rows
lakana sql "SELECT sensor_id, battery_pct FROM bronze.sensor_log WHERE battery_pct > 100"
# docs:end keep-rows
capture_end
grep -Eq '^│ S3 +│ 140 +│$' "$captured"

# A run that builds only the reader checks the kept table first, finds it still failing and stops.
trap - ERR
set +e
capture_start
# docs:start keep-run-reader
lakana run --select silver.battery_report --env dev
# docs:end keep-run-reader
status=$?
capture_end
set -e
trap 'echo "run.sh line ${LINENO}: ${BASH_COMMAND}" >&2' ERR
test "$status" -ne 0
grep -Eq '^│ bronze\.sensor_log +│ failed +│' "$captured"
grep -Eq '^│ silver\.battery_report +│ upstream_failed +│' "$captured"
grep -Eq '^bronze\.sensor_log +FAILED +0 of 1 checks passed, kept after a failed check$' "$captured"
python3 -c 'import json,sys; m=json.loads(sys.argv[1])["message"]; assert sys.argv[2] in m, m' "$(task_of "$(latest_run)" bronze.sensor_log)" "\`bronze.sensor_log\` was kept after a failed check in run $KEPT_RUN and still fails, so the assets that read it were not built."
out=$(lakana --output json table get bronze.sensor_log --env dev)
python3 -c 'import json,sys; assert json.loads(sys.argv[1])["state"] == "kept", sys.argv[1]' "$out"

# Repair the data, deploy, and run: the checks pass and the table is published again.
cp "$EXAMPLE_DIR/keep-repaired/assets/bronze/sensor_log.sql" assets/bronze/sensor_log.sql
capture_start
# docs:start keep-repair-deploy
git add -A
git commit -m "Cap the battery level at 100"
lakana deploy --env dev
# docs:end keep-repair-deploy
capture_end
grep -Eq '^Deployed readings in dev at [0-9a-f]{40}$' "$captured"
capture_start
# docs:start keep-run-repaired
lakana run --select bronze.sensor_log --downstream --env dev
# docs:end keep-run-repaired
capture_end
grep -Eq '^Run [0-9a-f-]{36} succeeded$' "$captured"
grep -Eq '^bronze\.sensor_log +passed +1 of 1 checks passed$' "$captured"
capture_start
# docs:start keep-published
lakana table get bronze.sensor_log --env dev
# docs:end keep-published
capture_end
grep -Eq '^│ state +│ published +│$' "$captured"
out=$(lakana --output json sql "SELECT sensor_id FROM silver.battery_report")
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [{"sensor_id":"S2"}], sys.argv[1]' "$out"

# A table the project only reads: someone else loaded it. The project builds a table from it.
# docs:start source-table
lakana sql "SELECT 'S1' AS sensor_id, 'A1' AS station_id UNION ALL SELECT 'S2', NULL" \
  --into bronze.sensors --mode create
# docs:end source-table
cp -R "$EXAMPLE_DIR/source-check/." .
git add -A
git commit -q -m "Check the sensors table"
capture_start
lakana deploy --env dev
capture_end
trap - ERR
set +e
capture_start
# docs:start run-source
lakana run --env dev
# docs:end run-source
status=$?
capture_end
set -e
trap 'echo "run.sh line ${LINENO}: ${BASH_COMMAND}" >&2' ERR
test "$status" -ne 0
grep -Eq '^Run [0-9a-f-]{36} failed$' "$captured"
grep -Eq '^bronze\.sensors +FAILED +0 of 1 checks passed, not built by this project$' "$captured"
grep -Eq '^│ silver\.sensor_summary +│ upstream_failed +│' "$captured"
python3 -c 'import json,sys; m=json.loads(sys.argv[1])["message"]; assert "This project does not build `bronze.sensors`, so it was not changed; the assets that read it were not built." in m, m' "$(task_of "$(latest_run)" bronze.sensors)"
# Nothing was set back, and the table the failed check read still holds its rows.
out=$(lakana --output json sql "SELECT count(*) AS n FROM bronze.sensors")
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [{"n": 2}], sys.argv[1]' "$out"

# A default for the project's checks: every check without a value of its own now warns, so the
# check of the sensors no longer stops the run.
cat "$EXAMPLE_DIR/default/checks.yaml" >> lakana.yaml
capture_start
# docs:start default-deploy
git add -A
git commit -m "Warn by default"
lakana deploy --env dev
# docs:end default-deploy
capture_end
grep -Fxq '8 checks take `on_failure: warn` from lakana.yaml.' "$captured"
grep -Fxq '  bronze.sensors: every_sensor_has_a_station (warn)' "$captured"
capture_start
# docs:start default-run
lakana run --env dev
# docs:end default-run
capture_end
grep -Eq '^Run [0-9a-f-]{36} succeeded$' "$captured"
grep -Eq '^bronze\.sensors +warned +1 of 1 checks passed, not built by this project$' "$captured"
out=$(lakana --output json check list --env dev --run "$(latest_run)" --table bronze.sensors)
python3 -c 'import json,sys; r=json.loads(sys.argv[1]); assert [(x["outcome"], x["on_failure"]) for x in r] == [("warned", "warn")], r' "$out"
