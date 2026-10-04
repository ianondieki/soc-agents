"""The public form's per-MSISDN sliding-window limiter, on an injected clock."""

from __future__ import annotations

from noc_agents.support.ratelimit import SlidingWindowLimiter


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_the_window_slides_and_the_refusal_says_how_long_to_wait():
    clock = _Clock()
    limiter = SlidingWindowLimiter(clock=clock)
    for _ in range(3):
        assert limiter.check("a", max_requests=3, window_seconds=60) is None
        clock.now += 10
    assert limiter.check("a", max_requests=3, window_seconds=60) == 30.0  # the first hit ages out at +60
    clock.now += 30
    assert limiter.check("a", max_requests=3, window_seconds=60) is None


def test_keys_are_independent_and_a_refusal_is_not_counted():
    clock = _Clock()
    limiter = SlidingWindowLimiter(clock=clock)
    assert limiter.check("a", max_requests=1, window_seconds=60) is None
    assert limiter.check("a", max_requests=1, window_seconds=60) is not None
    assert limiter.check("a", max_requests=1, window_seconds=60) is not None
    assert limiter.check("b", max_requests=1, window_seconds=60) is None
    clock.now += 60
    assert limiter.check("a", max_requests=1, window_seconds=60) is None  # refusals did not extend the window


def test_stale_keys_are_pruned_past_the_cap_and_reset_forgets_everything():
    clock = _Clock()
    limiter = SlidingWindowLimiter(clock=clock, max_keys=2)
    limiter.check("a", max_requests=1, window_seconds=10)
    limiter.check("b", max_requests=1, window_seconds=10)
    clock.now += 11
    limiter.check("c", max_requests=1, window_seconds=10)
    assert set(limiter._hits) == {"c"}
    limiter.reset()
    assert limiter._hits == {}
