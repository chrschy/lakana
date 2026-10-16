#!/usr/bin/env bash
# Build the offline wheel index the Python-asset integration tests install from:
# the lakana wheel of this working tree plus the locked wheels of lakana[assets],
# for the interpreter `uv run` uses. This is the only step of those tests that
# needs the network, and only for wheels the directory does not hold yet. The directory is never committed to this repository: it
# lives in a temporary directory or a CI cache, and the tests copy it into the
# throwaway repositories they deploy.
#
#   bash scripts/build_wheelhouse.sh DIR
#   LAKANA_TEST_WHEELHOUSE=DIR uv run pytest -m integration
set -euo pipefail
out=${1:?usage: build_wheelhouse.sh DIR}
root=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$out"
out=$(cd "$out" && pwd)
cd "$root"
# A wheel of an earlier commit would make `lakana[assets]==<version>` ambiguous.
rm -f "$out"/lakana-*.whl
uv build --package lakana --wheel --out-dir "$out"
# uv marks its output directory as ignored (`*`); a test repository must be able
# to commit the copy it gets, or the deployed code would have no wheels.
rm -f "$out/.gitignore"
# The export keeps the lock's hashes and pip checks every wheel against them: these
# wheels are executed by the tests, so they must be the ones the lock names.
uv export --frozen --no-dev --package lakana --extra assets --no-emit-workspace \
  -o "$out/requirements.txt" >/dev/null
# LAKANA_WHEELHOUSE_PYTHON (e.g. 3.13) picks the interpreter the wheels are for, when it is
# not the one `uv run` uses: the Compose smoke test needs the image's. If that interpreter is
# not installed, uv downloads it (a network step). The image's Python can be newer than the
# one in .python-version, which setup-uv installs in CI.
download() {
  uv run --no-project ${LAKANA_WHEELHOUSE_PYTHON:+--python "$LAKANA_WHEELHOUSE_PYTHON"} \
    --with pip python -m pip download --quiet --disable-pip-version-check \
    --require-hashes --only-binary=:all: --dest "$out" -r "$out/requirements.txt" "$@"
}
# A directory restored from a cache already holds every wheel: then no index is asked. When
# that pass fails (an empty or partial directory), say why before asking the index.
if ! offline=$(download --no-index --find-links "$out" 2>&1); then
  printf 'wheelhouse: the offline pass failed, asking the index:\n%s\n' "$offline" >&2
  download
fi
rm "$out/requirements.txt"
ls "$out"/lakana-*.whl >/dev/null
echo "wheelhouse: $(find "$out" -name '*.whl' | wc -l) wheels in $out"
