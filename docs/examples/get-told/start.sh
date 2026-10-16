#!/usr/bin/env bash
set -euo pipefail
# How the how-to starts a local instance. The runner (scripts/run_docs_examples.sh) runs the
# command in the region below and appends `--port <free port>`, so that two runs never fight
# over the default port. The worker may send to `localhost`: the example runs a receiver of its
# own there, so the notifications of the how-to really are delivered.

# docs:start start
env LAKANA_WEBHOOK__ALLOW_HOSTS=localhost lakana server start --local
# docs:end start
