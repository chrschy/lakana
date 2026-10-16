"""Tests for the cross-backend timestamp type in lakana_core.models.base.

These exercise :class:`~lakana_core.models.base.UTCDateTime` directly, rather
than through a full ORM round-trip, because SQLite always returns a naive
value on read: that path is already covered by
``test_models.test_timestamps_stay_timezone_aware_after_reload``. The
"already aware" branch of :meth:`~lakana_core.models.base.UTCDateTime.process_result_value`
only ever fires against Postgres, which normalises the same real bug this
type exists to fix, so it is verified here at the unit level instead.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy.engine import default

from lakana_core.models.base import UTCDateTime

_DIALECT = default.DefaultDialect()
"""A concrete dialect instance; process_bind_param/process_result_value never
inspect it, but the method signatures require one."""


def test_process_bind_param_passes_none_through() -> None:
    """Binding ``None`` writes ``None``, for nullable timestamp columns."""
    assert UTCDateTime().process_bind_param(None, _DIALECT) is None


def test_process_bind_param_converts_a_non_utc_value_to_utc() -> None:
    """An aware value in another zone is converted to UTC before writing."""
    plus_two = timezone(timedelta(hours=2))
    value = datetime(2026, 1, 1, 12, 0, tzinfo=plus_two)
    bound = UTCDateTime().process_bind_param(value, _DIALECT)
    assert bound == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)


def test_process_bind_param_rejects_a_naive_value() -> None:
    """A naive value has no defined offset and cannot be safely converted."""
    with pytest.raises(ValueError, match="timezone-aware"):
        UTCDateTime().process_bind_param(datetime(2026, 1, 1, 12, 0), _DIALECT)


def test_process_result_value_passes_none_through() -> None:
    """Reading ``None`` back yields ``None``, for nullable timestamp columns."""
    assert UTCDateTime().process_result_value(None, _DIALECT) is None


def test_process_result_value_attaches_utc_to_a_naive_value() -> None:
    """SQLite returns a naive value; UTC is reattached rather than guessed."""
    value = datetime(2026, 1, 1, 12, 0)
    result = UTCDateTime().process_result_value(value, _DIALECT)
    assert result == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_process_result_value_normalises_an_already_aware_value() -> None:
    """Postgres returns an aware value already; it is normalised to UTC."""
    plus_two = timezone(timedelta(hours=2))
    value = datetime(2026, 1, 1, 12, 0, tzinfo=plus_two)
    result = UTCDateTime().process_result_value(value, _DIALECT)
    assert result == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
