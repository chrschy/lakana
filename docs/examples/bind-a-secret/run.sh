#!/usr/bin/env bash
set -euo pipefail
# The commands of "Bind a secret to its use" in the how-to "Manage secrets". The page shows the
# regions; the lines between regions are the checks. The local instance's administrator may
# manage secrets and package indexes. Run by scripts/run_docs_examples.sh, which starts the
# local instance and sets EXAMPLE_DIR and WORK.
cd "$WORK"

# docs:start create
lakana secret create pypi-corp --env dev --value username=ci --value password=not-a-real-one
lakana index add corp --url https://pkgs.example.org/simple --credential-secret pypi-corp
# docs:end create
out=$(lakana --output json secret list --env dev)
python3 -c 'import json,sys; s={x["name"]:(x["use"],x["use_target"]) for x in json.loads(sys.argv[1])}; assert s["pypi-corp"]==("package_index","corp"), s' "$out"

# docs:start list
lakana secret list --env dev
# docs:end list

lakana secret create hook --env dev --value url=https://hooks.example.org/x
# docs:start bind
lakana secret bind hook --env dev --own-env
# docs:end bind
out=$(lakana --output json secret list --env dev)
python3 -c 'import json,sys; s={x["name"]:x["use"] for x in json.loads(sys.argv[1])}; assert s["hook"]=="environment", s' "$out"
