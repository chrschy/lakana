# The commands of "Backups" and "Restore" in the how-to "Deploying with Compose". The page
# shows the regions; the lines between regions are the checks and the change that the restore
# must undo.
#
# Not an example the runner starts (it needs Docker and the Compose bundle): the
# `compose-smoke` job (scripts/compose_smoke_test.sh) sources this file from the directory of
# a running bundle (its Compose project name set in .env, as the guide recommends) that has run a project, with the bundle's profile in effect and the table
# bronze.demo holding ids 1 and 2. $RESTORED is a directory that does not exist yet; it plays
# the part of the new directory the page describes.

before=$(lakana --output json runs list --env dev)
rows='[{"id":1,"name":"alpha"},{"id":2,"name":"beta"}]'

# docs:start backup
docker compose stop server worker worker-interactive worker-pipeline
docker compose exec -T postgres pg_dump -U lakana lakana > lakana.sql
docker compose stop seaweedfs seaweedfs-core
docker compose run --rm --no-deps -T -v "$PWD":/backup --entrypoint tar seaweedfs-core czf /backup/seaweed.tgz -C /data .
docker compose up -d --wait
# docs:end backup
# The archive holds the storage's data: SeaweedFS volume files, not an empty or another bundle's volume.
tar tzf seaweed.tgz | grep -q '\.dat$'

# A change after the backup, which the restore has to take back.
printf 'id,name\n3,gamma\n' > change.csv
lakana table append bronze.demo change.csv
out=$(lakana --output json sql "SELECT id, name FROM bronze.demo ORDER BY id")
python3 -c 'import json,sys; assert len(json.loads(sys.argv[1])) == 3, sys.argv[1]' "$out"

# The instance is lost; it comes back in a new directory from the two backups and the copies of
# .env and compose.yaml kept with them.
mkdir "$RESTORED"
cp -a compose.yaml .env lakana.sql seaweed.tgz repos "$RESTORED"
cd "$RESTORED"

# docs:start restore-directory
mkdir -p repos
# docs:end restore-directory
# docs:start restore-clear
docker compose down -v
# docs:end restore-clear
# docs:start restore-catalog
docker compose up -d --wait postgres
docker compose exec -T postgres psql -U lakana -d lakana -v ON_ERROR_STOP=1 --single-transaction < lakana.sql
# docs:end restore-catalog
# docs:start restore-storage
docker compose run --rm --no-deps -T -v "$PWD":/backup --entrypoint tar seaweedfs-core xzf /backup/seaweed.tgz -C /data
# docs:end restore-storage
# docs:start restore-start
docker compose up -d --wait
# docs:end restore-start

# docs:start check
lakana sql "SELECT id, name FROM bronze.demo ORDER BY id"
lakana runs list --env dev
# docs:end check
out=$(lakana --output json sql "SELECT id, name FROM bronze.demo ORDER BY id")
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == json.loads(sys.argv[2]), sys.argv[1]' "$out" "$rows"
after=$(lakana --output json runs list --env dev)
python3 -c 'import json,sys; ids=lambda t: sorted(r["id"] for r in json.loads(t)); assert ids(sys.argv[1]) == ids(sys.argv[2]) != [], sys.argv[2]' "$before" "$after"
