"""Cron expressions: their syntax, and when they fire in a time zone.

An expression has the five standard fields (or one of five ``@`` shorthands), nothing else. The
syntax check lets a deploy refuse a bad one with the field and the reason; the evaluator gives the
instants a schedule fires. Both read the fields through one parser, so what a deploy accepts is
exactly what the scheduler can evaluate. Neither ``croniter`` nor ``cronsim`` is used: they
disagree with this syntax on ranges that end on Sunday (``sat-sun``, ``5-0``) and on a day that
exists in no month (``31 2``), and their daylight saving time rules are not the ones Lakana
documents.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lakana_core.jobs.tz import load_zone

MACROS: Final[frozenset[str]] = frozenset({"@hourly", "@daily", "@weekly", "@monthly", "@yearly"})
"""The shorthands that are accepted in place of the five fields."""

_MONTHS: Final[dict[str, int]] = {
    name: number
    for number, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
        start=1,
    )
}
_WEEKDAYS: Final[dict[str, int]] = {
    name: number for number, name in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))
}

_MACRO_FIELDS: Final[dict[str, str]] = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
}

SEARCH_YEARS: Final = 400
"""How far ahead `next_fire_after` looks, as a safety stop. An expression that can fire at all
(`validate_cron_can_fire`) does so within this span: the longest wait of a real schedule is
``0 0 29 2 */7`` (a 29 February that is a Sunday) across 2100, 40 years; 400 years is the cycle of
the calendar, after which every weekday and date combination has come round."""

_DAYS_IN_MONTH: Final[dict[int, int]] = {
    1: 31,
    2: 29,
    3: 31,
    4: 30,
    5: 31,
    6: 30,
    7: 31,
    8: 31,
    9: 30,
    10: 31,
    11: 30,
    12: 31,
}
"""The most days each month has; February counts 29 because leap years exist."""
DEFAULT_FIRE_LIMIT: Final = 100
"""How many fire times `fire_times_between` keeps by default."""

# (name, lowest, highest, names a value may be written as)
_FIELDS: Final[tuple[tuple[str, int, int, dict[str, int]], ...]] = (
    ("minute", 0, 59, {}),
    ("hour", 0, 23, {}),
    ("day of month", 1, 31, {}),
    ("month", 1, 12, _MONTHS),
    ("day of week", 0, 7, _WEEKDAYS),
)


def _value(text: str, low: int, high: int, names: dict[str, int]) -> int:
    """Read one number or name of a field.

    Parameters
    ----------
    text
        The text between separators.
    low
        Smallest allowed number.
    high
        Largest allowed number.
    names
        Names the field accepts, lower case.

    Returns
    -------
    int
        The number.

    Raises
    ------
    ValueError
        If it is neither a known name nor a whole number from `low` to `high`.
    """
    if text.lower() in names:
        return names[text.lower()]
    if not (text.isascii() and text.isdigit()):
        raise ValueError(f"{text!r} is not a number; use {low} to {high}")
    number = int(text)
    if not low <= number <= high:
        raise ValueError(f"{text!r} is out of range; use {low} to {high}")
    return number


def _expand_field(text: str, low: int, high: int, names: dict[str, int]) -> frozenset[int]:
    """Read one field: a comma list of ``*``, a value or a range, each with an optional step.

    A value with a step (``5/20``) runs to the end of the field's range, as in Vixie cron. In the
    day-of-week field (highest 7), a range that starts above Sunday may end on 0 or ``sun``, which
    then means 7 (``fri-sun``); ``0-0`` and ``sun-sun`` are Sunday alone. The result is not folded
    (7 stays 7); the caller maps it.

    Parameters
    ----------
    text
        The field, as the user wrote it.
    low
        Smallest allowed number.
    high
        Largest allowed number.
    names
        Names the field accepts, lower case.

    Returns
    -------
    frozenset of int
        Every number the field selects.

    Raises
    ------
    ValueError
        With the reason, if the field is malformed.
    """
    selected: set[int] = set()
    for part in text.split(","):
        if not part:
            raise ValueError("it has an empty entry in a comma list")
        base, slash, step_text = part.partition("/")
        step = 1
        if slash:
            if not (step_text.isascii() and step_text.isdigit() and int(step_text) > 0):
                raise ValueError(f"the step {step_text!r} must be a whole number above 0")
            step = int(step_text)
        if base == "*":
            first, last = low, high
        else:
            start, dash, end = base.partition("-")
            first = _value(start, low, high, names)
            if dash:
                last = _value(end, low, high, names)
                if last == 0 and high == 7 and first > 0:
                    last = 7
                if last < first:
                    raise ValueError(f"the range {base!r} runs backwards")
            else:
                last = high if slash else first
        selected.update(range(first, last + 1, step))
    return frozenset(selected)


def validate_cron_syntax(expression: str) -> None:
    """Check that an expression has the five standard fields, each in range.

    Parameters
    ----------
    expression
        A cron expression: minute, hour, day of month, month and day of week, separated by
        blanks, or one of ``@hourly``, ``@daily``, ``@weekly``, ``@monthly``, ``@yearly``.
        A field is ``*``, a number, a range ``a-b`` or a comma list of these, each with an
        optional step ``/n``. Months and weekdays may be three-letter names, in any case; 0 and 7
        are both Sunday, and a weekday range may end on Sunday (``fri-sun``).

    Raises
    ------
    ValueError
        Naming the field and the reason, and the standard form when the count is wrong. Seconds
        are refused: a schedule fires at most once a minute.
    """
    _parse(expression)


def _parse(expression: str) -> _Schedule:
    """Parse an expression into the sets of values its fields select.

    Parameters
    ----------
    expression
        As for `validate_cron_syntax`.

    Returns
    -------
    _Schedule
        The selected values of each field.

    Raises
    ------
    ValueError
        As `validate_cron_syntax`.
    """
    text = expression.strip()
    if text.startswith("@"):
        if text.lower() not in MACROS:
            raise ValueError(
                f"{text!r} is not a shorthand Lakana knows; use one of "
                f"{', '.join(sorted(MACROS))} or write the five fields"
            )
        return _parse(_MACRO_FIELDS[text.lower()])
    fields = text.split()
    if len(fields) != len(_FIELDS):
        seconds = (
            " A cron here has no seconds field: a schedule fires at most once a minute."
            if len(fields) == 6
            else ""
        )
        raise ValueError(
            f"a cron expression has 5 fields (minute hour day-of-month month day-of-week), "
            f"got {len(fields)}.{seconds} Example: '0 6 * * *' is every day at 06:00"
        )
    values: list[frozenset[int]] = []
    for field, (name, low, high, names) in zip(fields, _FIELDS, strict=True):
        try:
            values.append(_expand_field(field, low, high, names))
        except ValueError as exc:
            raise ValueError(f"{name} field {field!r}: {exc}") from exc
    minutes, hours, days, months, weekdays = values
    return _Schedule(
        minutes=tuple(sorted(minutes)),
        hours=tuple(sorted(hours)),
        days=days,
        months=months,
        weekdays=frozenset(day % 7 for day in weekdays),
        any_day=fields[2].startswith("*"),
        any_weekday=fields[4].startswith("*"),
        real_time=fields[1].startswith("*"),
    )


@dataclass(frozen=True)
class _Schedule:
    """The values each field of an expression selects.

    Parameters
    ----------
    minutes
        Minutes of the hour, ascending.
    hours
        Hours of the day, ascending.
    days
        Days of the month.
    months
        Months, 1 to 12.
    weekdays
        Days of the week, 0 (Sunday) to 6.
    any_day
        Whether the day-of-month field starts with ``*``.
    any_weekday
        Whether the day-of-week field starts with ``*``.
    real_time
        Whether the hour field starts with ``*`` (a star or a step over it). Such a schedule
        follows real time across a change of clock; one with fixed hours follows the wall clock.
    """

    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    any_day: bool
    any_weekday: bool
    real_time: bool

    def matches_date(self, day: date) -> bool:
        """Say whether a calendar date is one the schedule fires on.

        As in standard cron, when both day fields are restricted a date matches if either
        does; when one starts with ``*``, only the other counts.

        Parameters
        ----------
        day
            The date.

        Returns
        -------
        bool
            True if the month matches and the day fields allow the date.
        """
        if day.month not in self.months:
            return False
        in_month = day.day in self.days
        in_week = (day.isoweekday() % 7) in self.weekdays
        if self.any_day or self.any_weekday:
            return in_month and in_week
        return in_month or in_week


@dataclass(frozen=True)
class FireWindow:
    """The fire times in a span, kept up to a limit, with their true number.

    Parameters
    ----------
    times
        The latest fire times of the span, ascending, aware UTC; at most the limit asked for.
    count
        How many times the schedule fired in the span, also those not kept.
    """

    times: list[datetime]
    count: int


def _require_aware(value: datetime, what: str) -> datetime:
    """Convert an aware datetime to UTC and refuse a naive one.

    Parameters
    ----------
    value
        The datetime.
    what
        Its role, for the error message.

    Returns
    -------
    datetime
        `value` in UTC.

    Raises
    ------
    ValueError
        If `value` has no time zone.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{what} must be timezone-aware; got the naive {value.isoformat()}")
    return value.astimezone(UTC)


def _zone(name: str) -> ZoneInfo:
    """Load a zone, turning a missing one into a `ValueError`.

    Parameters
    ----------
    name
        An IANA zone name.

    Returns
    -------
    ZoneInfo
        The zone.

    Raises
    ------
    ValueError
        If `tzdata` does not know it.
    """
    try:
        return load_zone(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(
            f"'{name}' is not a time zone this server knows. Use an IANA name such as "
            "'Europe/Berlin' or 'UTC'"
        ) from exc


def _wall_to_utc(wall: datetime, zone: ZoneInfo) -> datetime:
    """Turn a wall-clock time into the instant a job set for it fires.

    A time that happens twice maps to its first occurrence. A time that does not exist (the
    clock skipped it) maps to the first instant at which the wall clock reads that time or later,
    which is the moment the clock jumps.

    Parameters
    ----------
    wall
        A naive wall-clock time, whole minutes.
    zone
        The zone it is read in.

    Returns
    -------
    datetime
        The instant, aware UTC.
    """
    instant = wall.replace(tzinfo=zone, fold=0).astimezone(UTC)
    if instant.astimezone(zone).replace(tzinfo=None) == wall:
        return instant
    step = timedelta(minutes=1)
    while (instant - step).astimezone(zone).replace(tzinfo=None) >= wall:
        instant -= step
    return instant


def _real_instants(wall: datetime, zone: ZoneInfo) -> set[datetime]:
    """List every real instant at which the wall clock reads a given time.

    Parameters
    ----------
    wall
        A naive wall-clock time.
    zone
        The zone it is read in.

    Returns
    -------
    set of datetime
        Two instants for a time that happens twice, none for one the clock skipped, otherwise
        one; aware UTC.
    """
    found: set[datetime] = set()
    for fold in (0, 1):
        instant = wall.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        if instant.astimezone(zone).replace(tzinfo=None) == wall:
            found.add(instant)
    return found


def _day_fires(schedule: _Schedule, zone: ZoneInfo, day: date) -> list[datetime]:
    """List the instants a schedule fires for one local calendar day.

    Fixed hours follow the wall clock: a time the clock skipped fires at the jump, and one that
    happens twice fires once, at its first occurrence. A star in the hour field follows real
    time: both occurrences of a repeated time fire, and a skipped one is simply absent.

    Parameters
    ----------
    schedule
        The parsed expression.
    zone
        The zone the fields are read in.
    day
        The local date.

    Returns
    -------
    list of datetime
        The fires, ascending, aware UTC; empty if the day does not match.
    """
    if not schedule.matches_date(day):
        return []
    found: set[datetime] = set()
    for hour in schedule.hours:
        for minute in schedule.minutes:
            wall = datetime(day.year, day.month, day.day, hour, minute)
            if schedule.real_time:
                found |= _real_instants(wall, zone)
            else:
                found.add(_wall_to_utc(wall, zone))
    return sorted(found)


def _plain_day(day: date, zone: ZoneInfo) -> tuple[datetime, datetime] | None:
    """Give the first and last minute of a local day on which the clock does not change.

    Parameters
    ----------
    day
        The local date.
    zone
        The zone.

    Returns
    -------
    tuple of datetime or None
        The instants of 00:00 and 23:59, aware UTC; `None` if the offset differs between them.
    """
    start = datetime(day.year, day.month, day.day, tzinfo=zone)
    end = start.replace(hour=23, minute=59, fold=1)
    if start.utcoffset() != end.utcoffset():
        return None
    return start.astimezone(UTC), end.astimezone(UTC)


def _fires_after(schedule: _Schedule, zone: ZoneInfo, after: datetime) -> Iterator[datetime]:
    """Walk the instants a schedule fires, ascending, from just after a moment.

    Stops after `SEARCH_YEARS` years.

    Parameters
    ----------
    schedule
        The parsed expression.
    zone
        The zone the fields are read in.
    after
        Aware UTC; only later instants are yielded.

    Yields
    ------
    datetime
        Each fire, aware UTC, strictly later than the one before.
    """
    day = after.astimezone(zone).date() - timedelta(days=1)
    last_day = date(day.year + SEARCH_YEARS, day.month, min(day.day, 28))
    previous = after
    while day <= last_day:
        for instant in _day_fires(schedule, zone, day):
            if instant > previous:
                previous = instant
                yield instant
        day += timedelta(days=1)


def validate_cron_can_fire(expression: str) -> None:
    """Refuse an expression that names a day no month has.

    When both day fields are restricted, standard cron fires if either matches, and every month
    has every weekday, so it always fires. Otherwise only the day-of-month field decides, and
    ``0 0 31 2 *`` would never fire. Whether the day-of-week field then lets a date through does
    not matter: every date falls on every weekday within 400 years.

    Parameters
    ----------
    expression
        A cron expression that passed `validate_cron_syntax`.

    Raises
    ------
    ValueError
        Naming the fields to change, if no (month, day) pair of the fields exists.
    """
    schedule = _parse(expression)
    if not (schedule.any_day or schedule.any_weekday):
        return
    if any(day <= _DAYS_IN_MONTH[month] for month in schedule.months for day in schedule.days):
        return
    days = ", ".join(str(day) for day in sorted(schedule.days))
    raise ValueError(
        f"cron {expression.strip()!r} never fires: the day-of-month field allows only day {days}, "
        "and no month in the month field has such a day. Change the day-of-month field or the "
        "month field"
    )


def next_fire_after(expression: str, tz: str, after: datetime) -> datetime | None:
    """Find the next time an expression fires, later than a moment.

    Fields are read in the zone. A wall-clock time that does not exist on a day (02:30 when the
    clock jumps from 02:00 to 03:00) fires at the first valid moment after it; one that happens
    twice fires once, at its first occurrence.

    Parameters
    ----------
    expression
        A cron expression, as for `validate_cron_syntax`.
    tz
        The IANA zone the expression is read in.
    after
        An aware moment; the answer is strictly later.

    Returns
    -------
    datetime or None
        The next fire, aware UTC. `None` only if there is none within `SEARCH_YEARS` years,
        which `validate_cron_can_fire` rules out for every expression a deploy accepts.

    Raises
    ------
    ValueError
        If `after` is naive, the expression is malformed or the zone is unknown.
    """
    moment = _require_aware(after, "the moment to search from")
    schedule = _parse(expression)
    return next(_fires_after(schedule, _zone(tz), moment), None)


def fire_times_between(
    expression: str,
    tz: str,
    start: datetime,
    end: datetime,
    *,
    limit: int = DEFAULT_FIRE_LIMIT,
) -> FireWindow:
    """List the times an expression fired after `start`, up to and including `end`.

    A long outage must not build a huge list or take long to count, so only the latest `limit`
    times are kept and the count is made per day: a whole day without a change of clock adds the
    number of times its fields name in one step, and only the days at the edges of the window and
    the days with a change of clock are walked.

    Parameters
    ----------
    expression
        A cron expression, as for `validate_cron_syntax`.
    tz
        The IANA zone the expression is read in.
    start
        An aware moment, excluded.
    end
        An aware moment, included.
    limit
        The most times to keep.

    Returns
    -------
    FireWindow
        The latest fires and the true count; empty if `end` is not after `start`.

    Raises
    ------
    ValueError
        If a moment is naive, the expression is malformed or the zone is unknown.
    """
    first = _require_aware(start, "the start of the window")
    last = _require_aware(end, "the end of the window")
    schedule = _parse(expression)
    zone = _zone(tz)
    if last <= first:
        return FireWindow(times=[], count=0)
    first_day = first.astimezone(zone).date() - timedelta(days=1)
    last_day = last.astimezone(zone).date() + timedelta(days=1)
    per_plain_day = len(schedule.hours) * len(schedule.minutes)
    count = 0
    day = first_day
    while day <= last_day:
        if schedule.matches_date(day):
            bounds = _plain_day(day, zone)
            if bounds is not None and bounds[0] > first and bounds[1] <= last:
                count += per_plain_day
            else:
                count += sum(1 for i in _day_fires(schedule, zone, day) if first < i <= last)
        day += timedelta(days=1)
    tail: list[datetime] = []
    day = last_day
    while day >= first_day and len(tail) < limit:
        tail = [i for i in _day_fires(schedule, zone, day) if first < i <= last] + tail
        day -= timedelta(days=1)
    return FireWindow(times=tail[max(len(tail) - limit, 0) :] if limit > 0 else [], count=count)
