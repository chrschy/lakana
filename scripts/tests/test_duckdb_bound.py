"""The worker must not float onto a DuckDB major version Lakana has not been tested on."""

import re
import tomllib
from pathlib import Path

WORKER_PYPROJECT = Path(__file__).resolve().parents[2] / "packages/lakana-worker/pyproject.toml"


def _version(text: str) -> tuple[int, ...]:
    """Parse a dotted version, padded to three parts.

    Parameters
    ----------
    text
        A version such as ``3`` or ``3.0.0``.

    Returns
    -------
    tuple of int
        Its numeric parts.
    """
    parts = tuple(int(part) for part in text.split("."))
    return parts + (0,) * (3 - len(parts))


def test_worker_requires_duckdb_below_the_next_major() -> None:
    """The DuckDB requirement has an upper bound that excludes 3.0.

    Lakana moved to 2.0 in #96 after sweeping the guard and the query process's settings; the
    next major needs the same before the bound moves.
    """
    data = tomllib.loads(WORKER_PYPROJECT.read_text())
    deps = data["tool"]["hatch"]["metadata"]["hooks"]["custom"]["dependencies"]
    (requirement,) = (d for d in deps if re.match(r"duckdb\b", d))
    bounds = re.findall(r"(<=?)\s*(\d+(?:\.\d+)*)", requirement)
    excluded = any(
        _version(number) <= (3, 0, 0) if operator == "<" else _version(number) < (3, 0, 0)
        for operator, number in bounds
    )
    assert excluded, (
        "lakana-worker must require duckdb<3 until Lakana is tested on DuckDB 3 (as #96 did "
        "for 2.0); "
        f"found {requirement!r}"
    )
