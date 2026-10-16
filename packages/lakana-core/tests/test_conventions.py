"""Tests that the lint configuration actually enforces our conventions."""

import subprocess
import textwrap
from pathlib import Path


def test_missing_docstring_is_rejected(tmp_path: Path) -> None:
    """A function without a docstring fails ruff."""
    bad = tmp_path / "bad.py"
    bad.write_text('"""Module."""\n\n\ndef f(a: int) -> int:\n    return a\n')
    result = subprocess.run(
        ["ruff", "check", "--no-cache", str(bad)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "undocumented-public-function" in result.stdout


def test_docstring_disagreeing_with_signature_is_rejected(tmp_path: Path) -> None:
    """A docstring documenting a parameter that does not exist fails ruff."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        textwrap.dedent(
            '''\
            """Module."""


            def f(a: int) -> int:
                """Do a thing.

                Parameters
                ----------
                a
                    The input.
                b
                    Does not exist.

                Returns
                -------
                int
                    The output.
                """
                return a
            '''
        )
    )
    result = subprocess.run(
        ["ruff", "check", "--no-cache", str(bad)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
