"""Tests for the wait until PyPI shows a release, and for the release job that runs it."""

from __future__ import annotations

import http.client
import io
import urllib.error
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from scripts import wait_for_pypi as wait

ROOT = Path(__file__).resolve().parents[2]


class Clock:
    """A clock that only moves when the code sleeps."""

    def __init__(self) -> None:
        """Start at zero."""
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        """Read the clock.

        Returns
        -------
        float
            The seconds so far.
        """
        return self.now

    def sleep(self, seconds: float) -> None:
        """Advance the clock instead of waiting.

        Parameters
        ----------
        seconds
            How long the code asked to sleep.
        """
        self.sleeps.append(seconds)
        self.now += seconds


def fetcher(
    visible_after: dict[str, float], clock: Clock, *, simple_after: dict[str, float] | None = None
) -> Callable[[str], str | None]:
    """Build a fake `fetch`: a project's pages answer once the clock passed their time.

    Parameters
    ----------
    visible_after
        Package name → the second from which the JSON API shows its release; absent never.
    clock
        The fake clock.
    simple_after
        Package name → the second from which the simple index lists the release; defaults to
        `visible_after`.

    Returns
    -------
    Callable
        The fake, taking a URL and returning the body, or None for a 404.
    """
    simple = visible_after if simple_after is None else simple_after

    def fetch(url: str) -> str | None:
        """Answer for one URL.

        Parameters
        ----------
        url
            The JSON API URL or the simple index URL.

        Returns
        -------
        str | None
            The body once visible, else None.
        """
        if "/simple/" in url:
            name = url.split("/simple/")[1].strip("/")
            norm = name.replace("-", "_")
            at = simple.get(name, float("inf"))
            if clock.now < at:
                return f'<a href="{norm}-0.0.1.tar.gz">{norm}-0.0.1.tar.gz</a>'
            return f'<a href="{norm}-1.2.3-py3-none-any.whl">x</a>'
        name = url.split("/pypi/")[1].split("/")[0]
        return "{}" if clock.now >= visible_after.get(name, float("inf")) else None

    return fetch


def test_the_url_is_the_json_api_of_the_version() -> None:
    """One URL per package and version."""
    assert (
        wait.release_url("lakana-core", "1.2.3") == "https://pypi.org/pypi/lakana-core/1.2.3/json"
    )


def test_it_returns_at_once_when_every_package_is_visible() -> None:
    """No sleeping when PyPI is already up to date."""
    clock = Clock()
    fetch = fetcher(dict.fromkeys(wait.PACKAGES, 0.0), clock)
    wait.wait_for_release("1.2.3", fetch=fetch, sleep=clock.sleep, clock=clock.time)
    assert clock.sleeps == []


def test_the_simple_url_is_the_index_page_of_the_project() -> None:
    """The page pip reads, with the name as given."""
    assert wait.simple_url("lakana-core") == "https://pypi.org/simple/lakana-core/"


def test_it_polls_every_thirty_seconds_until_the_last_package_shows_up(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A lagging package is waited for, and the wait says which one."""
    clock = Clock()
    fetch = fetcher({**dict.fromkeys(wait.PACKAGES, 0.0), "lakana-worker": 100.0}, clock)
    wait.wait_for_release("1.2.3", fetch=fetch, sleep=clock.sleep, clock=clock.time)
    assert clock.sleeps == [30.0] * 4
    assert "lakana-worker" in capsys.readouterr().err


def test_a_package_the_simple_index_does_not_list_yet_is_waited_for(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The JSON API alone is not enough: pip reads the simple index, which lags behind it."""
    clock = Clock()
    fetch = fetcher(
        dict.fromkeys(wait.PACKAGES, 0.0),
        clock,
        simple_after={**dict.fromkeys(wait.PACKAGES, 0.0), "lakana-server": 200.0},
    )
    wait.wait_for_release("1.2.3", fetch=fetch, sleep=clock.sleep, clock=clock.time)
    assert 200.0 <= clock.now < 240.0
    assert "lakana-server" in capsys.readouterr().err


def test_a_similar_version_on_the_simple_index_does_not_count() -> None:
    """1.2.3 is not found in a listing of 1.2.30 or 11.2.3."""
    page = '<a href="lakana-1.2.30.tar.gz">x</a><a href="lakana-11.2.3.tar.gz">x</a>'
    assert not wait.lists_version(page, "1.2.3")
    assert wait.lists_version('<a href="lakana-1.2.3.tar.gz">x</a>', "1.2.3")
    assert wait.lists_version('<a href="lakana-1.2.3-py3-none-any.whl">x</a>', "1.2.3")
    assert wait.lists_version('<a href="lakana_core-1.2.3-py3-none-any.whl">x</a>', "1.2.3")


def test_it_gives_up_after_the_limit_and_names_the_missing_packages() -> None:
    """The timeout message names the cause and the next step."""
    clock = Clock()
    fetch = fetcher({"lakana": 0.0, "lakana-core": 0.0}, clock)
    with pytest.raises(wait.ReleaseNotVisible) as raised:
        wait.wait_for_release("1.2.3", fetch=fetch, sleep=clock.sleep, clock=clock.time)
    message = str(raised.value)
    assert "lakana-server" in message and "lakana-worker" in message
    assert "lakana-core" not in message
    assert "1.2.3" in message and "re-run" in message
    assert clock.now == wait.TIMEOUT_SECONDS


def test_the_defaults_are_fifteen_minutes_and_thirty_seconds() -> None:
    """The bounds the release job relies on."""
    assert wait.TIMEOUT_SECONDS == 900
    assert wait.POLL_SECONDS == 30
    assert wait.PACKAGES == ("lakana", "lakana-core", "lakana-server", "lakana-worker")


def test_fetch_body_reads_a_page_and_treats_a_failure_as_not_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 404 and a dropped connection both mean "not yet" (None); neither raises."""

    class Response(io.BytesIO):
        """A response that is a context manager with a status."""

        status = 200

        def __enter__(self) -> Response:
            """Enter the block.

            Returns
            -------
            Response
                Itself.
            """
            return self

    def ok(url: str, timeout: float) -> Response:
        """Answer 200.

        Parameters
        ----------
        url
            Ignored.
        timeout
            Ignored.

        Returns
        -------
        Response
            A 200.
        """
        return Response(b"page")

    monkeypatch.setattr(wait.urllib.request, "urlopen", ok)
    assert wait.fetch_body("https://pypi.org/x") == "page"

    def not_found(url: str, timeout: float) -> Response:
        """Answer 404.

        Parameters
        ----------
        url
            The URL.
        timeout
            Ignored.

        Raises
        ------
        urllib.error.HTTPError
            Always.
        """
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(wait.urllib.request, "urlopen", not_found)
    assert wait.fetch_body("https://pypi.org/x") is None

    def dropped(url: str, timeout: float) -> Response:
        """Fail the connection.

        Parameters
        ----------
        url
            Ignored.
        timeout
            Ignored.

        Raises
        ------
        urllib.error.URLError
            Always.
        """
        raise urllib.error.URLError("connection reset")

    monkeypatch.setattr(wait.urllib.request, "urlopen", dropped)
    assert wait.fetch_body("https://pypi.org/x") is None


def test_main_waits_for_the_version_in_the_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 0 when visible, 1 with the message when not, 2 without a version."""
    seen: list[str] = []
    monkeypatch.setattr(wait, "wait_for_release", lambda version: seen.append(version))
    assert wait.main(["1.2.3"]) == 0
    assert seen == ["1.2.3"]

    def refuse(version: str) -> None:
        """Time out.

        Parameters
        ----------
        version
            The version.

        Raises
        ------
        ReleaseNotVisible
            Always.
        """
        raise wait.ReleaseNotVisible("PyPI does not show it")

    monkeypatch.setattr(wait, "wait_for_release", refuse)
    assert wait.main(["1.2.3"]) == 1
    assert "PyPI does not show it" in capsys.readouterr().err
    assert wait.main([]) == 2
    assert "usage" in capsys.readouterr().err


def test_the_release_job_waits_before_its_first_install() -> None:
    """The script runs after checkout, before the five-minute script, with no secret."""
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text())
    steps = workflow["jobs"]["five-minute-release"]["steps"]
    waits = [i for i, s in enumerate(steps) if "scripts/wait_for_pypi.py" in s.get("run", "")]
    installs = [i for i, s in enumerate(steps) if "five_minute_test.sh" in s.get("run", "")]
    assert len(waits) == 1 and len(installs) == 1 and waits[0] < installs[0]
    assert "secrets." not in yaml.dump(steps[waits[0]])
    assert "steps.version.outputs.version" in steps[waits[0]]["run"]


def test_a_truncated_body_counts_as_not_there_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    """A read cut short (IncompleteRead is no OSError) is waited out, not raised."""

    class Cut:
        """A response whose body is cut short."""

        def __enter__(self) -> Cut:
            """Enter the block.

            Returns
            -------
            Cut
                Itself.
            """
            return self

        def __exit__(self, *exc: object) -> None:
            """Leave the block.

            Parameters
            ----------
            *exc
                Ignored.
            """

        def read(self) -> bytes:
            """Fail the read.

            Raises
            ------
            http.client.IncompleteRead
                Always.
            """
            raise http.client.IncompleteRead(b"par")

    monkeypatch.setattr(wait.urllib.request, "urlopen", lambda url, timeout: Cut())
    assert wait.fetch_body("https://pypi.org/x") is None
