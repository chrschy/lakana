"""Tests for `DenialAuditLimiter`, the per-principal bound on AUTHORIZATION_DENIED rows."""

from lakana_core.audit.denial_limiter import DenialAuditLimiter


class _FakeClock:
    """A clock tests move by hand.

    Parameters
    ----------
    start
        The time before any `advance`.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def advance(self, seconds: float) -> None:
        """Move the clock forward.

        Parameters
        ----------
        seconds
            How far.
        """
        self._now += seconds

    def __call__(self) -> float:
        """Return the current time.

        Returns
        -------
        float
            The fake time.
        """
        return self._now


def test_one_principal_is_admitted_up_to_the_rate_then_refused() -> None:
    """Past the rate, a repeated denial of one action is not recorded."""
    limiter = DenialAuditLimiter(per_minute=3, clock=_FakeClock())
    results = [limiter.admit("p1", "READ_TABLE") for _ in range(5)]
    assert results == [(True, 0), (True, 0), (True, 0), (False, 0), (False, 0)]


def test_the_count_dropped_is_handed_to_the_next_admitted_denial() -> None:
    """After a refill the next recorded denial says how many were not recorded."""
    clock = _FakeClock()
    limiter = DenialAuditLimiter(per_minute=3, clock=clock)
    for _ in range(5):
        limiter.admit("p1", "READ_TABLE")
    clock.advance(20)
    assert limiter.admit("p1", "READ_TABLE") == (True, 2)
    assert limiter.admit("p1", "READ_TABLE") == (False, 0)


def test_the_first_denial_of_each_action_is_always_admitted() -> None:
    """A principal that used its rate on one action still has its first denial of another kept."""
    limiter = DenialAuditLimiter(per_minute=1, clock=_FakeClock())
    assert limiter.admit("p1", "READ_TABLE") == (True, 0)
    assert limiter.admit("p1", "READ_TABLE") == (False, 0)
    assert limiter.admit("p1", "MANAGE_GRANT") == (True, 1)
    assert limiter.admit("p1", "MANAGE_GRANT") == (False, 0)


def test_a_flood_by_one_principal_never_suppresses_another() -> None:
    """Each principal has a bucket of its own: the second one's denials are all recorded."""
    limiter = DenialAuditLimiter(per_minute=2, clock=_FakeClock())
    for _ in range(100):
        limiter.admit("attacker", "READ_TABLE")
    assert limiter.admit("victim", "READ_TABLE") == (True, 0)
    assert limiter.admit("victim", "READ_TABLE") == (True, 0)


def test_the_first_denial_of_an_action_is_recorded_whoever_flooded_before() -> None:
    """The first-of-a-kind rule is per principal: it is not used up by anyone else."""
    limiter = DenialAuditLimiter(per_minute=1, clock=_FakeClock())
    for _ in range(10):
        limiter.admit("attacker", "READ_TABLE")
    assert limiter.admit("victim", "READ_TABLE") == (True, 0)


def test_an_attacker_cannot_suppress_records_by_filling_the_tables() -> None:
    """Many principals push old state out; that can only record more, never less."""
    limiter = DenialAuditLimiter(per_minute=1, clock=_FakeClock(), max_principals=3, max_kinds=3)
    limiter.admit("victim", "READ_TABLE")
    assert limiter.admit("victim", "READ_TABLE") == (False, 0)
    for number in range(10):
        limiter.admit(f"other{number}", "READ_TABLE")
    assert limiter.admit("victim", "READ_TABLE")[0] is True


def test_the_state_is_bounded() -> None:
    """A flood of distinct principals and actions does not grow the tables without bound."""
    limiter = DenialAuditLimiter(per_minute=1, clock=_FakeClock(), max_principals=5, max_kinds=7)
    for number in range(100):
        limiter.admit(f"p{number}", f"A{number}")
    assert limiter.sizes() == (5, 7)


def test_drain_hands_back_what_was_suppressed_and_forgets_it() -> None:
    """Pending counts leave the limiter once, for a principal that had any."""
    limiter = DenialAuditLimiter(per_minute=1, clock=_FakeClock())
    for _ in range(4):
        limiter.admit("a", "READ_TABLE")
    limiter.admit("b", "READ_TABLE")
    assert limiter.drain() == {"a": 3}
    assert limiter.drain() == {}
    assert limiter.admit("a", "READ_TABLE") == (False, 0)
    assert limiter.drain() == {"a": 1}
