"""Loading a time zone from the `tzdata` package, so that every machine gives one answer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfoNotFoundError

import pytest

from lakana_core.jobs.tz import load_zone


def test_a_zone_loads_with_its_key_and_its_rules() -> None:
    zone = load_zone("Europe/Berlin")
    assert zone.key == "Europe/Berlin"
    summer = datetime(2026, 7, 1, 12, tzinfo=UTC).astimezone(zone)
    winter = datetime(2026, 1, 1, 12, tzinfo=UTC).astimezone(zone)
    assert (summer.utcoffset(), winter.utcoffset()) == (timedelta(hours=2), timedelta(hours=1))


@pytest.mark.parametrize(
    "name", ["UTC", "EST", "MST", "HST", "Etc/GMT+5", "America/Argentina/Buenos_Aires"]
)
def test_the_zones_tzdata_has_load(name: str) -> None:
    assert load_zone(name).key == name


def test_the_same_zone_is_loaded_once() -> None:
    assert load_zone("Asia/Tokyo") is load_zone("Asia/Tokyo")


@pytest.mark.parametrize(
    "name",
    [
        "",
        "Nowhere/Land",
        "../etc/passwd",
        "Europe/../UTC",
        "/etc/passwd",
        "Europe",
        "zone.tab",
        "UTC\x00",
    ],
)
def test_an_unknown_or_unsafe_name_is_refused(name: str) -> None:
    with pytest.raises(ZoneInfoNotFoundError):
        load_zone(name)


def test_a_name_in_another_case_is_refused_on_every_machine() -> None:
    with pytest.raises(ZoneInfoNotFoundError):
        load_zone("europe/berlin")
    with pytest.raises(ZoneInfoNotFoundError):
        load_zone("utc")
