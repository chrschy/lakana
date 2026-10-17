#!/usr/bin/env bash
set -euo pipefail
# The commands of the how-to "Give a second person access". The page shows the regions; the lines
# between regions are the checks. The local instance's administrator may manage principals,
# grants and tokens, so the commands run as the page shows them. The commands of the new user
# run with a configuration folder of their own, as they would on their own machine. Run by
# scripts/run_docs_examples.sh, which starts the local instance and sets EXAMPLE_DIR and WORK.
cd "$WORK"

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

# A table for the new person to query; the page does not show it.
lakana table create silver.visits --column id:long --column region:string >/dev/null

capture_start
# docs:start create
lakana principal create alice --type user
# docs:end create
capture_end
grep -Fxq 'Created user alice' "$captured"

capture_start
# docs:start grant
lakana grant create --principal alice --role ANALYST --kind ENVIRONMENT --env dev
# docs:end grant
capture_end
grep -Eq '^Created grant [0-9a-f-]{36} \(ANALYST on ENVIRONMENT\)$' "$captured"

capture_start
# docs:start token
lakana token issue alice alice-laptop
# docs:end token
capture_end
grep -Eq '^Issued token [A-Za-z0-9]+ \(alice-laptop\) for alice$' "$captured"
TOKEN=$(sed -n 's/^Token: //p' "$captured")
case "$TOKEN" in lakana_pat_*) ;; *) echo "no token in the output" >&2; exit 1 ;; esac
KEY_ID=$(sed -n 's/^Issued token \([A-Za-z0-9]*\) .*/\1/p' "$captured")

# What the new person types, on a machine of their own: a configuration folder that has no
# profile yet. The address of the instance is the one the administrator's profile holds.
URL=$(lakana --output json profile list | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["url"])')
admin_config=$XDG_CONFIG_HOME
export XDG_CONFIG_HOME="$WORK/alice-config"
mkdir -p "$XDG_CONFIG_HOME"

capture_start
# docs:start login
printf '%s\n' "$TOKEN" | lakana login --url "$URL" --env dev --token-stdin
# docs:end login
capture_end
grep -Fq 'Logged in as alice (USER) at' "$captured"
grep -Fq "saved profile 'default'" "$captured"

capture_start
# docs:start first
lakana whoami
lakana sql "SELECT count(*) AS visits FROM silver.visits"
# docs:end first
capture_end
grep -Eq '^│ name +│ alice +│' "$captured" || grep -Fq 'alice' "$captured"
grep -Fq 'visits' "$captured"

# The smallest set is enough to read and not enough to change: alice may not create a table.
status=0
lakana table create silver.alices_table --column id:long >/dev/null 2>&1 || status=$?
test "$status" -ne 0
export XDG_CONFIG_HOME=$admin_config

capture_start
# docs:start who
lakana principal list
lakana --output json grant list --principal alice
lakana token list alice
# docs:end who
capture_end
grep -Fq 'alice' "$captured"

GRANT_ID=$(lakana --output json grant list --principal alice | python3 -c 'import json,sys; g=json.load(sys.stdin); assert len(g) == 1, g; print(g[0]["id"])')

as_alice() { XDG_CONFIG_HOME="$WORK/alice-config" "$@"; }
refused() { local status=0; "$@" >/dev/null 2>&1 || status=$?; test "$status" -ne 0; }

capture_start
# docs:start revoke-token
lakana token revoke alice "$KEY_ID" --yes
# docs:end revoke-token
capture_end
grep -Fxq "Revoked token $KEY_ID" "$captured"
refused as_alice lakana whoami

# A new token for the same person, to see what each later step takes away.
lakana --output json token issue alice alice-desktop > "$WORK/issued.json"
TOKEN=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["token"])' "$WORK/issued.json")
printf '%s\n' "$TOKEN" | as_alice lakana login --url "$URL" --env dev --token-stdin >/dev/null
as_alice lakana whoami >/dev/null

capture_start
# docs:start revoke-grant
lakana grant revoke "$GRANT_ID" --yes
# docs:end revoke-grant
capture_end
grep -Eq '^Revoked grant [0-9a-f-]{36}$' "$captured"
as_alice lakana whoami >/dev/null
refused as_alice lakana sql "SELECT count(*) AS visits FROM silver.visits"

capture_start
# docs:start deactivate
lakana principal deactivate alice --yes
# docs:end deactivate
capture_end
grep -Fxq 'Deactivated principal alice' "$captured"
refused as_alice lakana whoami
