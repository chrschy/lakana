#!/usr/bin/env bash
set -euo pipefail
# The commands of the how-to "Let tables expire". The page shows the regions; the lines between
# regions are the checks. Run by scripts/run_docs_examples.sh, which starts the local instance and
# sets EXAMPLE_DIR and WORK. The expiry itself needs the worker's sweep after the days have
# passed; the catalog's tests cover it (lakana-core/tests/test_table_retention.py).
cd "$WORK"

field() {
  # field JSON_TEXT NAME: print one field of a JSON object.
  python3 -c 'import json,sys; print(json.loads(sys.argv[1])[sys.argv[2]])' "$1" "$2"
}

# docs:start create
lakana table create bronze.scratch --column id:long --expire-after 14
# docs:end create

test "$(field "$(lakana --output json table retention bronze.scratch)" days)" = "14"

# docs:start show
lakana table retention bronze.scratch
# docs:end show

shown=$(lakana table retention bronze.scratch | tr -s '\n ' '  ')
case "$shown" in
  *"expires in 13 days"*"14 days after its last activity"*) ;;
  *) echo "unexpected retention line: $shown" >&2; exit 1 ;;
esac

# docs:start sql
lakana sql "SELECT 1 AS id" --into bronze.trial --mode create --expire-after 7
# docs:end sql

test "$(field "$(lakana --output json table retention bronze.trial)" days)" = "7"

# docs:start days
lakana table retention bronze.trial --days 3
# docs:end days

# docs:start expiring
lakana table expiring --within 7d
# docs:end expiring

out=$(lakana --output json table expiring --within 7d)
python3 -c '
import json, sys
names = [entry["name"] for entry in json.loads(sys.argv[1])]
assert names == ["trial"], names
' "$out"

# docs:start never
lakana table retention bronze.trial --never
# docs:end never

test "$(field "$(lakana --output json table retention bronze.trial)" never)" = "True"

# docs:start env
lakana env retention dev --days 30 --yes
# docs:end env

test "$(field "$(lakana --output json env get dev)" retention_days)" = "30"

# docs:start env-off
lakana env retention dev --off
# docs:end env-off

test "$(field "$(lakana --output json env get dev)" retention_days)" = "None"
