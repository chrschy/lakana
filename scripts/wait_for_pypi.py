"""Wait until PyPI shows a release of all four packages, before anything installs it.

The ``five-minute-release`` job of ``release.yml`` installs ``lakana[server]`` from PyPI right
after the packages were published. PyPI's index can lag behind an upload by several minutes,
longer than the install's own retries cover. This script polls PyPI's JSON API
(``https://pypi.org/pypi/<name>/<version>/json``) and the simple index pip reads
(``https://pypi.org/simple/<name>/``) every `POLL_SECONDS` until both show the version of each
package, for at most `TIMEOUT_SECONDS`. It uses only the standard library, so the job needs
no environment for it.

Usage: ``python3 scripts/wait_for_pypi.py <version>``. Exit 0 when every package is visible, 1
after the timeout, 2 for a wrong call.
"""

from __future__ import annotations

import http.client
import re
import sys
import time
import urllib.request
from collections.abc import Callable, Sequence

PACKAGES = ("lakana", "lakana-core", "lakana-server", "lakana-worker")
"""The four distributions a release publishes."""

TIMEOUT_SECONDS = 900
"""How long to wait for all of them."""

POLL_SECONDS = 30
"""The pause between two polls."""

REQUEST_SECONDS = 20
"""The limit of one request."""


class ReleaseNotVisible(Exception):
    """PyPI did not show a release within the time limit."""


def release_url(name: str, version: str) -> str:
    """Build the JSON API URL of one release.

    Parameters
    ----------
    name
        The distribution name.
    version
        The release version.

    Returns
    -------
    str
        The URL; it answers 200 once PyPI shows the release.
    """
    return f"https://pypi.org/pypi/{name}/{version}/json"


def simple_url(name: str) -> str:
    """Build the simple index URL of one project, the page pip reads.

    Parameters
    ----------
    name
        The distribution name.

    Returns
    -------
    str
        The URL; its page lists the files of every release PyPI's index knows.
    """
    return f"https://pypi.org/simple/{name}/"


def lists_version(page: str, version: str) -> bool:
    """Tell whether a simple index page lists a file of the version.

    Parameters
    ----------
    page
        The HTML of the project's simple index page.
    version
        The release version.

    Returns
    -------
    bool
        True when a wheel or source distribution of exactly this version is listed.
    """
    return re.search(rf"[-_]{re.escape(version)}(?:-|\.tar\.gz|\.zip)", page) is not None


def fetch_body(url: str) -> str | None:
    """Read a page; a failure to connect counts as "not there yet".

    Parameters
    ----------
    url
        The URL.

    Returns
    -------
    str | None
        The body of a 200 answer, else None.
    """
    try:
        with urllib.request.urlopen(url, timeout=REQUEST_SECONDS) as response:  # nosec B310 -- fixed https URL
            return str(response.read().decode("utf-8", "replace"))
    except (OSError, http.client.HTTPException):  # HTTPError is an OSError; 404 means not yet
        return None


def is_visible(name: str, version: str, fetch: Callable[[str], str | None]) -> bool:
    """Tell whether both the JSON API and the simple index show a release.

    Parameters
    ----------
    name
        The distribution name.
    version
        The release version.
    fetch
        Reads a page: the body, or None when it is not there.

    Returns
    -------
    bool
        True when both answer.
    """
    if fetch(release_url(name, version)) is None:
        return False
    page = fetch(simple_url(name))
    return page is not None and lists_version(page, version)


def wait_for_release(
    version: str,
    *,
    fetch: Callable[[str], str | None] = fetch_body,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Poll until every package shows the version.

    Parameters
    ----------
    version
        The release version.
    fetch
        Reads a page: the body, or None when it is not there.
    sleep
        Pauses; replaced in tests.
    clock
        Reads the time in seconds; replaced in tests.

    Raises
    ------
    ReleaseNotVisible
        If a package is still missing after `TIMEOUT_SECONDS`.
    """
    start = clock()
    while True:
        missing = [n for n in PACKAGES if not is_visible(n, version, fetch)]
        if not missing:
            return
        waited = clock() - start
        if waited + POLL_SECONDS > TIMEOUT_SECONDS:
            raise ReleaseNotVisible(
                f"PyPI does not show {', '.join(missing)} {version} after {waited:.0f} s. "
                "The upload may have failed or PyPI's index is slow: check "
                f"https://pypi.org/project/{missing[0]}/{version}/, then re-run this job."
            )
        print(
            f"Waiting for PyPI to show {version} of {', '.join(missing)} "
            f"({waited:.0f} s of {TIMEOUT_SECONDS} s)...",
            file=sys.stderr,
        )
        sleep(POLL_SECONDS)


def main(argv: Sequence[str]) -> int:
    """Run from the command line.

    Parameters
    ----------
    argv
        The arguments after the program name: the version.

    Returns
    -------
    int
        The exit status.
    """
    if len(argv) != 1:
        print("usage: python3 scripts/wait_for_pypi.py <version>", file=sys.stderr)
        return 2
    try:
        wait_for_release(argv[0])
    except ReleaseNotVisible as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
