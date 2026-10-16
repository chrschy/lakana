"""Tests for `AuthAuditLimiter`.

The per-client token bucket bounding failed-authentication audit writes.
"""

from lakana_server.auth_rate_limit import AuthAuditLimiter


class _FakeClock:
    """A controllable clock for deterministic token-bucket tests.

    Parameters
    ----------
    start
        The time `__call__` returns before any `advance` call.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def advance(self, seconds: float) -> None:
        """Move the clock forward.

        Parameters
        ----------
        seconds
            How many seconds to add to the current time.
        """
        self._now += seconds

    def __call__(self) -> float:
        """Return the current fake time.

        Returns
        -------
        float
            The time set by the constructor and any `advance` calls so far.
        """
        return self._now


def test_admits_up_to_the_configured_rate_then_refuses() -> None:
    """The 11th attempt within the same window, at a limit of 10, is refused."""
    clock = _FakeClock()
    limiter = AuthAuditLimiter(per_minute=10, clock=clock)

    results = [limiter.admit("1.2.3.4") for _ in range(11)]

    assert results[:10] == [(True, 0)] * 10
    assert results[10] == (False, 0)


def test_admits_again_after_the_bucket_refills() -> None:
    """After exhausting the bucket, waiting out a refill interval admits again."""
    clock = _FakeClock()
    limiter = AuthAuditLimiter(per_minute=10, clock=clock)
    for _ in range(10):
        assert limiter.admit("1.2.3.4") == (True, 0)
    assert limiter.admit("1.2.3.4") == (False, 0)

    clock.advance(6.0)  # 6s * (10 tokens / 60s) = 1 token.

    # The one refusal above is reported as suppressed on this admitted call.
    assert limiter.admit("1.2.3.4") == (True, 1)


def test_suppressed_count_carries_over_to_the_next_admitted_call() -> None:
    """Refusals between two admits are reported, and reset, on the next admit."""
    clock = _FakeClock()
    limiter = AuthAuditLimiter(per_minute=1, clock=clock)
    assert limiter.admit("1.2.3.4") == (True, 0)
    assert limiter.admit("1.2.3.4") == (False, 0)
    assert limiter.admit("1.2.3.4") == (False, 0)

    clock.advance(60.0)

    assert limiter.admit("1.2.3.4") == (True, 2)
    # The count was reset: an immediate refusal starts counting from zero.
    assert limiter.admit("1.2.3.4") == (False, 0)


def test_a_client_is_independent_of_other_clients() -> None:
    """One client's exhausted bucket does not affect another client's."""
    clock = _FakeClock()
    limiter = AuthAuditLimiter(per_minute=1, clock=clock)
    assert limiter.admit("1.2.3.4") == (True, 0)
    assert limiter.admit("1.2.3.4") == (False, 0)

    assert limiter.admit("5.6.7.8") == (True, 0)


def test_the_client_dict_is_bounded_evicting_the_oldest_first() -> None:
    """At most 10 000 distinct clients are tracked; the oldest is evicted first.

    An attacker-influenced client key (`request.client.host`) must not let
    the tracked-client dict grow without bound. Asserted entirely through
    `admit`'s own return value, never `_buckets`: `client-0`'s bucket is
    exhausted first, so, absent eviction, it keeps refusing forever with a
    frozen clock. Once 10 000 distinct clients are tracked and one more is
    admitted, `client-0` — the oldest — is evicted, which is only
    observable from the outside by asking about it again: an evicted
    client is indistinguishable from one never seen before, so it gets a
    brand-new, full bucket and is admitted, where a still-exhausted,
    un-evicted bucket would have refused again.
    """
    clock = _FakeClock()
    limiter = AuthAuditLimiter(per_minute=10, clock=clock)

    for _ in range(10):
        assert limiter.admit("client-0") == (True, 0)
    assert limiter.admit("client-0") == (False, 0)

    for i in range(1, 10_000):
        limiter.admit(f"client-{i}")
    # 10 000 distinct clients are now tracked (client-0..client-9999); still
    # under the bound, so client-0's exhausted bucket has not been evicted.
    assert limiter.admit("client-0") == (False, 0)

    limiter.admit("client-10000")  # The 10 001st distinct client: evicts client-0.

    assert limiter.admit("client-0") == (True, 0)
    # client-10000 itself was not evicted by its own admission.
    assert limiter.admit("client-10000") == (True, 0)
