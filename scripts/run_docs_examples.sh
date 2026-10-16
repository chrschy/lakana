#!/usr/bin/env bash
# Run the documentation's example projects end to end, each against its own fresh local
# instance. Usage: scripts/run_docs_examples.sh [NAME...]   (default: every example)
#
# An example is docs/examples/NAME/run.sh. It runs with the `lakana` CLI on PATH (start this
# script with `uv run` from the workspace), with these variables set:
#   EXAMPLE_DIR  the example's directory (read it, do not write to it)
#   WORK         an empty directory to work in
# plus NO_COLOR=1 and a private HOME with XDG_CONFIG_HOME and XDG_DATA_HOME (so the example neither
# reads nor touches the real profile, data or git config; a local instance accepts repositories only
# under HOME, and WORK is there). The runner sets no git identity: an example that commits sets one
# itself, as its tutorial tells the reader to. `run.sh` starts in WORK; HOME is its parent.
# The instance is started by the command in the `start` region of the example's start.sh (the
# one its tutorial shows) with `--port <free port>` appended; without a start.sh the default
# `lakana server start --local` is used.
# LAKANA_DOCS_EXAMPLE_BUDGET (default 120) is the number of seconds one example may take. An
# example that needs longer (it waits for a schedule, or builds a Python environment) says so in
# a file `budget` next to its run.sh, one number of seconds; the longer of the two applies.
# The instance is stopped and the directories removed on success, failure, timeout or ^C.
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
examples_dir="$root/docs/examples"
default_budget=${LAKANA_DOCS_EXAMPLE_BUDGET:-120}

names=("$@")
if [ ${#names[@]} -eq 0 ]; then
  mapfile -t names < <(find "$examples_dir" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort)
fi
if [ ${#names[@]} -eq 0 ]; then
  echo "no examples found under docs/examples (expected docs/examples/NAME/run.sh)" >&2
  exit 2
fi
for name in "${names[@]}"; do
  if [ ! -f "$examples_dir/$name/run.sh" ]; then
    echo "no example named '$name' under docs/examples (expected docs/examples/$name/run.sh)" >&2
    exit 2
  fi
done
if ! command -v lakana >/dev/null 2>&1; then
  echo "the 'lakana' command is not on PATH; run this script as 'uv run bash scripts/run_docs_examples.sh'" >&2
  exit 2
fi

summary=$(mktemp)
# Sourced by the bash that runs an example's run.sh (and only by that one): when a command fails,
# print which line it was, so a silent check (`test -f ...`) names itself.
tracer=$(mktemp)
cat > "$tracer" <<'TRACER'
if [ "$0" = "${EXAMPLE_DIR:-}/run.sh" ]; then
  set -E
  trap 'echo "run.sh line ${LINENO}: ${BASH_COMMAND}" >&2' ERR
fi
TRACER
base=""
pid=""
epid=""

# Stop the instance (politely first, then for good) and remove the example's directories.
cleanup() {
  if [ -n "$epid" ]; then
    kill -TERM -- "-$epid" 2>/dev/null || kill -TERM "$epid" 2>/dev/null || true
    for _ in $(seq 1 25); do
      kill -0 "$epid" 2>/dev/null || break
      sleep 0.2
    done
    kill -KILL -- "-$epid" 2>/dev/null || true
    wait "$epid" 2>/dev/null || true
    epid=""
  fi
  if [ -n "$pid" ]; then
    kill -INT "$pid" 2>/dev/null || true
    for _ in $(seq 1 50); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.2
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    fi
    wait "$pid" 2>/dev/null || true
    pid=""
  fi
  if [ -n "$base" ]; then
    rm -rf "$base"
    base=""
  fi
}
trap 'cleanup; rm -f "$summary" "$tracer"' EXIT
trap 'exit 130' INT TERM

# A free port, so a leftover instance (or anything else on 8765) cannot break this run.
free_port() {
  python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'
}

# The words of the example's start command: the `start` region of its start.sh, read with the
# documentation's own block checker, or the default.
start_command() {
  if [ -f "$1/start.sh" ]; then
    PYTHONPATH="$root" python3 -c '
import sys
from pathlib import Path
from scripts.docs_blocks import read_source
root, example = Path(sys.argv[1]), sys.argv[2]
print(read_source(root, f"docs/examples/{example}/start.sh#start").strip())
' "$root" "$(basename "$1")"
  else
    echo "lakana server start --local"
  fi
}

# After a failed example: the logs of the runs that did not succeed, from the instance that is
# still up. What `uv` printed while building a Python asset's environment is a task's output, so
# the instance's log does not hold it; `lakana runs logs` does. Best effort: every command is
# guarded and has a time limit, a failure costs one line, and nothing here changes the exit
# status of the example. At most 30 s in all, whatever the number of runs. The examples all use
# the environment `dev`.
print_failed_run_logs() {
  local env=$1 deadline=$((SECONDS + 30)) listing id status repo shown=0
  echo "--- logs of the runs that did not succeed (lakana runs logs):" >&2
  if ! listing=$(timeout -k 2 15 lakana --output json runs list --env "$env" --limit 20 </dev/null 2>/dev/null); then
    echo "(could not list the runs; try 'lakana runs list --env $env' against a running instance)" >&2
    return 0
  fi
  while IFS=$'\t' read -r id status repo; do
    [ -n "$id" ] || continue
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "(stopped after 30 s; read the rest with 'lakana runs logs RUN_ID --env $env')" >&2
      break
    fi
    shown=$((shown + 1))
    echo "== run $id ($repo, $status)" >&2
    timeout -k 2 "$((deadline - SECONDS > 0 ? deadline - SECONDS : 1))" \
      lakana runs logs "$id" --env "$env" </dev/null >&2 \
      || echo "(could not read the logs of run $id)" >&2
  done < <(python3 -c '
import json, sys
try:
    runs = json.load(sys.stdin)
except ValueError:
    runs = []
for run in runs if isinstance(runs, list) else []:
    if isinstance(run, dict) and run.get("id") and run.get("status") != "succeeded":
        print("\t".join(str(run.get(k, "")).replace("\t", " ") for k in ("id", "status", "repository")))
' <<< "$listing" 2>/dev/null || true)
  if [ "$shown" -eq 0 ]; then
    echo "(no run that did not succeed in the instance)" >&2
  fi
  return 0
}

run_one() {
  local name=$1 log status=0 port words budget=$default_budget own
  if [ -f "$examples_dir/$name/budget" ]; then
    own=$(tr -d '[:space:]' < "$examples_dir/$name/budget")
    if ! [[ "$own" =~ ^[0-9]+$ ]]; then
      echo "docs/examples/$name/budget must hold one number of seconds, not '$own'" >&2
      return 2
    fi
    [ "$own" -le "$budget" ] || budget=$own
  fi
  base=$(mktemp -d)
  log="$base/instance.log"
  mkdir -p "$base/home/config" "$base/home/data" "$base/home/work"
  # A local instance only accepts repositories under HOME, so the work directory lives there.
  export HOME="$base/home" XDG_CONFIG_HOME="$base/home/config" XDG_DATA_HOME="$base/home/data" NO_COLOR=1
  # No git identity: a machine's own would leak in. Unset, so the example proves its own steps.
  unset GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL EMAIL
  export GIT_CONFIG_NOSYSTEM=1
  # Git's own defaults, as a newcomer has them; stable English output.
  export LC_ALL=C
  export EXAMPLE_DIR="$examples_dir/$name" WORK="$base/home/work"
  port=$(free_port)
  # Its own process group, so a hard stop reaches the server and worker too.
  read -ra words <<< "$(start_command "$examples_dir/$name")"
  setsid "${words[@]}" --port "$port" > "$log" 2>&1 &
  pid=$!
  local ready=0
  for _ in $(seq 1 120); do
    if grep -q "Lakana is running" "$log"; then ready=1; break; fi
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
  done
  if [ "$ready" -ne 1 ]; then
    echo "the local instance for example '$name' did not start within 60 s. Its log is below; fix what it names and run again." >&2
    status=1
  else
    echo "== example '$name' (instance on port $port, budget ${budget} s)"
    # In the background, so ^C reaches the trap at once; its own session, so one signal stops all of it.
    BASH_ENV="$tracer" setsid --wait timeout -k 5 "$budget" bash "$EXAMPLE_DIR/run.sh" &
    epid=$!
    wait "$epid" || status=$?
    epid=""
    if [ "$status" -eq 124 ]; then
      echo "example '$name' took longer than its budget of ${budget} s and was stopped; raise LAKANA_DOCS_EXAMPLE_BUDGET, or the file docs/examples/$name/budget, only if the example really needs longer." >&2
    elif [ "$status" -ne 0 ]; then
      echo "example '$name' failed with exit code $status. The line 'run.sh line N: ...' above names the command that failed; open docs/examples/$name/run.sh at that line and fix the command or its check." >&2
    fi
  fi
  if [ "$status" -ne 0 ]; then
    echo "--- the instance's log:" >&2
    cat "$log" >&2 || true
    echo "--- end of the instance's log" >&2
    if [ "$ready" -eq 1 ]; then
      print_failed_run_logs dev || true
    fi
  fi
  cleanup
  return "$status"
}

for name in "${names[@]}"; do
  start=$(date +%s)
  run_one "$name"
  end=$(date +%s)
  printf '| %s | %d s |\n' "$name" "$((end - start))" >> "$summary"
done
{ echo "### Documentation examples"; echo; echo "| Example | Time |"; echo "|---|---|"; cat "$summary"; } \
  | tee -a "${GITHUB_STEP_SUMMARY:-/dev/null}"
