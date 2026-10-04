"""The public form's sliding-window limiter, on an injected clock: per-key windows, atomic
multi-key admission (per number AND per client address), and the hard cap on memory."""

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
        assert limiter.check([("a", 3)], window_seconds=60) is None
        clock.now += 10
    assert limiter.check([("a", 3)], window_seconds=60) == 30.0  # the first hit ages out at +60
    clock.now += 30
    assert limiter.check([("a", 3)], window_seconds=60) is None


def test_keys_are_independent_and_a_refusal_is_not_counted():
    clock = _Clock()
    limiter = SlidingWindowLimiter(clock=clock)
    assert limiter.check([("a", 1)], window_seconds=60) is None
    assert limiter.check([("a", 1)], window_seconds=60) is not None
    assert limiter.check([("a", 1)], window_seconds=60) is not None
    assert limiter.check([("b", 1)], window_seconds=60) is None
    clock.now += 60
    assert limiter.check([("a", 1)], window_seconds=60) is None  # refusals did not extend the window


def test_a_request_is_admitted_only_when_every_key_is_under_its_limit_and_counted_only_then():
    clock = _Clock()
    limiter = SlidingWindowLimiter(clock=clock)
    # one address, many numbers: the per-address limit bites even though each number is fresh
    assert limiter.check([("msisdn:1", 5), ("ip:x", 2)], window_seconds=60) is None
    assert limiter.check([("msisdn:2", 5), ("ip:x", 2)], window_seconds=60) is None
    assert limiter.check([("msisdn:3", 5), ("ip:x", 2)], window_seconds=60) is not None
    # ...and the refused request did not use up msisdn:3's allowance
    assert limiter.check([("msisdn:3", 1), ("ip:y", 2)], window_seconds=60) is None


def test_memory_is_hard_capped_stale_keys_first_then_the_least_recently_used():
    clock = _Clock()
    limiter = SlidingWindowLimiter(clock=clock, max_keys=3)
    for key in ("a", "b", "c"):
        limiter.check([(key, 10)], window_seconds=100)
        clock.now += 1
    limiter.check([("a", 10)], window_seconds=100)  # "a" is now the most recently used
    limiter.check([("d", 10)], window_seconds=100)  # four live keys: the oldest, "b", goes
    assert len(limiter) == 3 and set(limiter._hits) == {"a", "c", "d"}
    clock.now += 200  # everything stale: pruned before anything live would be evicted
    limiter.check([("e", 10)], window_seconds=100)
    assert set(limiter._hits) == {"e"}


def test_a_spray_of_fresh_numbers_never_grows_past_the_cap():
    clock = _Clock()
    limiter = SlidingWindowLimiter(clock=clock, max_keys=50)
    for n in range(5000):
        limiter.check([(f"msisdn:{n}", 5)], window_seconds=600)
    assert len(limiter) == 50


def test_reset_forgets_everything():
    limiter = SlidingWindowLimiter(clock=_Clock())
    limiter.check([("a", 1)], window_seconds=10)
    limiter.reset()
    assert len(limiter) == 0
