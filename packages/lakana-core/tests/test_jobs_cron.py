"""The syntax check of a cron expression: five standard fields, each value in range."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from lakana_core.jobs.cron import (
    _fires_after,
    _parse,
    fire_times_between,
    next_fire_after,
    validate_cron_can_fire,
    validate_cron_syntax,
)

BERLIN = ZoneInfo("Europe/Berlin")


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def local(instant: datetime, zone: str = "Europe/Berlin") -> str:
    return instant.astimezone(ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M")


def fires(expr: str, tz: str, start: datetime, count: int) -> list[datetime]:
    out: list[datetime] = []
    current = start
    for _ in range(count):
        found = next_fire_after(expr, tz, current)
        assert found is not None
        out.append(found)
        current = found
    return out


@pytest.mark.parametrize(
    "expression",
    [
        "* * * * *",
        "0 6 * * *",
        "15 * * * *",
        "*/5 0-6 1,15 * mon-fri",
        "0 0 1 jan,JUL *",
        "30 2 * * 7",
        "0 0 * * sun",
        "10-50/10 * * * *",
        "@hourly",
        "@daily",
        "@weekly",
        "@monthly",
        "@yearly",
    ],
)
def test_a_valid_expression_passes(expression: str) -> None:
    validate_cron_syntax(expression)


@pytest.mark.parametrize(
    ("expression", "needle"),
    [
        ("* * * * * *", "5 fields"),
        ("0 * * * * *", "seconds"),
        ("* * * *", "5 fields"),
        ("", "5 fields"),
        ("60 * * * *", "minute"),
        ("* 24 * * *", "hour"),
        ("* * 0 * *", "day of month"),
        ("* * 32 * *", "day of month"),
        ("* * * 13 *", "month"),
        ("* * * * 8", "day of week"),
        ("* * * * mox", "day of week"),
        ("5-1 * * * *", "minute"),
        ("*/0 * * * *", "minute"),
        ("a * * * *", "minute"),
        ("1,,2 * * * *", "minute"),
        ("@reboot", "@reboot"),
        ("@every 5m", "@every"),
    ],
)
def test_an_invalid_expression_names_the_field_and_the_reason(expression: str, needle: str) -> None:
    with pytest.raises(ValueError, match=needle):
        validate_cron_syntax(expression)


@pytest.mark.parametrize(
    "expression",
    ["0 0 * * sat-sun", "0 0 * * fri-sun", "0 0 * * 5-0", "0 0 * * 5-7", "@Daily", "@HOURLY"],
)
def test_a_weekday_range_may_end_on_sunday_and_macros_ignore_case(expression: str) -> None:
    validate_cron_syntax(expression)


@pytest.mark.parametrize(
    ("expression", "hint"),
    [
        ("60 * * * *", "use 0 to 59"),
        ("* 0-24 * * *", "use 0 to 23"),
        ("* * 0 * *", "use 1 to 31"),
    ],
)
def test_a_value_out_of_range_says_the_valid_range(expression: str, hint: str) -> None:
    with pytest.raises(ValueError, match=hint):
        validate_cron_syntax(expression)


def test_a_backwards_range_outside_weekdays_still_fails() -> None:
    with pytest.raises(ValueError, match="backwards"):
        validate_cron_syntax("5-1 * * * *")
    with pytest.raises(ValueError, match="backwards"):
        validate_cron_syntax("* * * * 3-1")


def test_a_field_error_quotes_the_expression_as_written() -> None:
    with pytest.raises(ValueError, match=r"month field 'JANX'"):
        validate_cron_syntax("0 0 1 JANX *")
    with pytest.raises(ValueError, match=r"'@REBOOT'"):
        validate_cron_syntax("@REBOOT")


def test_a_range_of_sunday_alone_is_sunday_only() -> None:
    validate_cron_syntax("0 0 * * 0-0")
    validate_cron_syntax("0 0 * * sun-sun")


def test_a_daily_job_stays_at_local_six_across_both_changes() -> None:
    """04:00 UTC in summer, 05:00 UTC in winter, in the days around each change."""
    spring = fires("0 6 * * *", "Europe/Berlin", utc(2026, 3, 27), 5)
    assert [(f.hour, local(f)[-5:]) for f in spring] == [
        (5, "06:00"),
        (5, "06:00"),
        (4, "06:00"),
        (4, "06:00"),
        (4, "06:00"),
    ]
    assert [f.day for f in spring] == [27, 28, 29, 30, 31]
    autumn = fires("0 6 * * *", "Europe/Berlin", utc(2026, 10, 23), 5)
    assert [(f.hour, local(f)[-5:]) for f in autumn] == [
        (4, "06:00"),
        (4, "06:00"),
        (5, "06:00"),
        (5, "06:00"),
        (5, "06:00"),
    ]
    assert all(f.tzinfo is not None and f.utcoffset() == timedelta(0) for f in spring + autumn)


def test_a_wall_time_that_does_not_exist_fires_at_the_first_valid_moment_after_it() -> None:
    found = fires("30 2 * * *", "Europe/Berlin", utc(2026, 3, 27, 12), 3)
    assert [local(f) for f in found] == ["2026-03-28 02:30", "2026-03-29 03:00", "2026-03-30 02:30"]


def test_two_wall_times_in_a_gap_fire_once() -> None:
    found = fires("0,30 2,3 * * *", "Europe/Berlin", utc(2026, 3, 28, 12), 4)
    assert [local(f) for f in found] == [
        "2026-03-29 03:00",
        "2026-03-29 03:30",
        "2026-03-30 02:00",
        "2026-03-30 02:30",
    ]


def test_a_wall_time_that_happens_twice_fires_once_at_the_first() -> None:
    found = fires("30 2 * * *", "Europe/Berlin", utc(2026, 10, 24), 3)
    assert [f.isoformat() for f in found[1:2]] == ["2026-10-25T00:30:00+00:00"]
    assert [local(f) for f in found] == ["2026-10-24 02:30", "2026-10-25 02:30", "2026-10-26 02:30"]
    # asking from the first occurrence, the second 02:30 (01:30 UTC) is not a fire
    assert next_fire_after("30 2 * * *", "Europe/Berlin", utc(2026, 10, 25, 0, 30)) == utc(
        2026, 10, 26, 1, 30
    )


def _walk(expr: str, tz: str, start: datetime, end: datetime) -> list[datetime]:
    # every fire in (start, end], one by one through the evaluator's own walk
    zone = ZoneInfo(tz)
    out: list[datetime] = []
    for instant in _fires_after(_parse(expr), zone, start):
        if instant > end:
            break
        out.append(instant)
    return out


def _window(expr: str, start: datetime, end: datetime) -> list[datetime]:
    return _walk(expr, "Europe/Berlin", start, end)


FALL_BACK = (
    utc(2026, 10, 24, 21, 59, 59),
    utc(2026, 10, 25, 22, 59),
)  # local day 25 Oct 00:00-23:59
SPRING = (utc(2026, 3, 28, 22, 59, 59), utc(2026, 3, 29, 21, 59))  # local day 29 Mar


def test_an_hourly_job_fires_in_every_real_hour_on_the_fall_back_day() -> None:
    found = _window("15 * * * *", *FALL_BACK)
    assert len(found) == 25
    assert len(set(found)) == 25
    assert [local(f)[-5:] for f in found].count("02:15") == 2
    assert utc(2026, 10, 25, 1, 15) in found  # the second 02:15


def test_an_hourly_job_fires_in_every_real_hour_on_the_spring_day() -> None:
    found = _window("15 * * * *", *SPRING)
    assert len(found) == 23
    assert [local(f)[-5:] for f in found].count("03:15") == 1
    assert "02:15" not in [local(f)[-5:] for f in found]
    assert utc(2026, 3, 29, 1, 0) not in found  # nothing is shifted out of the gap


def test_a_minutely_job_has_no_silent_hour_on_the_fall_back_day() -> None:
    found = _window("* * * * *", *FALL_BACK)
    assert len(found) == 25 * 60
    assert utc(2026, 10, 25, 1, 30) in found
    assert len(_window("* * * * *", *SPRING)) == 23 * 60


def test_a_step_over_star_in_the_hour_field_follows_real_time() -> None:
    found = _window("0 */2 * * *", *FALL_BACK)
    # local hours 0, 2, 4...: the repeated 02:00 happens twice, and so does nothing else
    assert [local(f)[-5:] for f in found].count("02:00") == 2
    assert len(found) == 13


def test_a_fixed_hour_keeps_following_the_wall_clock_on_both_days() -> None:
    assert len(_window("30 2 * * *", *FALL_BACK)) == 1
    assert [local(f) for f in _window("30 2 * * *", *SPRING)] == ["2026-03-29 03:00"]
    assert len(_window("0,30 2,3 * * *", *FALL_BACK)) == 4


def test_the_hourly_macro_follows_real_time_and_the_daily_macro_the_wall_clock() -> None:
    assert len(_window("@hourly", *FALL_BACK)) == 25
    assert len(_window("@daily", utc(2026, 10, 24, 12), utc(2026, 10, 26, 12))) == 2


@pytest.mark.parametrize(
    "expr", ["* * * * *", "*/7 * * * *", "15 * * * *", "30 2 * * *", "0 */3 * * mon-fri"]
)
@pytest.mark.parametrize(
    ("tz", "start", "end"),
    [
        ("Europe/Berlin", utc(2026, 10, 22, 7, 3), utc(2026, 10, 28, 5, 40)),
        ("Europe/Berlin", utc(2026, 3, 27, 1), utc(2026, 4, 1, 23, 59)),
        ("America/New_York", utc(2026, 11, 1, 0), utc(2026, 11, 3, 0)),
        ("Australia/Lord_Howe", utc(2026, 10, 3, 0), utc(2026, 10, 5, 0)),
    ],
)
def test_the_count_of_a_window_equals_a_one_by_one_count(
    expr: str, tz: str, start: datetime, end: datetime
) -> None:
    window = fire_times_between(expr, tz, start, end, limit=7)
    brute = _walk(expr, tz, start, end)
    assert window.count == len(brute)
    assert window.times == brute[-7:]


def test_a_year_of_minutes_is_counted_quickly_and_exactly() -> None:
    import time

    started = time.monotonic()
    window = fire_times_between(
        "* * * * *", "Europe/Berlin", utc(2025, 12, 31, 23, 0), utc(2026, 12, 31, 23, 0)
    )
    assert time.monotonic() - started < 10.0
    # real time: 365 days of 1440 minutes, whatever the clock did
    assert window.count == 365 * 24 * 60
    assert len(window.times) == 100
    assert window.times[-1] == utc(2026, 12, 31, 23, 0)
    assert window.times == [
        utc(2026, 12, 31, 23, 0) - timedelta(minutes=99 - n) for n in range(100)
    ]


def test_the_day_fields_decide_which_whole_days_are_counted() -> None:
    mondays_or_13th = fire_times_between(
        "0 0 13 * mon", "UTC", utc(2026, 1, 31, 23), utc(2026, 2, 28, 23)
    )
    assert mondays_or_13th.count == 5  # Mondays 2, 9, 16, 23 and the 13th
    odd_mondays = fire_times_between(
        "0 0 */2 * mon", "UTC", utc(2026, 1, 31, 23), utc(2026, 2, 28, 23)
    )
    assert odd_mondays.count == 2


def test_a_search_goes_on_until_it_finds_a_fire() -> None:
    # Mondays that are the 1st or the 31st of February: 2027-02-01, then 2038-02-01
    assert next_fire_after("0 0 */30 2 mon", "UTC", utc(2027, 2, 2)) == utc(2038, 2, 1)
    assert next_fire_after("0 0 29 2 *", "UTC", utc(2096, 3, 1)) == utc(2104, 2, 29)


@pytest.mark.parametrize(
    "expression",
    ["0 0 31 2 *", "0 0 30 2 *", "0 0 31 4,6,9,11 *", "0 0 30,31 feb *"],
)
def test_an_expression_that_names_no_existing_day_is_refused(expression: str) -> None:
    validate_cron_syntax(expression)
    with pytest.raises(ValueError, match="never fires"):
        validate_cron_can_fire(expression)


@pytest.mark.parametrize(
    "expression",
    ["0 0 29 2 *", "0 0 31 2 mon", "0 0 31 * *", "0 0 */30 2 mon", "0 0 30 2 mon,tue", "@yearly"],
)
def test_an_expression_that_can_fire_is_accepted(expression: str) -> None:
    validate_cron_can_fire(expression)


def test_the_refusal_names_the_fields_to_change() -> None:
    with pytest.raises(ValueError, match=r"day-of-month field.*month field") as info:
        validate_cron_can_fire("0 0 31 2 *")
    assert "'0 0 31 2 *'" in str(info.value)
    assert "31" in str(info.value)


def test_a_job_every_hour_in_utc_fires_at_quarter_past() -> None:
    found = fires("15 * * * *", "UTC", utc(2026, 1, 1, 0, 15), 3)
    assert found == [utc(2026, 1, 1, 1, 15), utc(2026, 1, 1, 2, 15), utc(2026, 1, 1, 3, 15)]


def test_the_time_after_is_exclusive() -> None:
    assert next_fire_after("15 * * * *", "UTC", utc(2026, 1, 1, 0, 14, 59)) == utc(
        2026, 1, 1, 0, 15
    )
    assert next_fire_after("15 * * * *", "UTC", utc(2026, 1, 1, 0, 15)) == utc(2026, 1, 1, 1, 15)


def test_day_of_month_and_day_of_week_either_matches_when_both_are_restricted() -> None:
    # the 13th, or any Friday: 2026-02-13 is a Friday, 2026-02-06 and 02-20 are Fridays too
    found = fires("0 0 13 * fri", "UTC", utc(2026, 2, 1), 4)
    assert [f.day for f in found] == [6, 13, 20, 27]


def test_a_star_in_one_of_the_two_day_fields_means_the_other_alone() -> None:
    mondays = fires("0 0 * * mon", "UTC", utc(2026, 2, 1), 2)
    assert [f.day for f in mondays] == [2, 9]
    the_first = fires("0 0 1 * *", "UTC", utc(2026, 2, 1), 2)
    assert [(f.month, f.day) for f in the_first] == [(3, 1), (4, 1)]
    odd_mondays = fires("0 0 */2 * mon", "UTC", utc(2026, 2, 1), 2)
    assert [f.day for f in odd_mondays] == [9, 23]


@pytest.mark.parametrize(
    ("expr", "expected_days"),
    [
        ("0 0 * * SAT-SUN", [3, 4, 10]),
        ("0 0 * * Sat-Sun", [3, 4, 10]),
        ("0 0 * * fri-sun", [2, 3, 4]),
        ("0 0 * * 5-0", [2, 3, 4]),
        ("0 0 * * 5-7", [2, 3, 4]),
        ("0 0 * * 7", [4, 11, 18]),
        ("0 0 * * 0", [4, 11, 18]),
        ("0 0 * * sun-sun", [4, 11, 18]),
        ("0 0 * * 0-0", [4, 11, 18]),
        ("0 0 * * 7-7", [4, 11, 18]),
    ],
)
def test_weekday_spellings_the_syntax_check_accepts_mean_the_same_days(
    expr: str, expected_days: list[int]
) -> None:
    """January 2026: the 2nd is a Friday, the 3rd a Saturday, the 4th a Sunday."""
    validate_cron_syntax(expr)
    found = fires(expr, "UTC", utc(2026, 1, 1), 3)
    assert [f.day for f in found] == expected_days


def test_names_in_any_case_work_in_months() -> None:
    found = fires("0 0 1 JUL,Jan *", "UTC", utc(2026, 1, 1), 2)
    assert [(f.month, f.day) for f in found] == [(7, 1), (1, 1)]


def test_a_value_with_a_step_runs_to_the_end_of_the_range() -> None:
    validate_cron_syntax("5/20 * * * *")
    found = fires("5/20 * * * *", "UTC", utc(2026, 1, 1), 4)
    assert [f.minute for f in found] == [5, 25, 45, 5]


def test_a_step_larger_than_the_range_keeps_only_the_first_value() -> None:
    validate_cron_syntax("*/100 * * * *")
    found = fires("*/100 * * * *", "UTC", utc(2026, 1, 1, 0, 1), 2)
    assert found == [utc(2026, 1, 1, 1, 0), utc(2026, 1, 1, 2, 0)]


def test_the_macros_mean_their_fields() -> None:
    assert next_fire_after("@hourly", "UTC", utc(2026, 1, 1, 0, 5)) == utc(2026, 1, 1, 1)
    assert next_fire_after("@Daily", "UTC", utc(2026, 1, 1, 0, 5)) == utc(2026, 1, 2)
    assert next_fire_after("@weekly", "UTC", utc(2026, 1, 1)) == utc(2026, 1, 4)
    assert next_fire_after("@monthly", "UTC", utc(2026, 1, 1)) == utc(2026, 2, 1)
    assert next_fire_after("@yearly", "UTC", utc(2026, 1, 1)) == utc(2027, 1, 1)


def test_the_29th_of_february_is_found_across_the_century_gap() -> None:
    assert next_fire_after("0 0 29 2 *", "UTC", utc(2096, 3, 1)) == utc(2104, 2, 29)


def test_the_evaluator_accepts_what_the_syntax_check_accepts() -> None:
    for expr in ["* * * * *", "*/5 0-6 1,15 * mon-fri", "10-50/10 * * * *", "30 2 * * 7"]:
        validate_cron_syntax(expr)
        assert next_fire_after(expr, "UTC", utc(2026, 1, 1)) is not None


def test_a_naive_datetime_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        next_fire_after("* * * * *", "UTC", datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="timezone-aware"):
        fire_times_between("* * * * *", "UTC", datetime(2026, 1, 1), utc(2026, 1, 2))
    with pytest.raises(ValueError, match="timezone-aware"):
        fire_times_between("* * * * *", "UTC", utc(2026, 1, 1), datetime(2026, 1, 2))


def test_an_invalid_expression_or_zone_is_refused() -> None:
    with pytest.raises(ValueError, match="5 fields"):
        next_fire_after("* * * *", "UTC", utc(2026, 1, 1))
    with pytest.raises(ValueError, match="time zone"):
        next_fire_after("* * * * *", "Nowhere/Land", utc(2026, 1, 1))


def test_an_input_in_another_offset_gives_utc() -> None:
    plus_two = datetime(2026, 1, 1, 2, 0, tzinfo=timezone(timedelta(hours=2)))
    assert next_fire_after("0 * * * *", "UTC", plus_two) == utc(2026, 1, 1, 1)


def test_a_three_day_outage_returns_the_latest_up_to_the_cap_and_the_true_count() -> None:
    window = fire_times_between(
        "0 * * * *", "UTC", utc(2026, 1, 1, 0), utc(2026, 1, 4, 0), limit=10
    )
    assert window.count == 72
    assert len(window.times) == 10
    assert window.times[-1] == utc(2026, 1, 4, 0)
    assert window.times[0] == utc(2026, 1, 3, 15)
    assert window.times == sorted(window.times)


def test_a_window_under_the_cap_returns_all_of_it() -> None:
    window = fire_times_between("0 * * * *", "UTC", utc(2026, 1, 1, 0), utc(2026, 1, 1, 3))
    assert window.times == [utc(2026, 1, 1, 1), utc(2026, 1, 1, 2), utc(2026, 1, 1, 3)]
    assert window.count == 3


def test_an_empty_or_backwards_window_has_no_fires() -> None:
    assert fire_times_between("* * * * *", "UTC", utc(2026, 1, 2), utc(2026, 1, 1)).count == 0


def test_a_search_for_a_fire_that_does_not_exist_stops_at_the_safety_cap() -> None:
    # a deploy refuses this expression; the evaluator still must not loop on it
    assert next_fire_after("0 0 31 2 *", "UTC", utc(2026, 1, 1)) is None


def test_the_longest_real_wait_is_found() -> None:
    # a 29 February that is a Sunday: the weekday cycle breaks across the year 2100
    assert next_fire_after("0 0 29 2 */7", "UTC", utc(2089, 1, 1)) == utc(2128, 2, 29)
