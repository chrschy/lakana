# The commands of "Add an environment to a Compose instance" in the how-to "Deploying with
# Compose". The page shows the regions; the lines between regions are the checks.
#
# Not an example the runner starts (it needs the Compose bundle's own storage and .env): the
# `compose-smoke` job (scripts/compose_smoke_test.sh) sources this file from the directory of
# a running bundle, with the bundle's profile in effect, as the bootstrap administrator.

# docs:start create
lakana env create prod --storage-root s3://lakana/prod --option s3.endpoint=http://seaweedfs:8333
# docs:end create

# docs:start secret
s3_key=$(grep '^LAKANA_S3_ACCESS_KEY=' .env | cut -d"'" -f2)
s3_secret=$(grep '^LAKANA_S3_SECRET_KEY=' .env | cut -d"'" -f2)
printf '{"s3.access-key-id": "%s", "s3.secret-access-key": "%s"}' "$s3_key" "$s3_secret" \
  | lakana secret create seaweedfs-s3 --env prod --own-env --values-stdin
unset s3_key s3_secret
# docs:end secret
out=$(lakana --output json secret list --env prod)
python3 -c 'import json,sys; s={x["name"]:(x["use"],x["format_version"]) for x in json.loads(sys.argv[1])}; assert s == {"seaweedfs-s3": ("environment", 2)}, s' "$out"

# docs:start attach
lakana env set-storage prod --option s3.endpoint=http://seaweedfs:8333 --secret-ref seaweedfs-s3
# docs:end attach

# docs:start check
lakana env get prod
lakana table create bronze.check --env prod --column id:long
lakana table delete bronze.check --env prod --yes
# docs:end check
out=$(lakana --output json env get prod)
python3 -c 'import json,sys; e=json.loads(sys.argv[1]); assert (e["storage_root"], e["storage_secret_ref"]) == ("s3://lakana/prod", "seaweedfs-s3"), e' "$out"
