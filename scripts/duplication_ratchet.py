"""Enforce that duplicated code in the packages never grows.

Reads the JSON report of ``jscpd`` (the copy-paste detector pinned in ``tools/jscpd``),
compares the duplicated lines and the number of clones with the checked-in baseline, and
fails when either rises. A fall passes and says to lower the baseline in the same pull
request, so the room it frees is not spent by the next change.

A failure lists the blocks of every pair of files whose duplication grew, as
``file:start-end`` against ``file:start-end``, so the new copy can be found and removed.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

ARROW = "↔"
UPDATE_COMMAND = "uv run python scripts/duplication_ratchet.py --update"


@dataclass(frozen=True)
class Clone:
    """One block of code that appears twice.

    Attributes
    ----------
    first_file, second_file
        Names of the two files, as jscpd reports them.
    first_start, first_end
        Line range in the first file.
    second_start, second_end
        Line range in the second file.
    lines
        Length of the block.
    """

    first_file: str
    first_start: int
    first_end: int
    second_file: str
    second_start: int
    second_end: int
    lines: int

    @property
    def pair(self) -> str:
        """Name the two files, in sorted order.

        Returns
        -------
        str
            ``a ↔ b``, the same whichever file jscpd lists first.
        """
        first, second = sorted((self.first_file, self.second_file))
        return f"{first} {ARROW} {second}"

    def describe(self) -> str:
        """Say where the block is.

        Returns
        -------
        str
            ``file:start-end ↔ file:start-end (N lines)``.
        """
        return (
            f"{self.first_file}:{self.first_start}-{self.first_end} {ARROW} "
            f"{self.second_file}:{self.second_start}-{self.second_end} ({_lines(self.lines)})"
        )


@dataclass(frozen=True)
class Baseline:
    """The duplication recorded, in total and per pair of files.

    Attributes
    ----------
    duplicated_lines
        Lines in all clones.
    clones
        Number of clones.
    pairs
        For each pair of files (see `Clone.pair`), its number of clones and their lines.
    """

    duplicated_lines: int
    clones: int
    pairs: dict[str, tuple[int, int]]

    def summary(self) -> str:
        """Say the totals in words.

        Returns
        -------
        str
            For example ``1433 lines in 74 clones``.
        """
        return f"{_lines(self.duplicated_lines)} in {_clones(self.clones)}"


def _lines(count: int) -> str:
    """Count lines in words.

    Parameters
    ----------
    count
        The number.

    Returns
    -------
    str
        ``1 line`` or ``N lines``.
    """
    return f"{count} line" + ("" if count == 1 else "s")


def _clones(count: int) -> str:
    """Count clones in words.

    Parameters
    ----------
    count
        The number.

    Returns
    -------
    str
        ``1 clone`` or ``N clones``.
    """
    return f"{count} clone" + ("" if count == 1 else "s")


def _is_count(value: object) -> bool:
    """Report whether `value` is a non-negative integer (a boolean is not).

    Parameters
    ----------
    value
        What a JSON file holds.

    Returns
    -------
    bool
        True for an ``int`` of at least 0 that is not a ``bool``.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def parse_report(text: str) -> list[Clone]:
    """Read the clones from the text of jscpd's JSON report.

    Parameters
    ----------
    text
        Contents of ``jscpd-report.json``.

    Returns
    -------
    list of Clone
        One entry per ``duplicates`` entry.

    Raises
    ------
    ValueError
        If the text is not a jscpd report (not JSON, no ``duplicates`` list, or an
        entry missing a file, a line range or a length).
    """
    try:
        entries = json.loads(text)["duplicates"]
        clones = [
            Clone(
                entry["firstFile"]["name"],
                entry["firstFile"]["start"],
                entry["firstFile"]["end"],
                entry["secondFile"]["name"],
                entry["secondFile"]["start"],
                entry["secondFile"]["end"],
                entry["lines"],
            )
            for entry in entries
        ]
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError("this is not a jscpd JSON report") from error
    if not all(_is_count(clone.lines) for clone in clones):
        raise ValueError("this is not a jscpd JSON report: a clone has no line count")
    return clones


def measure(clones: list[Clone]) -> Baseline:
    """Total the duplication.

    Parameters
    ----------
    clones
        The clones of one run.

    Returns
    -------
    Baseline
        Lines and clones in total and for each pair of files.
    """
    pairs: dict[str, tuple[int, int]] = {}
    for clone in clones:
        count, lines = pairs.get(clone.pair, (0, 0))
        pairs[clone.pair] = (count + 1, lines + clone.lines)
    return Baseline(sum(clone.lines for clone in clones), len(clones), pairs)


def write_baseline(path: Path, baseline: Baseline) -> None:
    """Record a baseline.

    Parameters
    ----------
    path
        File to write.
    baseline
        What to record.
    """
    data = {
        "duplicated_lines": baseline.duplicated_lines,
        "clones": baseline.clones,
        "pairs": {
            pair: {"clones": count, "lines": lines}
            for pair, (count, lines) in sorted(baseline.pairs.items())
        },
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_baseline(path: Path) -> Baseline:
    """Read the recorded baseline.

    Parameters
    ----------
    path
        File written by `write_baseline`.

    Returns
    -------
    Baseline
        The recorded numbers.

    Raises
    ------
    FileNotFoundError
        If the file does not exist; the message says how to create it.
    ValueError
        If the file does not hold a baseline.
    OSError
        If the file exists but cannot be read.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise FileNotFoundError(
            f"{path} not found. Record today's duplication with: {UPDATE_COMMAND}"
        ) from error
    try:
        data = json.loads(text)
        pairs = {pair: (entry["clones"], entry["lines"]) for pair, entry in data["pairs"].items()}
        numbers = [
            data["duplicated_lines"],
            data["clones"],
            *(n for p in pairs.values() for n in p),
        ]
        if not all(_is_count(number) for number in numbers):
            raise ValueError("a number is not a count")
        return Baseline(data["duplicated_lines"], data["clones"], pairs)
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise ValueError(f"{path} does not hold a duplication baseline") from error


def check(clones: list[Clone], baseline: Baseline) -> str | None:
    """Decide whether the duplication is acceptable.

    Parameters
    ----------
    clones
        The clones of this run.
    baseline
        The recorded duplication.

    Returns
    -------
    str or None
        An error message listing the blocks of every pair of files that gained duplication,
        or ``None`` when neither the duplicated lines nor the clones rose.
    """
    current = measure(clones)
    if current.duplicated_lines <= baseline.duplicated_lines and current.clones <= baseline.clones:
        return None
    grown = {
        pair
        for pair, (count, lines) in current.pairs.items()
        if count > baseline.pairs.get(pair, (0, 0))[0]
        or lines > baseline.pairs.get(pair, (0, 0))[1]
    }
    listing = "\n".join(f"  {clone.describe()}" for clone in clones if clone.pair in grown)
    return (
        f"Duplication rose to {current.summary()} (baseline {baseline.summary()}). "
        "It may fall but never rise.\n"
        f"Blocks in the files that gained duplication:\n{listing}\n"
        "Extract the shared code into one function. For a copy that is justified, wrap it in "
        "`# jscpd:ignore-start` and `# jscpd:ignore-end` comments and give the reason in a "
        "comment beside them."
    )


def main() -> int:
    """Run the ratchet against jscpd's report.

    Returns
    -------
    int
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report", type=Path, default=Path("jscpd-report/jscpd-report.json"), help="jscpd's report"
    )
    parser.add_argument("--baseline", type=Path, default=Path("scripts/duplication-baseline.json"))
    parser.add_argument(
        "--update",
        action="store_true",
        help="Record the baseline when duplication fell, or create it when missing.",
    )
    args = parser.parse_args()

    rerun = (
        "Run jscpd with --reporters json --output jscpd-report, as the `lint` job of "
        ".github/workflows/ci.yml does."
    )
    try:
        clones = parse_report(args.report.read_text(encoding="utf-8"))
    except OSError as error:
        print(f"cannot read {args.report} ({error}). {rerun}", file=sys.stderr)
        return 1
    except ValueError as error:
        print(f"{args.report}: {error}. {rerun}", file=sys.stderr)
        return 1
    current = measure(clones)

    try:
        baseline = read_baseline(args.baseline)
    except FileNotFoundError as error:
        if args.update:
            write_baseline(args.baseline, current)
            print(f"Baseline created: {current.summary()}.")
            return 0
        print(error, file=sys.stderr)
        return 1
    except OSError as error:
        print(
            f"cannot read {args.baseline} ({error}). Check the path and permissions.",
            file=sys.stderr,
        )
        return 1
    except ValueError as error:
        print(
            f"{error}. Restore it from git, or delete it and run {UPDATE_COMMAND}.", file=sys.stderr
        )
        return 1

    message = check(clones, baseline)
    if message is not None:
        print(message, file=sys.stderr)
        return 1

    print(f"Duplication {current.summary()} (baseline {baseline.summary()}).")
    if current.duplicated_lines != baseline.duplicated_lines or current.clones != baseline.clones:
        if args.update:
            write_baseline(args.baseline, current)
            print(f"Baseline lowered to {current.summary()}.")
        else:
            print(
                f"Duplication fell. Lower the baseline to {current.summary()} in the same pull "
                f"request: {UPDATE_COMMAND}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
