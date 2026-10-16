"""Decide whether a pull request changes documentation only.

CI skips its heavy jobs for a pull request that changes nothing but documentation.
This script is the whole definition of "documentation": it reads the changed paths,
one per line, from standard input and prints ``true`` or ``false``.

The example projects under ``docs/examples/`` are not documentation, whatever their
suffix: they are code that CI runs, and a change to one must run it.

Every doubt resolves to ``false``, which runs the whole pipeline: an empty list, a
path that is not plainly relative, and a listing long enough to have been cut short
by the pull-request files API.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

MAX_FILES = 3000
"""The pull-request files API returns at most this many entries."""


def _is_documentation(path: str) -> bool:
    """Say whether one changed path is documentation.

    Parameters
    ----------
    path
        A repository-relative path as the pull-request files API reports it.

    Returns
    -------
    bool
        ``True`` for a file under ``docs/`` other than one under ``docs/examples/``,
        for ``mkdocs.yml`` at the repository root (the site's navigation: a new page
        needs an entry), and for a Markdown file that is not inside a package's ``src`` tree, where
        it ships as package data. A Markdown file under ``docs/examples/`` is not
        documentation either.
    """
    parts = path.split("/")
    if not path or "\\" in path or any(part in ("", ".", "..") for part in parts):
        return False
    if path == "mkdocs.yml":
        return True
    if parts[:2] == ["docs", "examples"]:
        return False
    if parts[0] == "docs":
        return len(parts) > 1
    if not path.endswith(".md"):
        return False
    return not (parts[0] == "packages" and len(parts) > 2 and parts[2] == "src")


def is_docs_only(paths: Sequence[str]) -> bool:
    """Say whether a set of changed paths is documentation only.

    Parameters
    ----------
    paths
        The changed paths of a pull request, old names of renamed files included.

    Returns
    -------
    bool
        ``True`` only if there is at least one path, fewer than `MAX_FILES`, and
        every one of them is documentation.
    """
    if not paths or len(paths) >= MAX_FILES:
        return False
    return all(_is_documentation(path) for path in paths)


def main() -> int:
    """Read changed paths from standard input and print the verdict.

    Returns
    -------
    int
        Always ``0``; the verdict is the output.
    """
    paths = sys.stdin.read().splitlines()
    print("true" if is_docs_only(paths) else "false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
