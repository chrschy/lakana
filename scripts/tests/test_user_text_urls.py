"""Text that leaves the repository names documentation pages by URL, never by repository path.

The files `lakana init` writes, the Compose file and `.env` that `lakana bundle init` writes, the
messages the CLI and the server print, and the READMEs that PyPI shows reach readers who have no
checkout. A path such as ``docs/site/how-to/projects.md`` means nothing to them; a URL on the
documentation site does, and this test checks that each URL points at a page that exists.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "docs" / "site"
BASE = "https://lakana-labs.github.io/lakana/"
LEAVING = (
    "packages/lakana/src/lakana/cli/project_init.py",
    "packages/lakana/src/lakana/cli/server.py",
    "packages/lakana/src/lakana/cli/deploy/compose.yaml",
    "packages/lakana/src/lakana/cli/deploy/_init.py",
    "packages/lakana-core/src/lakana_core/grants/admin.py",
    "packages/lakana/README.md",
    "packages/lakana-core/README.md",
    "packages/lakana-server/README.md",
    "packages/lakana-worker/README.md",
)
"""Files whose text reaches a reader outside the repository."""
_REPO_PATH = re.compile(r"docs/(?:site|agent)/")
_URL = re.compile(re.escape(BASE) + r"([A-Za-z0-9_/-]*)(?:#[A-Za-z0-9_-]+)?")


def _page_of(url_path: str) -> Path:
    """Return the source page that the site serves at a URL path.

    Parameters
    ----------
    url_path
        The part of the URL after the site's base, for example ``how-to/projects/``.

    Returns
    -------
    pathlib.Path
        ``docs/site/<path>.md``, or the ``index.md`` of the directory, whichever the URL names.
    """
    stripped = url_path.strip("/")
    if stripped == "":
        return SITE / "index.md"
    if url_path.endswith("/"):
        return (
            SITE / stripped / "index.md" if (SITE / stripped).is_dir() else SITE / f"{stripped}.md"
        )
    return SITE / f"{stripped}.md"


def test_text_that_leaves_the_repository_names_no_repository_path() -> None:
    """No file that reaches a reader outside the repository names a ``docs/`` path."""
    found = [
        f"{name}:{number}: {line.strip()}"
        for name in LEAVING
        for number, line in enumerate((ROOT / name).read_text(encoding="utf-8").splitlines(), 1)
        if _REPO_PATH.search(line)
    ]
    assert found == [], (
        "name the documentation page by its URL, "
        "https://lakana-labs.github.io/lakana/<kind>/<page>/ "
        f"(a user has no checkout): {found}"
    )


def test_every_documentation_url_in_that_text_points_to_a_page_that_exists() -> None:
    """Each site URL maps to a page under ``docs/site``; a renamed page fails here."""
    missing = []
    seen = 0
    for name in LEAVING:
        text = (ROOT / name).read_text(encoding="utf-8")
        for match in _URL.finditer(text):
            seen += 1
            if not _page_of(match.group(1)).is_file():
                missing.append(f"{name}: {match.group(0)}")
    assert missing == [], f"no page under docs/site for these URLs: {missing}"
    assert seen >= 10, "expected the documentation URLs of the files that leave the repository"


def test_the_url_of_a_page_maps_to_its_source_file() -> None:
    """The mapping the test relies on: directory URLs, the index page and a nested page."""
    assert _page_of("") == SITE / "index.md"
    assert _page_of("how-to/projects/") == SITE / "how-to" / "projects.md"
    assert _page_of("how-to/") == SITE / "how-to" / "index.md"
    assert _page_of("how-to/projects") == SITE / "how-to" / "projects.md"
