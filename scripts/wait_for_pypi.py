"""Wait until PyPI shows a release of all four packages, before anything installs it.

The ``five-minute-release`` job of ``release.yml`` installs ``lakana[server]`` from PyPI right
after the packages were published. PyPI's index can lag behind an upload by several minutes,
longer than the install's own retries cover. This script polls PyPI's JSON API
(``https://pypi.org/pypi/<name>/<version>/json``) every `POLL_SECONDS` until each package shows
the version, for at most `TIMEOUT_SECONDS`. It uses only the standard library, so the job needs
no environment for it.

Usage: ``python3 scripts/wait_for_pypi.py <version>``. Exit 0 when every package is visible, 1
after the timeout, 2 for a wrong call.
"""

from __future__ import annotations

import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence

PACKAGES = ("lakana", "lakana-core", "lakana-server", "lakana-worker")
"""The four distributions a release publishes."""

TIMEOUT_SECONDS = 600
"""How long to wait for all of them."""

POLL_SECONDS = 15
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


def fetch_status(url: str) -> int:
    """Read the HTTP status of a URL; a failure to connect counts as "not there yet".

    Parameters
    ----------
    url
        The URL.

    Returns
    -------
    int
        The status, or 0 when no answer came.
    """
    try:
        with urllib.request.urlopen(url, timeout=REQUEST_SECONDS) as response:  # nosec B310 -- fixed https URL
            return int(response.status)
    except urllib.error.HTTPError as error:
        return error.code
    except OSError:
        return 0


def wait_for_release(
    version: str,
    *,
    fetch: Callable[[str], int] = fetch_status,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Poll until every package shows the version.

    Parameters
    ----------
    version
        The release version.
    fetch
        Reads the status of a URL.
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
        missing = [n for n in PACKAGES if fetch(release_url(n, version)) != 200]
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
