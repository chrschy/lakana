#!/usr/bin/env bash
# The M4 exit criterion: clean machine → install → start local →
# create a table → append → query → deploy and run a Python asset that stores an artifact and
# one that reads it, in under 300 s. Runs inside a fresh
# python:3.14-slim container. Args are passed to `pip install`. If
# LAKANA_EXPECTED_VERSION is set, the installed `lakana`'s own version is
# checked against it after install -- proving
# the package actually installed is the one the caller pinned, not some
# other release resolved from an index.
set -euo pipefail
# bc prints a decimal point; printf '%.1f' reads one only in the C locale, so each such
# printf below runs with LC_ALL=C (a German locale otherwise ends the script at the first step).
BUDGET=300
summary=$(mktemp)
t0=$(date +%s.%N)
step() {  # step NAME CMD...
  local name=$1; shift
  local s; s=$(date +%s.%N)
  "$@"
  local e; e=$(date +%s.%N)
  LC_ALL=C printf '| %s | %.1f s |\n' "$name" "$(echo "$e - $s" | bc)" >> "$summary"
}
apt_bc() { command -v bc >/dev/null || { apt-get update -qq && apt-get install -y -qq bc >/dev/null; }; }
apt_bc
# git (deploying a local repository) and uv (building the Python asset's venv) are
# prerequisites of Python assets, not part of the product install: set up before the stopwatch.
apt_git() { command -v git >/dev/null || { apt-get update -qq && apt-get install -y -qq git >/dev/null; }; }
apt_git
# uv is a tool the script needs, not the product under test: pinned by hash (one per platform wheel).
pip install --no-cache-dir --quiet --require-hashes -r "$(dirname "$0")/requirements-uv.txt"
export XDG_CONFIG_HOME=$(mktemp -d) XDG_DATA_HOME=$(mktemp -d) NO_COLOR=1
t0=$(date +%s.%N)   # the stopwatch starts after tool setup, at the install
# The product install under test, as a user types it: unpinned on purpose, hashes would defeat the test.
step "pip install" pip install --no-cache-dir --quiet "$@"
# What the unpinned install resolved, written at once so that a failure further down still names it.
{ echo "<details><summary>Installed packages (pip freeze)</summary>"; echo; echo '```'; pip freeze; echo '```'; echo; echo "</details>"; echo; } \
  | tee -a "${GITHUB_STEP_SUMMARY:-/dev/null}"
verify_version() {
  installed=$(python3 -c 'import lakana; print(lakana.__version__)')
  if [ "$installed" != "$LAKANA_EXPECTED_VERSION" ]; then
    echo "installed lakana version '$installed' != expected '$LAKANA_EXPECTED_VERSION'" >&2
    return 1
  fi
  echo "installed lakana version: $installed"
}
if [ -n "${LAKANA_EXPECTED_VERSION:-}" ]; then
  step "verify installed version" verify_version
fi
log=$(mktemp)
start_local() {
  lakana server start --local > "$log" 2>&1 &
  echo $! > /tmp/lakana.pid
  for _ in $(seq 1 120); do
    grep -q "Lakana is running" "$log" && return 0
    kill -0 "$(cat /tmp/lakana.pid)" 2>/dev/null || { cat "$log"; return 1; }
    sleep 0.5
  done
  cat "$log"; return 1
}
# Stop the server on every exit: a failing step would otherwise leave it on its port.
stop_local() {
  [ -f /tmp/lakana.pid ] || return 0
  kill -INT "$(cat /tmp/lakana.pid)" 2>/dev/null || true
  wait "$(cat /tmp/lakana.pid)" 2>/dev/null || true
}
trap stop_local EXIT
step "lakana server start --local" start_local
step "lakana table create" lakana table create bronze.demo --column id:long --column name:string?
printf 'id,name\n1,alpha\n2,beta\n' > /tmp/demo.csv
step "lakana table append" lakana table append bronze.demo /tmp/demo.csv
query() {
  out=$(lakana --output json sql "SELECT id, name FROM bronze.demo ORDER BY id")
  python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [{"id":1,"name":"alpha"},{"id":2,"name":"beta"}], sys.argv[1]' "$out"
}
step "lakana sql" query
# One Python asset, deployed from a local repository and run by the local worker. Its
# packages come from a wheel directory fetched with the same pip arguments as the install above
# (minus the lakana requirement, replaced by lakana[assets]), then locked offline: the venv build
# asks no index. The `lakana` wheel is the one that was just installed, not a release from PyPI.
here=$(cd "$(dirname "$0")" && pwd)
project="$HOME/demo"
python_asset() {
  local opts=() arg
  for arg in "$@"; do case $arg in lakana*) ;; *) opts+=("$arg");; esac; done
  local version python wheels
  version=$(python3 -c 'import lakana; print(lakana.__version__)')
  python=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
  wheels=$(mktemp -d)
  pip download --no-cache-dir --quiet --dest "$wheels" "${opts[@]}" "lakana[assets]==$version"
  lakana init "$project"
  bash "$here/python_asset_project.sh" "$project" "$wheels" "$python" artifact
  git -C "$project" init -q
  git -C "$project" -c user.name=five -c user.email=five@lakana.invalid add -A
  git -C "$project" -c user.name=five -c user.email=five@lakana.invalid commit -qm "five-minute"
  lakana repo add demo --kind local --url "$project"
  out=$(lakana --output json deploy demo --env dev)
  python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["status"]=="succeeded", d' "$out"
  lakana run demo --env dev
  out=$(lakana --output json sql "SELECT id, doubled FROM silver.doubled")
  python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [{"id":1,"doubled":2}], sys.argv[1]' "$out"
  # The same run stored a model as an artifact (local files, no object storage) and the
  # next task read it: train, then predict, in local mode.
  out=$(lakana --output json sql "SELECT id, doubled FROM gold.by_model")
  python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [{"id":1,"doubled":2}], sys.argv[1]' "$out"
  out=$(lakana --output json artifact list --env dev)
  python3 -c 'import json,sys; a=json.loads(sys.argv[1]); assert [(x["name"], x["current_version"]) for x in a] == [("doubler", 1)], a' "$out"
}
step "python asset and artifact: init, lock, deploy, run" python_asset "$@"
total=$(echo "$(date +%s.%N) - $t0" | bc)
stop_local
{ echo "### Five-minute test"; echo; echo "| Step | Time |"; echo "|---|---|"; cat "$summary";
  LC_ALL=C printf '| **total** | **%.1f s** (budget %d s) |\n' "$total" "$BUDGET"; } | tee -a "${GITHUB_STEP_SUMMARY:-/dev/null}"
if (( $(echo "$total > $BUDGET" | bc) )); then echo "Over budget" >&2; exit 1; fi
