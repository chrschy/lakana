"""Repo hygiene: the project's former name must not come back.

The rename was a clean break, with no alias and no fallback environment variable, so any
tracked path or line that still carries the old name is a leftover or a regression. This
test scans everything git tracks and fails on the first kind of hit.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# The old name in any case and any position, glued forms and plurals included
# (`proa_core`, `ProaError`, `proatest`, `myproa`, `XPROA_URL`). A denylist rather than
# word boundaries: the only ordinary words in the tree that contain the string are
# "approach(es)" and "proactive(ly)", so those two continuations are the whole exception.
LEGACY = re.compile(r"(?i)proa(?!ch|ctiv)")

# This file has to spell the old name to look for it.
ALLOWED_FILES = frozenset({"scripts/tests/test_no_legacy_name.py"})


def _tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    ).stdout
    return sorted(p for p in out.decode().split("\0") if p)


def find_content_hits(path: str, text: str) -> list[str]:
    """Find the lines of one file that name the old project.

    Parameters
    ----------
    path
        Tracked path of the file, relative to the repository root.
    text
        Its content.

    Returns
    -------
    list[str]
        One ``path:line: text`` entry per offending line.
    """
    return [
        f"{path}:{number}: {line.strip()[:120]}"
        for number, line in enumerate(text.splitlines(), start=1)
        if LEGACY.search(line)
    ]


@pytest.mark.parametrize(
    "text",
    [
        "proa",
        "Proa",
        "PROA_URL",
        "proa_core",
        "proa-server",
        "ProaError",
        "/* proa",
        "isProaX",
        "proatest",
        "myproa",
        "Proas",
        "XPROA_URL",
    ],
)
def test_pattern_matches_the_old_name(text: str) -> None:
    assert LEGACY.search(text)


@pytest.mark.parametrize("text", ["approach", "Approaches", "proactive", "Proactively", "APPROACH"])
def test_pattern_ignores_ordinary_words(text: str) -> None:
    assert not LEGACY.search(text)


def test_a_line_that_names_the_old_name_is_a_hit() -> None:
    assert find_content_hits("README.md", "ok\nrun `proa`.\n") == ["README.md:2: run `proa`."]
    assert find_content_hits("README.md", "an approach") == []


def test_no_tracked_path_carries_the_old_name() -> None:
    hits = [p for p in _tracked() if p not in ALLOWED_FILES and LEGACY.search(p)]
    assert not hits, f"{len(hits)} tracked paths still carry the old name:\n" + "\n".join(hits[:40])


def test_no_tracked_file_mentions_the_old_name() -> None:
    paths = _tracked()
    hits: list[str] = []
    for path in paths:
        if path in ALLOWED_FILES:
            continue
        file = REPO_ROOT / path
        if not file.is_file():
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary: only its path is checked
        hits.extend(find_content_hits(path, text))
    assert not hits, f"{len(hits)} lines still mention the old name:\n" + "\n".join(hits[:40])
