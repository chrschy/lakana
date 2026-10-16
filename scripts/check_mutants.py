"""Fail the build when a mutation-testing survivor is not recorded as equivalent.

The M3 epic's exit criterion is mutation testing on `authz/` and `policy/`: every
mutant `mutmut` reports as anything other than killed or skipped must either be
killed by a strengthened test or recorded, with a reason, as equivalent in
`docs/security/mutation-testing.md`. This script is the gate that enforces
that — it reads `mutmut results --all true` output and refuses a run with an
unrecorded survivor, or with no mutants at all for one of the three mutation
targets (a missing `.meta` file, or an `only_mutate` glob that stopped
matching after a rename, would otherwise report nothing and pass vacuously),
rather than letting either slip through silently.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_RESULT_LINE = re.compile(r"^\s*(\S+): (.+)$")
_RECORDED_KEY = re.compile(r"^\|\s*`([^`]+)`\s*\|", re.MULTILINE)
_ACCOUNTED_FOR_STATUSES = frozenset({"killed", "skipped"})
"""Statuses that need no entry in the equivalence record: mutmut already
accounts for them on its own, by killing the mutant or explicitly skipping
it. Every other status (`survived`, `timeout`, `suspicious`, `no tests`, ...)
needs either a killing test or a recorded equivalence."""

REQUIRED_TARGETS: tuple[str, ...] = (
    "lakana_core.grants.authorization",
    "lakana_core.policy",
    "lakana_core.audit.authorization",
    "lakana_core.catalog.external_location",
)
"""Module prefixes every mutation-testing run must cover. `missing_targets`
fails closed when `mutmut results` reports zero mutants for one of these —
nothing to report about a target is not the same as nothing wrong with it."""


def check(results: str, record: Path) -> list[str]:
    """Find every mutant that is neither accounted for nor recorded as equivalent.

    Parameters
    ----------
    results
        The stdout of `mutmut results` (or `mutmut results --all true`), one
        `<key>: <status>` line per mutant (indentation and any surrounding
        lines are ignored).
    record
        Path to `docs/security/mutation-testing.md`, whose table rows
        (``| `<key>` | ... | ... |``) list the mutants recorded as
        equivalent.

    Returns
    -------
    list of str
        The sorted keys of every mutant whose status is not `killed` or
        `skipped` and whose key is not recorded in `record`. Empty when
        every non-killed mutant is accounted for.
    """
    recorded = set(_RECORDED_KEY.findall(record.read_text(encoding="utf-8")))
    unrecorded = set()
    for line in results.splitlines():
        match = _RESULT_LINE.match(line)
        if match is None:
            continue
        key, status = match.group(1), match.group(2)
        if status in _ACCOUNTED_FOR_STATUSES:
            continue
        if key not in recorded:
            unrecorded.add(key)
    return sorted(unrecorded)


def missing_targets(results: str) -> list[str]:
    """Find required mutation targets with no mutant keys at all in `results`.

    Parameters
    ----------
    results
        The stdout of `mutmut results --all true` — one `<key>: <status>`
        line per mutant, covering every status, including `killed`. Plain
        `mutmut results` (survivors only) would make a target that mutmut
        ran and fully killed indistinguishable from one it never ran any
        mutants against at all, so this check needs the `--all true` form.

    Returns
    -------
    list of str
        The sorted `REQUIRED_TARGETS` entries for which no mutant key equals
        that target or starts with it at a `.` module boundary. Empty when
        every required target has at least one mutant — including when
        `results` is itself empty, which is reported as every target
        missing rather than silently passing.
    """
    keys = [match.group(1) for line in results.splitlines() if (match := _RESULT_LINE.match(line))]
    return sorted(
        target
        for target in REQUIRED_TARGETS
        if not any(key == target or key.startswith(target + ".") for key in keys)
    )


def main() -> int:
    """Read `mutmut results --all true` from stdin and check it against the record file.

    Returns
    -------
    int
        `1` if any survivor is unrecorded, or a required target has no
        mutants at all (each is printed under its own header), else `0`.
    """
    record = Path(sys.argv[1])
    results = sys.stdin.buffer.read().decode("utf-8")
    unrecorded = check(results, record)
    absent = missing_targets(results)
    if not unrecorded and not absent:
        return 0
    if unrecorded:
        print("Unrecorded surviving mutants:")
        for key in unrecorded:
            print(key)
    if absent:
        print("Targets with no mutants at all (failing closed):")
        for target in absent:
            print(target)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
