from __future__ import annotations

import asyncio
import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"


@dataclass
class RealtimeEvent:
    type: str
    operator_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    incident_id: str | None = None
    run_id: str | None = None
    ts: str = field(default_factory=_utc_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class HistoryRecord(dict):
    """One ring-buffer record: the six-key envelope, plus the global ``seq`` as a sibling field.

    It *is* the envelope dict (same keys, same equality, same JSON), so every reader of
    ``recent()`` / ``_history`` sees exactly what it saw before; ``seq`` lives on the object,
    never as a key, so the published envelope stays six keys (spec §7.0.4, G2).
    """

    __slots__ = ("seq",)

    def __init__(self, data: dict[str, Any], seq: int) -> None:
        super().__init__(data)
        self.seq = seq


class EventHub:
    """In-process pub/sub for WebSocket/SSE clients.

    Subscriber queues belong to the event loop that created them (``subscribe`` must be
    called from a coroutine). ``publish_sync`` is called from threadpool threads, so it
    hands each put to that loop with ``call_soon_threadsafe``. A subscriber whose queue
    overflows is dropped and completed with a ``None`` sentinel so its consumer wakes up
    and closes instead of staying open while receiving nothing. ``publish_sync`` never
    raises: every step is guarded per subscriber.

    Every stored record carries a global monotonic ``seq`` (a sibling field of the record,
    not an envelope key). ``recent(n)`` keeps returning the last *n* envelopes; ``since(seq)``
    returns the newer records wrapped as ``{"seq": N, "event": <envelope>}`` for clients that
    resume after a reconnect. A subscriber that asks for ``with_seq=True`` receives the same
    wrapped frame on the live path; the default subscriber still receives the bare envelope.
    """

    QUEUE_SIZE = 200

    def __init__(self, history: int = 2000) -> None:
        self._subs: dict[asyncio.Queue, asyncio.AbstractEventLoop] = {}
        self._wrapped: set[asyncio.Queue] = set()  # subscribers that want {"seq", "event"} frames
        self._history: deque[dict] = deque(maxlen=history)  # HistoryRecord: envelope dict + .seq
        self._seq = 0
        self._lock = threading.Lock()  # seq assignment + append happen together, across threads

    def subscribe(self, *, with_seq: bool = False) -> asyncio.Queue:
        """Register a queue bound to the running loop. Call from a coroutine only."""
        q: asyncio.Queue = asyncio.Queue(maxsize=self.QUEUE_SIZE)
        self._subs[q] = asyncio.get_running_loop()
        if with_seq:
            self._wrapped.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.pop(q, None)
        self._wrapped.discard(q)

    def _offer(self, q: asyncio.Queue, data: dict | None) -> None:
        """Put on the queue; always runs on the queue's own loop thread."""
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            self._subs.pop(q, None)
            self._wrapped.discard(q)
            try:
                q.get_nowait()  # make room for the overflow sentinel so the consumer wakes up
                q.put_nowait(None)
            except Exception:
                pass

    def publish_sync(self, event: RealtimeEvent) -> None:
        """Fan out from sync code (threadpool or loop thread). Never raises."""
        with self._lock:
            self._seq += 1
            data = HistoryRecord(event.to_dict(), self._seq)
            self._history.append(data)
        frame = {"seq": data.seq, "event": data}
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        for q, loop in list(self._subs.items()):
            item = frame if q in self._wrapped else data
            try:
                if loop.is_closed():
                    self._subs.pop(q, None)
                    self._wrapped.discard(q)
                elif running is loop:
                    self._offer(q, item)
                else:
                    loop.call_soon_threadsafe(self._offer, q, item)  # RuntimeError if the loop closed meanwhile
            except Exception:
                self._subs.pop(q, None)  # drop the subscriber, keep publishing to the others
                self._wrapped.discard(q)

    async def publish(self, event: RealtimeEvent) -> None:
        self.publish_sync(event)

    def recent(self, n: int = 20) -> list[dict]:
        return list(self._history)[-n:]

    def since(self, seq: int) -> list[dict]:
        """Records newer than ``seq`` (strictly greater), oldest first, as ``{"seq", "event"}``.

        Records older than the ring buffer keeps are gone: a client that falls further behind
        than ``history`` records receives whatever is still retained.
        """
        with self._lock:
            records = list(self._history)
        return [{"seq": r.seq, "event": r} for r in records if getattr(r, "seq", 0) > seq]

    @property
    def last_seq(self) -> int:
        """The seq of the most recently published event (0 before the first)."""
        return self._seq


hub = EventHub()
