#!/usr/bin/env bash
set -euo pipefail
# The commands of the how-to "Show some people only some rows". The page shows the regions; the
# lines between regions are the checks. The local instance's administrator may manage policies
# and read every table, so the commands run as the page shows them. Run by
# scripts/run_docs_examples.sh, which starts the local instance and sets EXAMPLE_DIR and WORK.
cd "$WORK"

lakana principal create alice --type service-account
lakana group create sales_us

# docs:start table
lakana table create gold.customers --column id:long --column region:string --column tier:string
# docs:end table

# docs:start create
lakana row-filter create gold.customers --principal alice --expression "region = 'EU'"
lakana row-filter create gold.customers --group sales_us --expression "region = 'US' AND NOT (tier = 'vip')"
# docs:end create

# A column the table does not have, and a qualified column, are refused at once.
for refused in "regoin = 'EU'" "customers.region = 'EU'"; do
  if lakana row-filter create gold.customers --principal alice --expression "$refused"; then
    echo "a filter that should have been refused was created: $refused" >&2
    exit 1
  fi
done

# A text column compared with a number is refused with the fix, as the page shows.
if message=$(lakana row-filter create gold.customers --principal alice --expression "region = 5" 2>&1); then
  echo "a filter that compares text with a number was created" >&2
  exit 1
fi
expected="'region = 5' is not a valid filter expression: \`region\` is text; compare it with '5', not 5."
case "$message" in
  *"$expected"*) ;;
  *) echo "unexpected refusal: $message" >&2; exit 1 ;;
esac

# docs:start list
lakana row-filter list --principal alice
# docs:end list

out=$(lakana --output json row-filter list --principal alice)
test "$(python3 -c 'import json,sys; print(len(json.loads(sys.argv[1])))' "$out")" -eq 1
ROW_FILTER_ID=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])[0]["id"])' "$out")

# docs:start revoke
lakana row-filter revoke "$ROW_FILTER_ID" --yes
# docs:end revoke

out=$(lakana row-filter list --principal alice)
test "$out" = "(none)"
