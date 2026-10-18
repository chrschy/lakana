#!/usr/bin/env bash
set -euo pipefail
# The commands of the how-to "Describe a table". The page shows the regions; the lines between
# regions are the checks. The local instance's administrator owns what it creates, so the commands
# run as the page shows them. Run by scripts/run_docs_examples.sh, which starts the local instance
# and sets EXAMPLE_DIR and WORK.
cd "$WORK"

lakana table create silver.orders --column id:long --column 'status:string?'

field() {
  # field JSON_TEXT PYTHON_EXPRESSION_OF_t: print one value of `lakana table get` output.
  python3 -c 'import json,sys; t=json.loads(sys.argv[1]); print(eval(sys.argv[2]))' "$1" "$2"
}

out=$(lakana --output json table get silver.orders)
test "$(field "$out" 't["description"]')" = "None"

# docs:start describe
lakana table describe silver.orders --text "One row per customer order, whatever its state."
lakana table describe silver.orders --column status --text "open, shipped or cancelled."
# docs:end describe

# docs:start get
lakana table get silver.orders
# docs:end get

out=$(lakana --output json table get silver.orders)
test "$(field "$out" 't["description"]')" = "One row per customer order, whatever its state."
test "$(field "$out" 't["description_source"]')" = "edited"
test "$(field "$out" '[c["description"] for c in t["columns"] if c["name"] == "status"][0]')" = "open, shipped or cancelled."

# docs:start reset
lakana table describe silver.orders --reset
lakana table describe silver.orders --column status --reset
# docs:end reset

out=$(lakana --output json table get silver.orders)
test "$(field "$out" 't["description"]')" = "None"
test "$(field "$out" '[c["description"] for c in t["columns"] if c["name"] == "status"][0]')" = "None"

# A text over the limit is refused with the next step.
long=$(python3 -c 'print("x" * 4001)')
if message=$(lakana table describe silver.orders --text "$long" 2>&1); then
  echo "a description over the limit was accepted" >&2
  exit 1
fi
message=$(printf '%s' "$message" | tr -s '\n ' '  ')
case "$message" in
  *"4,000"*"Write a shorter one"*) ;;
  *) echo "unexpected refusal: $message" >&2; exit 1 ;;
esac
