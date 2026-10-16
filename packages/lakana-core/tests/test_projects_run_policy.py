"""Retry and timeout policy: durations, merging, caps and manifest read-back."""

from __future__ import annotations

import pytest
from test_projects_dag import make_asset

from lakana_core.config import QueryLimitsConfig, RunConfig
from lakana_core.projects.frontmatter import AssetMetadata
from lakana_core.projects.project_config import parse_duration
from lakana_core.projects.run_policy import (
    DEFAULT_RETRIES,
    DEFAULT_RETRY_DELAY_SECONDS,
    MAX_RETRY_DELAY_SECONDS,
    RunCaps,
    RunPolicy,
    policy_from_manifest,
    resolve_run_policy,
    run_policy_issues,
)


@pytest.mark.parametrize(
    ("value", "seconds"),
    [(0, 0), (45, 45), ("90s", 90), ("5m", 300), ("1h", 3600), ("1h30m", 5400), ("2m5s", 125)],
)
def test_durations(value: object, seconds: int) -> None:
    """Integers are seconds; strings are h/m/s parts in that order."""
    assert parse_duration(value) == seconds


@pytest.mark.parametrize(
    "value", ["", "5", "1m1h", "5 m", "-5s", "1.5s", "\uff15m", "1m\uff15s", True, None, 2.5, []]
)
def test_bad_durations(value: object) -> None:
    """Anything else is a `ValueError` (which pydantic reports as a field error)."""
    with pytest.raises(ValueError, match="duration"):
        parse_duration(value)


def test_negative_integer_duration_is_refused() -> None:
    """A negative number of seconds is not a duration."""
    with pytest.raises(ValueError, match="duration"):
        parse_duration(-1)


def test_defaults_when_nothing_is_set() -> None:
    """Retries 2, retry_delay 30 s, timeout = the operator cap."""
    caps = RunCaps(max_retries=5, max_timeout_seconds=600)
    assert resolve_run_policy(AssetMetadata(), caps) == RunPolicy(
        retries=DEFAULT_RETRIES,
        retry_delay_seconds=DEFAULT_RETRY_DELAY_SECONDS,
        timeout_seconds=600,
    )


def test_explicit_values_win() -> None:
    """The merged metadata's values are used as they are."""
    metadata = AssetMetadata(retries=0, retry_delay=5, timeout=60)
    assert resolve_run_policy(metadata, RunCaps()) == RunPolicy(0, 5, 60)


def test_values_above_a_cap_are_issues_with_the_asset_path() -> None:
    """Deploy validation refuses retries above LAKANA_RUN__MAX_RETRIES and timeouts over the cap."""
    caps = RunCaps(max_retries=3, max_timeout_seconds=100)
    assets = [
        make_asset("gold.ok", metadata=AssetMetadata(materialize="table", retries=3, timeout=100)),
        make_asset("gold.many", metadata=AssetMetadata(materialize="table", retries=4)),
        make_asset("gold.slow", metadata=AssetMetadata(materialize="table", timeout=101)),
    ]
    rendered = [issue.render() for issue in run_policy_issues(assets, caps)]
    assert rendered == [
        "assets/gold/many.sql: retries 4 is above the operator maximum of 3 "
        "(LAKANA_RUN__MAX_RETRIES)",
        "assets/gold/slow.sql: timeout 101s is above the operator maximum of 100s "
        "(LAKANA_QUERY_LIMITS__TIMEOUT_SECONDS)",
    ]


def test_retry_delay_above_the_backoff_ceiling_is_an_issue() -> None:
    """A retry delay over 10 minutes is refused at deploy, with the asset path."""
    assets = [
        make_asset("gold.ok", metadata=AssetMetadata(materialize="table", retry_delay=600)),
        make_asset("gold.late", metadata=AssetMetadata(materialize="table", retry_delay=601)),
    ]
    rendered = [issue.render() for issue in run_policy_issues(assets, RunCaps())]
    assert rendered == [
        "assets/gold/late.sql: retry_delay 601s is above the maximum of 600s "
        "(the retry backoff ceiling)"
    ]


def test_manifest_retry_delay_is_clamped_to_the_backoff_ceiling() -> None:
    """An old or hand-made manifest cannot carry an absurd delay into the scheduler."""
    entry = {"retries": 1, "retry_delay_seconds": 10**12, "timeout_seconds": 50}
    assert policy_from_manifest(entry, RunCaps()).retry_delay_seconds == MAX_RETRY_DELAY_SECONDS


def test_caps_from_config() -> None:
    """The timeout cap is the query limit, rounded down, never below one second."""
    assert RunCaps.from_config(
        QueryLimitsConfig(timeout_seconds=90.7), RunConfig(max_retries=1)
    ) == (RunCaps(max_retries=1, max_timeout_seconds=90))
    assert (
        RunCaps.from_config(QueryLimitsConfig(timeout_seconds=0.2), RunConfig()).max_timeout_seconds
        == 1
    )


def test_policy_from_a_current_manifest_entry() -> None:
    """A manifest entry's resolved values are read back."""
    entry = {"retries": 1, "retry_delay_seconds": 7, "timeout_seconds": 50}
    assert policy_from_manifest(entry, RunCaps()) == RunPolicy(1, 7, 50)


def test_policy_from_a_pre_m5c1_manifest_entry_uses_the_defaults() -> None:
    """An older manifest has no policy keys."""
    caps = RunCaps(max_retries=5, max_timeout_seconds=120)
    assert policy_from_manifest({"table": "gold.daily"}, caps) == RunPolicy(
        DEFAULT_RETRIES, DEFAULT_RETRY_DELAY_SECONDS, 120
    )


@pytest.mark.parametrize(
    "entry",
    [
        {"retries": "2", "retry_delay_seconds": None, "timeout_seconds": "x"},
        {"retries": -1, "retry_delay_seconds": -5, "timeout_seconds": 0},
        {"retries": True, "retry_delay_seconds": False, "timeout_seconds": True},
    ],
)
def test_malformed_manifest_values_fall_back_to_the_defaults(entry: dict) -> None:
    """A manifest read back from the database never raises over a bad value."""
    caps = RunCaps(max_retries=5, max_timeout_seconds=120)
    assert policy_from_manifest(entry, caps) == RunPolicy(2, 30, 120)


def test_manifest_values_are_clamped_to_caps_lowered_after_the_deploy() -> None:
    """The current cap wins at execution."""
    entry = {"retries": 5, "retry_delay_seconds": 1, "timeout_seconds": 900}
    assert policy_from_manifest(entry, RunCaps(max_retries=2, max_timeout_seconds=60)) == (
        RunPolicy(2, 1, 60)
    )
