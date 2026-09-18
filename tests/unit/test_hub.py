"""Stage C8: EventHub.publish_sync is thread-safe, never raises, and completes overflowing subscribers."""

from __future__ import annotations

import asyncio
import threading

from noc_agents.realtime.hub import EventHub, RealtimeEvent


def _ev(i: int = 0) -> RealtimeEvent:
    return RealtimeEvent(type="test.event", operator_id="safaricom", payload={"i": i})


def test_publish_from_another_thread_reaches_the_loop_queue():
    hub = EventHub()
    ready = threading.Event()
    got: list[dict] = []

    async def consumer():
        q = hub.subscribe()  # bound to this background loop
        ready.set()
        got.append(await asyncio.wait_for(q.get(), timeout=5))

    thread = threading.Thread(target=lambda: asyncio.run(consumer()), daemon=True)
    thread.start()
    assert ready.wait(5)
    hub.publish_sync(_ev(7))  # main thread: no running loop here -> call_soon_threadsafe path
    thread.join(5)
    assert not thread.is_alive()
    assert got and got[0]["payload"] == {"i": 7} and got[0]["type"] == "test.event"


def test_overflow_drops_subscriber_and_leaves_a_none_sentinel():
    hub = EventHub()

    async def scenario():
        q = hub.subscribe()
        for i in range(EventHub.QUEUE_SIZE + 1):  # 201 publishes on the loop thread
            hub.publish_sync(_ev(i))
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        return q, items

    q, items = asyncio.run(scenario())
    assert q not in hub._subs  # no zombie: the subscriber is gone
    assert items[-1] is None  # the consumer wakes up and closes
    assert len(items) == EventHub.QUEUE_SIZE
    assert items[0]["payload"] == {"i": 1}  # the oldest item made room for the sentinel
    assert all(isinstance(x, dict) for x in items[:-1])


def test_publish_without_subscribers_or_loop_only_appends_history():
    hub = EventHub()
    hub.publish_sync(_ev(1))
    hub.publish_sync(_ev(2))
    assert [e["payload"]["i"] for e in hub.recent(5)] == [1, 2]


class _ClosedLoop:
    def is_closed(self) -> bool:
        return True

    def call_soon_threadsafe(self, *_a, **_k):  # pragma: no cover - must not be reached
        raise AssertionError("closed loop must be skipped, not scheduled")


class _DyingLoop:
    def is_closed(self) -> bool:
        return False

    def call_soon_threadsafe(self, *_a, **_k):
        raise RuntimeError("Event loop is closed")


def test_dead_loops_are_dropped_and_publish_still_returns():
    hub = EventHub()
    closed_q, dying_q = asyncio.Queue(), asyncio.Queue()
    hub._subs[closed_q] = _ClosedLoop()  # type: ignore[assignment]
    hub._subs[dying_q] = _DyingLoop()  # type: ignore[assignment]

    async def scenario():
        live_q = hub.subscribe()
        hub.publish_sync(_ev(3))
        return live_q

    live_q = asyncio.run(scenario())
    assert closed_q not in hub._subs and dying_q not in hub._subs
    assert live_q.get_nowait()["payload"] == {"i": 3}
    assert hub.recent(1)[0]["payload"] == {"i": 3}
