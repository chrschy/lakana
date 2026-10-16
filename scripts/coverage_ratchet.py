"""Enforce that test coverage never decreases.

Reads total coverage from ``coverage.json``, compares it against the recorded
baseline and an absolute floor, and updates the baseline when coverage improves.

The report is refused if it is older than any source file it claims to cover:
running ``pytest`` without ``--cov`` leaves the previous run's ``coverage.json``
in place, and a ratchet that reads it reports a number for code it never
measured.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import math
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

DEFAULT_FLOOR = 90.0


def _is_percentage(value: float) -> bool:
    """Report whether `value` is a finite number from 0 to 100.

    Parameters
    ----------
    value
        The number to test.

    Returns
    -------
    bool
        False for NaN, infinities and anything outside 0 to 100.
    """
    return math.isfinite(value) and 0.0 <= value <= 100.0


def read_baseline(path: Path) -> float:
    """Read the recorded baseline.

    Parameters
    ----------
    path
        File holding the baseline percentage.

    Returns
    -------
    float
        The baseline, or ``0.0`` when the file does not exist.

    Raises
    ------
    ValueError
        If the file does not hold a finite percentage between 0 and 100.
    OSError
        If the file exists but cannot be read.
    """
    if not path.exists():
        return 0.0
    text = path.read_text().strip()
    value = float(text)
    if not _is_percentage(value):
        raise ValueError(f"{text!r} is not a percentage between 0 and 100")
    return value


def write_baseline(path: Path, value: float) -> None:
    """Record a new baseline.

    Parameters
    ----------
    path
        File to write.
    value
        Percentage to record.
    """
    path.write_text(f"{value:.2f}\n")


def coverage_source_roots(pyproject: Path) -> list[Path]:
    """Read the directories coverage is configured to measure.

    Parameters
    ----------
    pyproject
        Path to ``pyproject.toml``.

    Returns
    -------
    list of Path
        The configured source roots, empty when none are configured.
    """
    if not pyproject.exists():
        return []
    config = tomllib.loads(pyproject.read_text())
    source = config.get("tool", {}).get("coverage", {}).get("run", {}).get("source", [])
    return [Path(entry) for entry in source]


def coverage_omit_patterns(pyproject: Path) -> list[str]:
    """Read the file patterns coverage is configured to leave out.

    Parameters
    ----------
    pyproject
        Path to ``pyproject.toml``.

    Returns
    -------
    list of str
        The ``[tool.coverage.run] omit`` patterns, empty when none are configured.
    """
    if not pyproject.exists():
        return []
    config = tomllib.loads(pyproject.read_text())
    return list(config.get("tool", {}).get("coverage", {}).get("run", {}).get("omit", []))


def _is_omitted(path: Path, omit: Sequence[str]) -> bool:
    """Report whether coverage leaves `path` out of its measurement.

    Parameters
    ----------
    path
        A source file.
    omit
        Coverage ``omit`` patterns.

    Returns
    -------
    bool
        True when any pattern matches the path, relative or absolute.
    """
    names = (path.as_posix(), path.resolve().as_posix())
    return any(fnmatch.fnmatch(name, pattern) for name in names for pattern in omit)


def is_stale(*, report: Path, roots: Sequence[Path], omit: Sequence[str] = ()) -> bool:
    """Report whether the coverage report predates the code it describes.

    Parameters
    ----------
    report
        Path to ``coverage.json``.
    roots
        Directories holding the measured source. Roots that do not exist
        are skipped.
    omit
        Coverage ``omit`` patterns; files matching one are not source.

    Returns
    -------
    bool
        True when any Python file under `roots` is newer than `report`, or when
        the report or a file cannot be inspected: a report that cannot be
        checked is not trusted.
    """
    try:
        report_mtime = report.stat().st_mtime
        return any(
            path.stat().st_mtime > report_mtime
            for root in roots
            if root.exists()
            for path in root.rglob("*.py")
            if not _is_omitted(path, omit)
        )
    except OSError:
        return True


def check(*, current: float, baseline: float, floor: float) -> str | None:
    """Decide whether the current coverage is acceptable.

    Parameters
    ----------
    current
        Coverage percentage from this run.
    baseline
        Highest percentage recorded so far.
    floor
        Absolute minimum, regardless of baseline.

    Returns
    -------
    str or None
        An error message, or ``None`` when coverage is acceptable.
    """
    if current < floor:
        return f"Coverage {current:.2f}% is below the floor of {floor:.2f}%."
    if current < baseline:
        return (
            f"Coverage {current:.2f}% is below the baseline of {baseline:.2f}%. "
            "Coverage may rise but never fall."
        )
    return None


def _report_percentage(text: str) -> float | None:
    """Extract total coverage from the text of a ``coverage.json``.

    Parameters
    ----------
    text
        Contents of the report.

    Returns
    -------
    float or None
        ``totals.percent_covered``, or ``None`` when the text is not JSON, the
        field is missing, is not a number (a string or boolean is refused), or
        is not a finite percentage between 0 and 100.
    """
    try:
        value = json.loads(text)["totals"]["percent_covered"]
    except (ValueError, KeyError, TypeError):
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if _is_percentage(number) else None


def main() -> int:
    """Run the ratchet against ``coverage.json``.

    Returns
    -------
    int
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage-json", type=Path, default=Path("coverage.json"))
    parser.add_argument("--baseline", type=Path, default=Path(".coverage-baseline"))
    parser.add_argument("--floor", type=float, default=DEFAULT_FLOOR)
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument(
        "--update",
        action="store_true",
        help="Write the new baseline when coverage improves.",
    )
    args = parser.parse_args()

    rerun = "Re-run: pytest --cov --cov-report=json"
    if not args.coverage_json.exists():
        print(f"{args.coverage_json} not found. {rerun}", file=sys.stderr)
        return 1

    try:
        roots = coverage_source_roots(args.pyproject)
        omit = coverage_omit_patterns(args.pyproject)
    except (OSError, ValueError) as error:
        print(
            f"cannot read the coverage configuration in {args.pyproject} ({error}). "
            "Fix the file, or pass --pyproject.",
            file=sys.stderr,
        )
        return 1
    if is_stale(report=args.coverage_json, roots=roots, omit=omit):
        print(
            f"{args.coverage_json} is older than the source it covers. {rerun}",
            file=sys.stderr,
        )
        return 1

    try:
        text = args.coverage_json.read_text()
    except OSError as error:
        print(f"cannot read {args.coverage_json} ({error}). {rerun}", file=sys.stderr)
        return 1
    current = _report_percentage(text)
    if current is None:
        print(f"{args.coverage_json} is not a coverage.py JSON report. {rerun}", file=sys.stderr)
        return 1
    try:
        baseline = read_baseline(args.baseline)
    except OSError as error:
        print(
            f"cannot read {args.baseline} ({error}). Check the path and permissions.",
            file=sys.stderr,
        )
        return 1
    except ValueError:
        print(
            f"{args.baseline} does not hold a percentage between 0 and 100. "
            "Restore it from git, or delete it to start the ratchet from zero.",
            file=sys.stderr,
        )
        return 1

    error = check(current=current, baseline=baseline, floor=args.floor)
    if error is not None:
        print(error, file=sys.stderr)
        return 1

    print(f"Coverage {current:.2f}% (baseline {baseline:.2f}%).")
    if args.update and current > baseline:
        write_baseline(args.baseline, current)
        print(f"Baseline raised to {current:.2f}%.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
