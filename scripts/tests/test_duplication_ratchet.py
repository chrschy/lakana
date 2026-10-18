"""Tests for the duplication ratchet."""

import json
from pathlib import Path

import pytest

from scripts.duplication_ratchet import (
    Baseline,
    Clone,
    check,
    main,
    measure,
    parse_report,
    read_baseline,
    write_baseline,
)


def _clone(
    first: str = "pkg/a.py",
    second: str = "pkg/b.py",
    start: int = 10,
    lines: int = 12,
) -> dict:
    """Build one entry of jscpd's ``duplicates`` list.

    Parameters
    ----------
    first
        Name of the first file.
    second
        Name of the second file.
    start
        First line of the block in both files.
    lines
        Length of the block.

    Returns
    -------
    dict
        The entry, with the keys the ratchet reads.
    """
    end = start + lines - 1
    return {
        "firstFile": {"name": first, "start": start, "end": end},
        "secondFile": {"name": second, "start": start + 100, "end": end + 100},
        "lines": lines,
    }


def _report(*clones: dict) -> str:
    """Wrap clone entries in the shape of jscpd's JSON report.

    Parameters
    ----------
    *clones
        Entries from `_clone`.

    Returns
    -------
    str
        The report text.
    """
    return json.dumps({"duplicates": list(clones), "statistics": {}})


def test_a_report_becomes_clones_with_their_places() -> None:
    """Each entry gives both files, both line ranges and the length."""
    (clone,) = parse_report(_report(_clone()))
    assert clone == Clone("pkg/a.py", 10, 21, "pkg/b.py", 110, 121, 12)
    assert clone.describe() == "pkg/a.py:10-21 ↔ pkg/b.py:110-121 (12 lines)"


def test_a_pair_is_named_the_same_whichever_file_comes_first() -> None:
    """The key of a clone does not depend on jscpd's order of the two files."""
    ab = parse_report(_report(_clone("pkg/a.py", "pkg/b.py")))[0]
    ba = parse_report(_report(_clone("pkg/b.py", "pkg/a.py")))[0]
    assert ab.pair == ba.pair == "pkg/a.py ↔ pkg/b.py"


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        "{}",
        '{"duplicates": 3}',
        '{"duplicates": [{}]}',
        '{"duplicates": [{"firstFile": 1, "secondFile": 2, "lines": 3}]}',
        json.dumps({"duplicates": [{**_clone(), "lines": "many"}]}),
        json.dumps({"duplicates": [{**_clone(), "lines": True}]}),
    ],
)
def test_a_report_that_is_not_jscpd_s_is_refused(text: str) -> None:
    """Anything but a jscpd JSON report raises ``ValueError``."""
    with pytest.raises(ValueError, match="jscpd"):
        parse_report(text)


def test_measure_totals_the_lines_and_the_clones_per_pair() -> None:
    """The totals are the sums; each pair keeps its own count and lines."""
    clones = parse_report(
        _report(_clone(lines=10), _clone(start=50, lines=5), _clone("pkg/c.py", lines=7))
    )
    assert measure(clones) == Baseline(
        duplicated_lines=22,
        clones=3,
        pairs={
            "pkg/a.py ↔ pkg/b.py": (2, 15),
            "pkg/b.py ↔ pkg/c.py": (1, 7),
        },
    )


def test_the_baseline_round_trips(tmp_path: Path) -> None:
    """A written baseline reads back identically."""
    baseline = Baseline(duplicated_lines=15, clones=2, pairs={"x ↔ y": (2, 15)})
    path = tmp_path / "baseline.json"
    write_baseline(path, baseline)
    assert read_baseline(path) == baseline


def test_a_missing_baseline_is_an_error_that_names_the_next_step(tmp_path: Path) -> None:
    """Without a file there is nothing to compare with; `--update` creates it."""
    with pytest.raises(FileNotFoundError, match="--update"):
        read_baseline(tmp_path / "absent.json")


@pytest.mark.parametrize(
    "text",
    [
        "nope",
        "[]",
        '{"duplicated_lines": 1}',
        '{"duplicated_lines": -1, "clones": 0, "pairs": {}}',
        '{"duplicated_lines": 1, "clones": 0, "pairs": []}',
        '{"duplicated_lines": 1, "clones": 0, "pairs": {"a": [1]}}',
        '{"duplicated_lines": true, "clones": 0, "pairs": {}}',
    ],
)
def test_a_damaged_baseline_is_refused(tmp_path: Path, text: str) -> None:
    """A baseline that is not ours raises ``ValueError`` naming the file."""
    path = tmp_path / "baseline.json"
    path.write_text(text)
    with pytest.raises(ValueError, match=r"baseline\.json"):
        read_baseline(path)


def _clones(*entries: dict) -> list[Clone]:
    """Parse clone entries.

    Parameters
    ----------
    *entries
        Entries from `_clone`.

    Returns
    -------
    list of Clone
        The clones.
    """
    return parse_report(_report(*entries))


def test_the_same_duplication_is_accepted() -> None:
    """Equal numbers pass."""
    clones = _clones(_clone())
    assert check(clones, measure(clones)) is None


def test_a_fall_is_accepted() -> None:
    """Fewer duplicated lines and clones pass."""
    before = measure(_clones(_clone(), _clone(start=50)))
    assert check(_clones(_clone()), before) is None


def test_a_new_clone_fails_and_is_listed_with_its_places() -> None:
    """The message gives both numbers, the new pair's blocks and what to do."""
    baseline = measure(_clones(_clone()))
    message = check(_clones(_clone(), _clone("pkg/c.py", "pkg/d.py", start=30, lines=9)), baseline)
    assert message is not None
    assert "21 lines in 2 clones" in message and "12 lines in 1 clone" in message
    assert "pkg/c.py:30-38 ↔ pkg/d.py:130-138 (9 lines)" in message
    assert "pkg/a.py:10-21" not in message
    assert "Extract" in message and "jscpd:ignore-start" in message and "reason" in message


def test_a_clone_added_to_a_known_pair_lists_the_pair() -> None:
    """A second block between two files that already share one names that pair."""
    baseline = measure(_clones(_clone()))
    message = check(_clones(_clone(), _clone(start=60, lines=8)), baseline)
    assert message is not None
    assert "pkg/a.py:60-67" in message


def test_a_clone_that_grew_lists_its_pair() -> None:
    """More lines in an existing clone raise the total and name the pair."""
    baseline = measure(_clones(_clone(lines=10)))
    message = check(_clones(_clone(lines=14)), baseline)
    assert message is not None
    assert "10 lines in 1 clone" in message and "14 lines in 1 clone" in message
    assert "pkg/a.py:10-23" in message


def test_one_more_clone_fails_even_when_the_lines_fall() -> None:
    """A clone split in two raises the count; the count may not rise either."""
    baseline = measure(_clones(_clone(lines=20)))
    message = check(_clones(_clone(lines=8), _clone(start=50, lines=8)), baseline)
    assert message is not None


def test_a_rise_hidden_by_a_fall_elsewhere_is_named_only_if_the_total_rises() -> None:
    """Totals decide: a new clone paid for by a removed one passes."""
    baseline = measure(_clones(_clone(lines=20)))
    assert check(_clones(_clone("pkg/c.py", "pkg/d.py", lines=10)), baseline) is None


@pytest.fixture
def in_tmp(tmp_path: Path) -> Path:
    """Give a test a directory holding a report and a baseline.

    Parameters
    ----------
    tmp_path
        The test's temporary directory.

    Returns
    -------
    Path
        `tmp_path`.
    """
    return tmp_path


def _write(directory: Path, report: list[dict], baseline: list[dict] | None) -> list[str]:
    """Lay out a report and a baseline and build the command line.

    Parameters
    ----------
    directory
        Where the files go.
    report
        Clone entries of the current run.
    baseline
        Clone entries the baseline records, or ``None`` for no baseline file.

    Returns
    -------
    list of str
        Arguments pointing the ratchet at those files.
    """
    (directory / "report.json").write_text(_report(*report))
    if baseline is not None:
        write_baseline(directory / "baseline.json", measure(_clones(*baseline)))
    return [
        "--report",
        str(directory / "report.json"),
        "--baseline",
        str(directory / "baseline.json"),
    ]


def _run(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> int:
    """Invoke `main` with `args` as its command line.

    Parameters
    ----------
    monkeypatch
        Used to set ``sys.argv``.
    args
        Arguments after the program name.

    Returns
    -------
    int
        The exit status.
    """
    monkeypatch.setattr("sys.argv", ["duplication_ratchet", *args])
    return main()


def test_main_passes_at_the_baseline(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Equal duplication exits 0 and prints both figures."""
    args = _write(in_tmp, [_clone()], [_clone()])
    assert _run(monkeypatch, args) == 0
    assert "Duplication 12 lines in 1 clone (baseline 12 lines in 1 clone)." in (
        capsys.readouterr().out
    )


def test_main_fails_on_a_rise_and_leaves_the_baseline_alone(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A rise exits 1 with the new blocks; `--update` does not hide it."""
    args = _write(in_tmp, [_clone(), _clone(start=50)], [_clone()])
    before = (in_tmp / "baseline.json").read_text()
    assert _run(monkeypatch, [*args, "--update"]) == 1
    assert "pkg/a.py:50-61" in capsys.readouterr().err
    assert (in_tmp / "baseline.json").read_text() == before


def test_main_on_a_fall_says_to_lower_the_baseline(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A fall passes, prints the new numbers and the command that records them."""
    args = _write(in_tmp, [_clone()], [_clone(), _clone(start=50)])
    assert _run(monkeypatch, args) == 0
    out = capsys.readouterr().out
    assert "12 lines in 1 clone" in out and "24 lines in 2 clones" in out
    assert "--update" in out and "same pull request" in out


def test_main_update_lowers_the_baseline(in_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--update` records a fall."""
    args = _write(in_tmp, [_clone()], [_clone(), _clone(start=50)])
    assert _run(monkeypatch, [*args, "--update"]) == 0
    assert read_baseline(in_tmp / "baseline.json").duplicated_lines == 12


def test_main_update_leaves_an_equal_baseline_alone(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nothing to lower, `--update` writes nothing."""
    args = _write(in_tmp, [_clone()], [_clone()])
    before = (in_tmp / "baseline.json").read_text()
    assert _run(monkeypatch, [*args, "--update"]) == 0
    assert (in_tmp / "baseline.json").read_text() == before


def test_main_update_creates_a_missing_baseline(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first run records today's measurement."""
    args = _write(in_tmp, [_clone()], None)
    assert _run(monkeypatch, [*args, "--update"]) == 0
    assert read_baseline(in_tmp / "baseline.json").clones == 1


def test_main_without_a_baseline_says_how_to_create_one(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing baseline exits 1 with the command."""
    args = _write(in_tmp, [_clone()], None)
    assert _run(monkeypatch, args) == 1
    assert "--update" in capsys.readouterr().err


def test_main_refuses_a_damaged_baseline(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A baseline that does not parse exits 1 and says to restore it."""
    args = _write(in_tmp, [_clone()], None)
    (in_tmp / "baseline.json").write_text("nope")
    assert _run(monkeypatch, args) == 1
    err = capsys.readouterr().err
    assert "baseline.json" in err and "git" in err


def test_main_refuses_a_missing_report(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without jscpd's report the ratchet says how to produce one."""
    args = _write(in_tmp, [_clone()], [_clone()])
    (in_tmp / "report.json").unlink()
    assert _run(monkeypatch, args) == 1
    assert "jscpd" in capsys.readouterr().err


def test_main_refuses_a_report_that_is_not_jscpd_s(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed report exits 1 instead of scoring it as zero duplication."""
    args = _write(in_tmp, [_clone()], [_clone()])
    (in_tmp / "report.json").write_text("{}")
    assert _run(monkeypatch, args) == 1
    assert "jscpd" in capsys.readouterr().err


def test_main_refuses_an_unreadable_baseline(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A baseline path that is a directory is an error that names the file."""
    args = _write(in_tmp, [_clone()], None)
    (in_tmp / "baseline.json").mkdir()
    assert _run(monkeypatch, args) == 1
    assert "baseline.json" in capsys.readouterr().err
