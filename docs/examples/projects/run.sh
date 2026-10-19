#!/usr/bin/env bash
set -euo pipefail
# The commands of the how-to "Projects", in order: starting a project, the first example's SQL
# assets, and undeploying. The page shows the regions; the lines between regions are the checks.
# Run by scripts/run_docs_examples.sh, which starts the local instance and sets EXAMPLE_DIR and
# WORK. The Python asset of the page's first example needs a lock and the public package index;
# the example `train-and-predict` runs those steps.
cd "$WORK"

git config --global user.name "Your Name"
git config --global user.email "you@example.com"

# "Start a project": the sample is written because the profile points at the local instance.
mkdir ~/start ~/walk
cd ~/start
# docs:start init
lakana init analytics          # lakana.yaml, a sample (three assets, one check), tests/,
                               # pyproject.toml, .github/workflows/lakana-deploy.yml,
                               # .gitlab-ci.yml
cd analytics && git init && git add -A && git commit -m "Initial project"
# docs:end init
test -f lakana.yaml
test -f pyproject.toml
test -f .github/workflows/lakana-deploy.yml
test -f .gitlab-ci.yml
test -d tests
test -f assets/bronze/readings.sql
test -f assets/silver/station_temperatures.sql
test -f assets/gold/station_ranking.sql
test "$(git rev-list --count HEAD)" -eq 1

# "A first example": a project without the sample, and two SQL assets of its own.
cd ~/walk
# docs:start create
lakana init analytics --no-sample && cd analytics
# docs:end create
test -f lakana.yaml
test ! -e assets
mkdir -p assets/bronze
cp "$EXAMPLE_DIR"/project/assets/bronze/*.sql assets/bronze/
test "$(find assets -name '*.sql' | wc -l)" -eq 2

git init -q
git add -A
git commit -q -m "Two SQL assets"
lakana repo add analytics --kind local --url "$PWD" >/dev/null
lakana deploy --env dev >/dev/null
lakana run --env dev >/dev/null
out=$(lakana --output json sql "SELECT count(*) AS n FROM bronze.readings" --env dev)
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [{"n": 4}], sys.argv[1]' "$out"
out=$(lakana --output json sql "SELECT count(*) AS n FROM bronze.stations" --env dev)
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [{"n": 2}], sys.argv[1]' "$out"
deployed=$(lakana deployments --env dev)
grep -q analytics <<<"$deployed"

# "Deploy, dry-run, roll back" and "Run": the commands of the two blocks, without their optional
# parts, against the deployment above (a second deploy gives rollback a previous one).
git commit -q --allow-empty -m "A second deploy"
lakana deploy --env dev --dry-run >/dev/null
lakana deploy --env dev >/dev/null
lakana rollback --env dev --to-previous >/dev/null
lakana deploys list --env dev --repo analytics --limit 5 >/dev/null
lakana orphans --env dev >/dev/null
lakana run --env dev --select 'bronze.*' --upstream >/dev/null
lakana run --env dev --select 'bronze.readings' --downstream >/dev/null
lakana runs list --env dev --repo analytics --limit 5 >/dev/null

# "Undeploy": the deployment goes, the tables stay.
# docs:start undeploy
lakana undeploy analytics --env dev --yes
# docs:end undeploy
deployed=$(lakana deployments --env dev)
if grep -q analytics <<<"$deployed"; then
  echo "analytics is still deployed after lakana undeploy" >&2
  exit 1
fi
out=$(lakana --output json sql "SELECT count(*) AS n FROM bronze.readings" --env dev)
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [{"n": 4}], sys.argv[1]' "$out"
