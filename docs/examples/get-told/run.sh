#!/usr/bin/env bash
set -euo pipefail
# The commands of the how-to "Get told when a job fails". The page shows the regions; the lines
# between regions are the checks. The page's secret holds a made-up address, so after it this
# script points the secret at a receiver of its own on localhost (the instance's start.sh allows
# that host), and the messages really are delivered. Run by scripts/run_docs_examples.sh, which
# starts the local instance (start.sh) and sets EXAMPLE_DIR and WORK.
cd "$WORK"
export COLUMNS=120
receiver=""

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
trap '[ -z "$capturing" ] || { exec >&3 2>&4; cat "$captured"; }; rm -f "$captured"; [ -z "$receiver" ] || kill "$receiver" 2>/dev/null || true' EXIT

# A receiver that answers 200 to every POST and keeps each body as one line of $WORK/received.
cat > "$WORK/receiver.py" <<'PY'
import http.server
import sys


class Receiver(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        with open(sys.argv[1], "ab") as out:
            out.write(body + b"\n")
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


server = http.server.HTTPServer(("127.0.0.1", 0), Receiver)
print(server.server_address[1], flush=True)
server.serve_forever()
PY
python3 "$WORK/receiver.py" "$WORK/received" > "$WORK/receiver.port" &
receiver=$!
for _ in $(seq 1 50); do
  [ -s "$WORK/receiver.port" ] && break
  sleep 0.1
done
port=$(cat "$WORK/receiver.port")
test -n "$port"

cd ~
lakana init readings --no-sample >/dev/null
cd readings
cp -R "$EXAMPLE_DIR/project/assets/." assets/
git config --global user.name "Your Name"
git config --global user.email "you@example.com"

# docs:start declare
cat "$EXAMPLE_DIR/project/jobs.yaml" >> lakana.yaml
# docs:end declare
git init -q
git add -A
git commit -q -m "Readings and a job that notifies"
lakana repo add readings --kind local --url "$PWD" >/dev/null

# docs:start secret
printf '{"url": "https://hooks.example.com/services/T000/B000/XXXX"}' \
  | lakana secret create team-alerts --env dev --values-stdin
# docs:end secret
out=$(lakana --output json secret list --env dev)
python3 -c 'import json,sys; assert [s["name"] for s in json.loads(sys.argv[1])] == ["team-alerts"]' "$out"
! grep -rF "hooks.example.com" lakana.yaml
# Not shown on the page: the secret now names the receiver above.
printf '{"url": "http://localhost:%s/hook"}' "$port" \
  | lakana secret rotate team-alerts --env dev --values-stdin >/dev/null

lakana deploy --env dev 2>&1 | tee "$WORK/deploy.out"
! grep -Fq "notifies through secret" "$WORK/deploy.out"

capture_start
# docs:start notify-test
lakana job notify-test daily_bronze --env dev
# docs:end notify-test
capture_end
test "$(cat "$captured")" = "delivered"
test "$(wc -l < "$WORK/received")" = 1
python3 - "$WORK/received" <<'PY'
import json
import sys

(line,) = open(sys.argv[1], "rb").read().splitlines()
text = json.loads(line)["text"]
assert "daily_bronze" in text and "Delivery" in text, text
PY

# The job fails (its one asset cannot be built), which queues a `failed` notification.
lakana job run daily_bronze --env dev || true
run_id=$(lakana --output json runs list --env dev --limit 1 | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')
# The worker sends within a few seconds of the run's end.
for _ in $(seq 1 60); do
  lakana runs show "$run_id" --env dev | grep -Eq '^  failed +[0-9a-f-]{36}  delivered$' && break
  sleep 1
done
capture_start
# docs:start runs-show
lakana runs show "$run_id" --env dev
# docs:end runs-show
capture_end
grep -Eq '^Notifications$' "$captured"
grep -Eq '^  failed +[0-9a-f-]{36}  delivered$' "$captured"
test "$(wc -l < "$WORK/received")" = 2
