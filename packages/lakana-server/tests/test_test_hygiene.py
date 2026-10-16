"""Guard: server tests never share a fixed storage root."""

from pathlib import Path


def test_no_server_test_hard_codes_a_tmp_storage_root() -> None:
    """Every storage root comes from `tmp_path`, so runs can't interfere or leak files."""
    tests = Path(__file__).parent
    offenders = [
        f"{path.relative_to(tests)}:{n}"
        for path in tests.rglob("*.py")
        if path.name != Path(__file__).name
        for n, line in enumerate(path.read_text().splitlines(), start=1)
        if "file:///tmp/lakana-test" in line
    ]

    assert not offenders, offenders
