"""A small in-process sliding-window limiter for the public complaint form.

``POST /api/v1/support/complaints`` needs no login (it is how a customer reaches the desk), so
something has to stop it being used to flood the queue. Two limits apply to every request, from
``rate_limit`` in ``config/support/policy.yaml``:

* per **MSISDN** (default 5 per 10 minutes, per operator): "0712 345 678" and "+254712345678"
  are the same customer, because the key is the normalised number;
* per **client address** (default 30 per 10 minutes): one caller cycling through numbers.

A request is admitted only when every one of its keys is under its limit, and is counted
against all of them only then: a refused request never uses up anyone's allowance.

Memory is bounded. Keys whose window has passed are pruned first; if more than ``max_keys``
are still live, the least recently used are evicted oldest-first. Evicting a live key forgets
its history -- the price of a hard cap, and the right one: unbounded growth under a spray of
fresh numbers is the failure a limiter must not have.

Deliberately in-process and in memory. It resets on restart and is not shared between workers,
which is the right trade for a single-process demo. A deployment with several workers needs a
shared limiter at the edge (gateway or reverse proxy) as well; this one then still bounds each
worker. It never stores complaint text, only timestamps.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Sequence

DEFAULT_MAX_KEYS = 10_000


class SlidingWindowLimiter:
    """At most ``limit`` hits per key in any ``window_seconds``, for several keys at once."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic, max_keys: int = DEFAULT_MAX_KEYS) -> None:
        self._clock = clock
        self._max_keys = max_keys
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def check(self, limits: Sequence[tuple[str, int]], *, window_seconds: float) -> float | None:
        """Admit a request that counts against every ``(key, limit)`` and return None, or refuse it
        and return the seconds until the most constrained key admits one again."""
        now = self._clock()
        with self._lock:
            waits = []
            for key, limit in limits:
                hits = self._hits.get(key)
                if hits is None:
                    continue
                while hits and now - hits[0] >= window_seconds:
                    hits.popleft()
                if len(hits) >= limit:
                    waits.append(max(0.0, window_seconds - (now - hits[0])))
            if waits:
                return max(waits)
            for key, _limit in limits:
                self._hits.setdefault(key, deque()).append(now)
                self._hits.move_to_end(key)
            if len(self._hits) > self._max_keys:
                self._shrink(now, window_seconds)
            return None

    def peek(self, limits: Sequence[tuple[str, int]], *, window_seconds: float) -> float | None:
        """Like :meth:`check` but counts nothing: the seconds until the most constrained key admits
        one more hit, or None. With :meth:`record`, a budget that only some outcomes spend (the
        Track page's FAILED attempts): look first, decide, then record only the failure."""
        now = self._clock()
        with self._lock:
            waits = []
            for key, limit in limits:
                hits = self._hits.get(key)
                if hits is None:
                    continue
                while hits and now - hits[0] >= window_seconds:
                    hits.popleft()
                if len(hits) >= limit:
                    waits.append(max(0.0, window_seconds - (now - hits[0])))
            return max(waits) if waits else None

    def record(self, keys: Sequence[str], *, window_seconds: float) -> None:
        """Count one hit against each key, whatever its limit (the outcome has already happened)."""
        now = self._clock()
        with self._lock:
            for key in keys:
                self._hits.setdefault(key, deque()).append(now)
                self._hits.move_to_end(key)
            if len(self._hits) > self._max_keys:
                self._shrink(now, window_seconds)

    def _shrink(self, now: float, window_seconds: float) -> None:
        for key in [k for k, h in self._hits.items() if not h or now - h[-1] >= window_seconds]:
            del self._hits[key]
        while len(self._hits) > self._max_keys:
            self._hits.popitem(last=False)  # the least recently hit live key

    def __len__(self) -> int:
        return len(self._hits)

    def reset(self) -> None:
        """Forget everything (tests)."""
        with self._lock:
            self._hits.clear()


#: The limiter the public route uses.
complaint_limiter = SlidingWindowLimiter()
