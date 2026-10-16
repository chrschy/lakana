#!/usr/bin/env bash
# Turn a `lakana init` project into one with a Python asset, installed from an offline wheel
# directory, for the smoke tests. Writes assets/silver/doubled.py (reads bronze.example,
# which `lakana init` creates), copies the wheel directory into the project as `wheelhouse/`,
# points uv at it alone (`no-index`), and locks with `uv lock --offline`: neither the lock nor
# the worker's venv build asks a package index, and the wheels travel with the committed code.
#
#   bash scripts/python_asset_project.sh PROJECT WHEELHOUSE PYTHON [artifact]   # PYTHON: e.g. 3.13
#
# WHEELHOUSE holds the lakana wheel and its locked dependencies (scripts/build_wheelhouse.sh
# builds one; the five-minute test downloads one). PYTHON is the interpreter the worker runs.
# The project is locked for this machine's platform only, which is where the worker runs.
# With a fourth argument `artifact` the project also gets models.doubler, an asset that
# stores a model as an artifact, and gold.by_model, which reads it and writes a table.
set -euo pipefail
usage="usage: python_asset_project.sh PROJECT WHEELHOUSE PYTHON [artifact]"
project=${1:?$usage}
wheelhouse=${2:?$usage}
python=${3:?$usage}
if [ $# -gt 4 ] || { [ $# -eq 4 ] && [ "$4" != "artifact" ]; }; then
  if [ "${4:-}" != "artifact" ]; then extra=${4:-}; else extra=$5; fi
  echo "unknown argument '$extra'; the only optional fourth argument is 'artifact'" >&2
  echo "$usage" >&2
  exit 2
fi
mkdir -p "$project/assets/silver" "$project/wheelhouse"
cat > "$project/assets/silver/doubled.py" <<'PY'
import polars as pl
from lakana import asset


@asset(inputs={"example": "bronze.example"})
def doubled(example: pl.DataFrame) -> pl.DataFrame:
    """Double the example id."""
    return example.select(pl.col("id"), (pl.col("id") * 2).alias("doubled"))
PY
wheels=("$wheelhouse"/lakana-[0-9]*.whl)
[ -e "${wheels[0]}" ] && [ "${#wheels[@]}" -eq 1 ] || {
  echo "$wheelhouse must hold exactly one lakana wheel, found: ${wheels[*]}" >&2; exit 1; }
version=$(basename "${wheels[0]}" | cut -d- -f2)
# `uv build` marks its output directory as ignored; the project's repository must keep the wheels.
find "$wheelhouse" -maxdepth 1 -name '*.whl' -exec cp {} "$project/wheelhouse/" \;
marker=$(python3 -c 'import platform, sys; print(f"sys_platform == {sys.platform!r} and platform_machine == {platform.machine()!r}")')
cat > "$project/pyproject.toml" <<TOML
[project]
name = "demo"
version = "0.1.0"
requires-python = "==$python.*"
dependencies = ["lakana[assets]==$version"]

[tool.uv]
no-index = true
find-links = ["wheelhouse"]
environments = ["$marker"]
TOML
if [ $# -eq 4 ]; then
  mkdir -p "$project/assets/models" "$project/assets/gold"
  cat > "$project/assets/models/doubler.py" <<'PY'
import json
from pathlib import Path

import polars as pl
import lakana
from lakana import asset


@asset(inputs={"example": "bronze.example"}, output="artifact", keep_versions=2)
def doubler(example: pl.DataFrame) -> Path:
    """A model of one number, stored as an artifact."""
    out = lakana.output_dir() / "model.json"
    out.write_text(json.dumps({"factor": 2, "rows": example.height}))
    return out
PY
  cat > "$project/assets/gold/by_model.py" <<'PY'
import json
from pathlib import Path

import polars as pl
from lakana import asset


@asset(inputs={"model": "models.doubler", "example": "bronze.example"})
def by_model(model: Path, example: pl.DataFrame) -> pl.DataFrame:
    """Apply the stored model."""
    factor = json.loads(model.read_text())["factor"]
    return example.select(pl.col("id"), (pl.col("id") * factor).alias("doubled"))
PY
fi
# The unreachable proxy makes a stray network request fail instead of succeeding quietly.
(cd "$project" && https_proxy=http://127.0.0.1:9 UV_NO_PROGRESS=1 uv lock --offline --quiet --python "$python")
