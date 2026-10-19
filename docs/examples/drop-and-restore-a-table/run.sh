#!/usr/bin/env bash
set -euo pipefail
# The commands of the how-to "Drop and restore a table". The page shows the regions; the lines
# between regions are the checks. Run by scripts/run_docs_examples.sh, which starts the local
# instance and sets EXAMPLE_DIR and WORK.
cd "$WORK"

lakana table create silver.orders --column id:long --column 'status:string?'

uuid() {
  # uuid JSON_TEXT: print the `uuid` of `lakana table get` output.
  python3 -c 'import json,sys; print(json.loads(sys.argv[1])["uuid"])' "$1"
}

before=$(uuid "$(lakana --output json table get silver.orders)")

# docs:start drop
lakana table delete silver.orders --yes
# docs:end drop

if lakana table get silver.orders >/dev/null 2>&1; then
  echo "the dropped table is still there" >&2
  exit 1
fi

# docs:start list
lakana table list silver --dropped
# docs:end list

out=$(lakana --output json table list silver --dropped)
python3 -c '
import json, sys
(entry,) = json.loads(sys.argv[1])
assert entry["name"] == "orders" and entry["restorable"] is True, entry
assert entry["purge_after"] > entry["dropped_at"], entry
' "$out"

# docs:start undrop
lakana table undrop silver.orders
# docs:end undrop

test "$(uuid "$(lakana --output json table get silver.orders)")" = "$before"
test "$(lakana --output json table list silver --dropped)" = "[]"

lakana table delete silver.orders --yes
lakana table create silver.orders --column id:long

# docs:start as
lakana table undrop silver.orders --as orders_before
# docs:end as

test "$(uuid "$(lakana --output json table get silver.orders_before)")" = "$before"

# docs:start purge
lakana table delete silver.orders_before --purge --yes
# docs:end purge

if message=$(lakana table undrop silver.orders_before 2>&1); then
  echo "a purged table was restored" >&2
  exit 1
fi
message=$(printf '%s' "$message" | tr -s '\n ' '  ')
case "$message" in
  *"undo period ended"*"--purge cannot be restored"*) ;;
  *) echo "unexpected refusal: $message" >&2; exit 1 ;;
esac
