"""Tests for the mutation-testing survivor check."""

import io
import sys
from pathlib import Path

import pytest

from scripts.check_mutants import REQUIRED_TARGETS, check, main, missing_targets


def test_passes_when_every_survivor_is_recorded(tmp_path: Path) -> None:
    """Recorded equivalent mutants do not fail the check."""
    record = tmp_path / "record.md"
    record.write_text("| `m.x__mutmut_1` | ... | ... |\n")
    results = "    m.x__mutmut_1: survived\n    m.x__mutmut_2: killed\n"
    assert check(results, record) == []


def test_reports_an_unrecorded_survivor(tmp_path: Path) -> None:
    """A survivor absent from the record is returned by name."""
    record = tmp_path / "record.md"
    record.write_text("| `m.x__mutmut_1` | ... | ... |\n")
    results = "    m.x__mutmut_1: survived\n    m.x__mutmut_3: survived\n"
    assert check(results, record) == ["m.x__mutmut_3"]


def test_lines_that_are_not_results_are_ignored(tmp_path: Path) -> None:
    """Banners, blank lines and progress output around the results never count as mutants."""
    record = tmp_path / "record.md"
    record.write_text("")
    results = "Running mutmut\n\n    m.x__mutmut_1: killed\nnot a result line\n"
    assert check(results, record) == []


def test_timeouts_and_suspicious_count_as_unrecorded_survivors(tmp_path: Path) -> None:
    """A mutant that is neither killed nor recorded is never silently passed."""
    record = tmp_path / "record.md"
    record.write_text("")
    results = "    m.a: timeout\n    m.b: suspicious\n    m.c: no tests\n    m.d: killed\n"
    assert check(results, record) == ["m.a", "m.b", "m.c"]


def test_a_record_key_with_non_ascii_characters_is_read_correctly(tmp_path: Path) -> None:
    """The record file is read as UTF-8: mutmut keys contain the U+01C1 'ǁ' separator."""
    record = tmp_path / "record.md"
    record.write_text("| `lakana_core.xǁFooǁbar__mutmut_1` | ... | ... |\n", encoding="utf-8")
    results = "    lakana_core.xǁFooǁbar__mutmut_1: survived\n"
    assert check(results, record) == []


def test_missing_targets_reports_every_target_when_results_is_empty() -> None:
    """Empty input (e.g. a crashed mutmut command) fails closed, not silently.

    `check()` alone would return `[]` for empty input — there's nothing to
    report as unrecorded. `missing_targets()` is what turns "nothing to
    report" into a failure when it should have found mutants and didn't.
    """
    assert missing_targets("") == sorted(REQUIRED_TARGETS)


def test_missing_targets_reports_only_the_absent_target() -> None:
    """A target with zero mutant keys is reported even though the others are fine."""
    results = (
        "    lakana_core.grants.authorization.x__mutmut_1: killed\n"
        "    lakana_core.audit.authorization.x__mutmut_1: killed\n"
        "    lakana_core.catalog.external_location.x__mutmut_1: killed\n"
    )
    assert missing_targets(results) == ["lakana_core.policy"]


def test_missing_targets_passes_when_every_target_has_a_mutant() -> None:
    """Every required target having at least one mutant of any status is enough."""
    results = (
        "    lakana_core.grants.authorization.x__mutmut_1: killed\n"
        "    lakana_core.policy.row_filters.x__mutmut_1: survived\n"
        "    lakana_core.audit.authorization.x__mutmut_1: killed\n"
        "    lakana_core.catalog.external_location.x__mutmut_1: killed\n"
    )
    assert missing_targets(results) == []


def test_missing_targets_reports_external_location_when_its_key_is_absent() -> None:
    """A results listing without an `external_location` key fails `missing_targets`.

    `external_location.py` is a new mutation target added after the
    id-binding security fixes; a results listing that covers the three
    pre-existing targets but omits it must still be reported as missing.
    """
    results = (
        "    lakana_core.grants.authorization.x__mutmut_1: killed\n"
        "    lakana_core.policy.row_filters.x__mutmut_1: killed\n"
        "    lakana_core.audit.authorization.x__mutmut_1: killed\n"
    )
    assert missing_targets(results) == ["lakana_core.catalog.external_location"]


def test_missing_targets_requires_a_module_boundary_after_the_prefix() -> None:
    """A key from an unrelated module that merely starts with a target's text doesn't count.

    `lakana_core.policyfoo.x__mutmut_1` is a different module from
    `lakana_core.policy` — plain `str.startswith` would wrongly treat it as
    coverage for the `lakana_core.policy` target, so `lakana_core.policy` must
    still be reported missing even though a key with that exact text as a
    prefix is present. `lakana_core.grants.authorization` and
    `lakana_core.audit.authorization` are given real, correctly-scoped keys
    (a `.x_...` form and a `ǁ`-separated class-method form) so only the
    `lakana_core.policy` target is exercised by the boundary bug.
    """
    results = (
        "    lakana_core.policyfoo.x__mutmut_1: killed\n"
        "    lakana_core.grants.authorization.x__mutmut_1: killed\n"
        "    lakana_core.audit.authorization.xǁAuditingAuthorizationǁauthorize__mutmut_1: killed\n"
        "    lakana_core.catalog.external_location.x__mutmut_1: killed\n"
    )
    assert missing_targets(results) == ["lakana_core.policy"]


def _stdin_with(text: str) -> io.TextIOWrapper:
    """Build a stdin replacement whose `.buffer` yields UTF-8 bytes, like the real one.

    Parameters
    ----------
    text
        The text the fake stdin should yield when read.

    Returns
    -------
    io.TextIOWrapper
        A text stream backed by an in-memory UTF-8-encoded buffer, so
        `sys.stdin.buffer.read().decode("utf-8")` works against it exactly
        as it would against the real `sys.stdin`.
    """
    return io.TextIOWrapper(io.BytesIO(text.encode("utf-8")), encoding="utf-8")


def test_main_exits_1_when_a_survivor_is_unrecorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every target present, but one survivor unrecorded, still fails — and names it."""
    record = tmp_path / "record.md"
    record.write_text("")
    results = (
        "    lakana_core.grants.authorization.x__mutmut_1: killed\n"
        "    lakana_core.policy.row_filters.x__mutmut_1: survived\n"
        "    lakana_core.audit.authorization.x__mutmut_1: killed\n"
    )
    monkeypatch.setattr(sys, "argv", ["check_mutants.py", str(record)])
    monkeypatch.setattr(sys, "stdin", _stdin_with(results))

    assert main() == 1
    assert "lakana_core.policy.row_filters.x__mutmut_1" in capsys.readouterr().out


def test_main_exits_1_when_the_input_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crashed `mutmut results` (empty stdout) fails closed instead of passing vacuously."""
    record = tmp_path / "record.md"
    record.write_text("")
    monkeypatch.setattr(sys, "argv", ["check_mutants.py", str(record)])
    monkeypatch.setattr(sys, "stdin", _stdin_with(""))

    assert main() == 1


def test_main_exits_0_when_every_target_has_mutants_and_none_are_unrecorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean run — every target represented, every survivor accounted for — passes."""
    record = tmp_path / "record.md"
    record.write_text("")
    results = (
        "    lakana_core.grants.authorization.x__mutmut_1: killed\n"
        "    lakana_core.policy.row_filters.x__mutmut_1: killed\n"
        "    lakana_core.audit.authorization.x__mutmut_1: killed\n"
        "    lakana_core.catalog.external_location.x__mutmut_1: killed\n"
    )
    monkeypatch.setattr(sys, "argv", ["check_mutants.py", str(record)])
    monkeypatch.setattr(sys, "stdin", _stdin_with(results))

    assert main() == 0


def test_main_reports_only_unrecorded_survivors_when_every_target_is_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unrecorded survivor alone fails, without the missing-target header."""
    record = tmp_path / "record.md"
    record.write_text("")
    results = "".join(f"    {target}.x__mutmut_1: killed\n" for target in REQUIRED_TARGETS)
    results += "    lakana_core.policy.x__mutmut_2: survived\n"
    monkeypatch.setattr(sys, "argv", ["check_mutants.py", str(record)])
    monkeypatch.setattr(sys, "stdin", _stdin_with(results))

    assert main() == 1
    out = capsys.readouterr().out
    assert "Unrecorded surviving mutants:" in out
    assert "lakana_core.policy.x__mutmut_2" in out
    assert "Targets with no mutants" not in out
