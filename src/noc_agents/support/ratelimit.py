"""A small in-process sliding-window limiter for the public complaint form, keyed per MSISDN.

``POST /api/v1/support/complaints`` needs no login (it is how a customer reaches the desk),
so something has to stop one number being used to flood the queue. The limit
(``rate_limit`` in ``config/support/policy.yaml``, default 5 per 10 minutes) is per normalised
MSISDN and per operator: "0712 345 678" and "+254712345678" are the same customer.

Deliberately in-process and in memory. It resets on restart and is not shared between
workers, which is the right trade for a single-process demo: no new store, nothing to
configure. A deployment with several workers needs a shared limiter at the edge (gateway or
reverse proxy) as well; this one then still bounds each worker. It never stores complaint
text, only timestamps, and forgets a key once its window has passed.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable


class SlidingWindowLimiter:
    """At most ``max_requests`` hits per key in any ``window_seconds``."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic, max_keys: int = 10_000) -> None:
        self._clock = clock
        self._max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str, *, max_requests: int, window_seconds: float) -> float | None:
        """Record a hit and return None, or refuse it and return the seconds until one is allowed."""
        now = self._clock()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= window_seconds:
                hits.popleft()
            if len(hits) >= max_requests:
                return max(0.0, window_seconds - (now - hits[0]))
            hits.append(now)
            if len(self._hits) > self._max_keys:
                self._prune(now, window_seconds)
            return None

    def _prune(self, now: float, window_seconds: float) -> None:
        stale = [k for k, h in self._hits.items() if not h or now - h[-1] >= window_seconds]
        for key in stale:
            del self._hits[key]

    def reset(self) -> None:
        """Forget everything (tests)."""
        with self._lock:
            self._hits.clear()


#: The limiter the public route uses.
complaint_limiter = SlidingWindowLimiter()
