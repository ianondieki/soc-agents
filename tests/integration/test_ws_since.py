"""The WebSocket replay contract (spec §7.0.4): ``seq``, ``EventHub.since`` and ``/ws/ops?since=N``.

What is implemented, and pinned here:

* every ring-buffer record carries a global monotonic ``seq`` — a sibling field of the record
  (``HistoryRecord.seq``), never an envelope key, so the published envelope stays six keys;
* ``since(N)`` returns the records **strictly newer** than N, oldest first, each wrapped as
  ``{"seq": N, "event": <envelope>}``;
* the ring buffer holds ``EventHub(history=2000)`` — the raised default, not the old 100;
* ``recent(10)`` (SSE ``/api/v1/stream/events``) and ``recent(15)`` (the ``/ws/ops`` connect
  replay) are byte-for-byte what they were: bare envelopes, same order, same type;
* ``/ws/ops?since=N`` replays from ``since(N)``; ``/ws/ops`` with no parameter replays
  ``recent(15)``. A reconnect at the last seq the client saw replays **nothing it already
  had and nothing it missed** — the off-by-one in *both* directions is the point of this file.

KNOWN LIMITATION — resume-by-seq is not usable end to end (unresolved spec/contract conflict).
    Spec §7.0.4 says the live ``/ws/ops`` frame should be wrapped as ``{"seq", "event"}``.
    The FROZEN ``tests/system/test_contracts.py::test_ws_ops_replays_recent_and_delivers_live_events``
    asserts the live frame is the bare six-key envelope. The implementation therefore ships
    live frames BARE and wraps only the ``?since=`` replay path (``hub.subscribe(with_seq=True)``
    exists and works, but ``main.ws_ops`` does not use it). Consequence: a browser can only
    learn a ``seq`` from a replay, so after a live-frame-only session it has no cursor to
    resume from and must fall back to ``recent(15)``. This file tests what IS implemented —
    ``test_live_frames_are_bare_so_a_seq_is_only_learnable_from_a_replay`` pins that reality
    and the limitation with it. Whoever re-cuts the frozen contract flips one line and this
    test is the one that should then be re-baselined; no aspiration is tested here.

Also NOT testable from here, and deliberately not faked:
    a client that falls further behind than the buffer retains gets a SILENT gap —
    ``since()`` returns what survives and says nothing about what was evicted. There is no
    gap signal in the wire format to assert on (see ``test_a_client_further_behind_than_the_buffer``).
"""

from __future__ import annotations

import importlib
import threading

import pytest

from noc_agents.realtime.hub import EventHub, HistoryRecord, RealtimeEvent, hub

ENVELOPE_KEYS = {"type", "operator_id", "payload", "incident_id", "run_id", "ts"}

HUB_EVENT = {
    "site_id": "SFC-NBIE-HUB-EMB",
    "site_name": "Embakasi East Aggregation HUB",
    "site_type": "HUB",
    "region_code": "NBI_E",
    "alarm_code": "POWER_GRID_FAIL",
    "failure_domain": "POWER",
    "users_affected": 450000,
    "access_notes": "Genset not started",
}


def _ev(i: int, **kw) -> RealtimeEvent:
    return RealtimeEvent(type="test.event", operator_id="safaricom", payload={"i": i}, **kw)


@pytest.fixture()
def clean_hub():
    """Empty the singleton's ring buffer. ``_seq`` is deliberately NOT reset: a seq that
    survives a buffer clear is exactly the 'never reused' property under test."""
    hub._history.clear()
    yield hub
    hub._history.clear()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    db = tmp_path / "ws_since.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")

    import noc_agents.config as cfg
    import noc_agents.db.models as models
    import noc_agents.main as main

    cfg.clear_settings_cache()
    models._engine = None
    models.SessionLocal = None
    importlib.reload(main)
    assert main.hub is hub  # the route and these tests share one singleton
    with TestClient(main.app) as c:
        yield c


def _recv(ws, timeout: float = 10.0):
    """``ws.receive_json()`` blocks forever when nothing arrives; fail fast instead of hanging."""
    box: list = []
    reader = threading.Thread(target=lambda: box.append(ws.receive_json()), daemon=True)
    reader.start()
    reader.join(timeout)
    if not box:
        raise AssertionError(f"no websocket frame within {timeout}s")
    return box[0]


# --------------------------------------------------------------- seq: monotonic, never reused


def test_seq_is_monotonic_contiguous_and_never_reused(clean_hub):
    start = hub.last_seq
    for i in range(6):
        hub.publish_sync(_ev(i))
    seqs = [r.seq for r in hub.recent(6)]

    assert seqs == list(range(start + 1, start + 7))  # +1 each time, no gaps, no repeats
    assert hub.last_seq == seqs[-1]
    assert all(isinstance(r, HistoryRecord) and isinstance(r.seq, int) for r in hub.recent(6))

    # Clearing the buffer must NOT rewind the counter: a reconnecting client holding an old
    # cursor would otherwise be replayed events it had already seen under recycled numbers.
    hub._history.clear()
    hub.publish_sync(_ev(99))
    after = hub.recent(1)[0].seq
    assert after == seqs[-1] + 1
    assert after not in seqs


def test_seq_is_unique_across_concurrent_publishers(clean_hub):
    """publish_sync runs on threadpool threads; seq assignment and append are under one lock."""
    start = hub.last_seq
    threads = [
        threading.Thread(target=lambda n=n: [hub.publish_sync(_ev(n * 50 + j)) for j in range(50)])
        for n in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not any(t.is_alive() for t in threads)

    seqs = sorted(r.seq for r in hub.recent(400))
    assert len(seqs) == 400
    assert len(set(seqs)) == 400  # no seq handed out twice
    assert seqs == list(range(start + 1, start + 401))  # and none skipped
    assert hub.last_seq == seqs[-1]
    assert [r.seq for r in hub.recent(400)] == seqs  # stored in seq order, not interleaved


# --------------------------------------------------------------- since(): strictly newer, both ends


def test_since_returns_only_strictly_newer_records_at_every_cut_point(clean_hub):
    for i in range(8):
        hub.publish_sync(_ev(i, incident_id=f"inc-{i}"))
    records = hub.recent(8)
    seqs = [r.seq for r in records]

    # The whole off-by-one surface, checked at every possible cursor: the record AT the
    # cursor is never replayed (the client already had it) and the one after it always is.
    for cut in range(8):
        frames = hub.since(seqs[cut])
        assert [f["seq"] for f in frames] == seqs[cut + 1 :], f"wrong slice at cut {cut}"
        assert [f["event"]["incident_id"] for f in frames] == [f"inc-{i}" for i in range(cut + 1, 8)]
        assert all(f["seq"] > seqs[cut] for f in frames)

    assert [f["seq"] for f in hub.since(seqs[0] - 1)] == seqs  # before the first: everything
    assert hub.since(seqs[-1]) == []  # at the newest: nothing
    assert hub.since(hub.last_seq + 1000) == []  # a cursor from the future: nothing, no crash
    assert hub.since(0) == hub.since(-1)  # 0 and a negative floor both mean "from the start"


def test_since_frames_wrap_the_envelope_without_polluting_it(clean_hub):
    hub.publish_sync(_ev(1, incident_id="inc-1", run_id="run-1"))
    (frame,) = hub.since(hub.last_seq - 1)

    assert set(frame) == {"seq", "event"}
    assert frame["seq"] == hub.last_seq
    assert set(frame["event"]) == ENVELOPE_KEYS  # six keys: seq is never one of them
    assert "seq" not in frame["event"]
    assert frame["event"] == hub.recent(1)[0]  # the wrapped event IS the stored envelope


# --------------------------------------------------------------- the buffer is 2000, not 100


def test_ring_buffer_default_is_2000_and_holds_more_than_the_old_100(clean_hub):
    import inspect

    assert inspect.signature(EventHub.__init__).parameters["history"].default == 2000
    assert hub._history.maxlen == 2000  # the live singleton /ws/ops replays from

    fresh = EventHub()
    for i in range(150):  # more than the old 100: nothing may be evicted
        fresh.publish_sync(_ev(i))
    assert len(fresh._history) == 150
    assert [f["event"]["payload"]["i"] for f in fresh.since(0)] == list(range(150))
    assert fresh.recent(150)[0]["payload"]["i"] == 0  # the oldest of the 150 is still there


def test_a_client_further_behind_than_the_buffer(clean_hub):
    """Eviction is silent: ``since`` returns what survives and cannot report the gap."""
    small = EventHub(history=3)
    for i in range(5):
        small.publish_sync(_ev(i))

    assert [f["event"]["payload"]["i"] for f in small.since(0)] == [2, 3, 4]  # 0 and 1 are gone
    assert [f["seq"] for f in small.since(0)] == [3, 4, 5]  # their seqs are gone with them
    assert small.last_seq == 5  # the counter still knows 5 were published...
    # ...but the replay carries no marker saying "1 and 2 were dropped", so a client resuming
    # from seq 1 silently receives 3,4,5 and cannot tell it lost two. Reported, not worked around.
    assert [f["seq"] for f in small.since(1)] == [3, 4, 5]


# --------------------------------------------------------------- recent(10) / recent(15) unaffected


def test_recent_10_and_15_the_two_live_callers_are_unaffected(clean_hub):
    for i in range(40):  # far more than either caller asks for, and more than the old maxlen tail
        hub.publish_sync(_ev(i))

    ten = hub.recent(10)  # main.py SSE /api/v1/stream/events
    fifteen = hub.recent(15)  # main.py /ws/ops connect replay
    assert [e["payload"]["i"] for e in ten] == list(range(30, 40))
    assert [e["payload"]["i"] for e in fifteen] == list(range(25, 40))
    assert [e["payload"]["i"] for e in hub.recent()] == list(range(20, 40))  # default n=20 unchanged

    for e in ten + fifteen:
        assert isinstance(e, dict) and set(e) == ENVELOPE_KEYS  # bare envelopes, as before
        assert e == dict(e)  # HistoryRecord compares and serialises as a plain dict

    assert len(hub.recent(10_000)) == 40  # asking for more than exists returns what exists

    # FOOTGUN, pinned as-is, not fixed here: recent(n) is ``list(self._history)[-n:]``, and
    # ``[-0:]`` is ``[0:]``, so recent(0) returns the WHOLE buffer instead of nothing. No live
    # caller passes 0 (10, 15 and the default 20 are the only ones), and §7.0.4 says recent(n)
    # must not change, so this stays. It is worth knowing that raising the buffer 100 -> 2000
    # made the blast radius of an accidental recent(0) twenty times bigger.
    assert len(hub.recent(0)) == 40


# --------------------------------------------------------------- /ws/ops over the real app


def test_ws_ops_without_since_replays_recent_15_bare(client, clean_hub):
    for i in range(20):
        hub.publish_sync(_ev(i, incident_id=f"inc-{i}"))

    with client.websocket_connect("/ws/ops") as ws:
        replay = [_recv(ws) for _ in range(15)]
    assert [f["incident_id"] for f in replay] == [f"inc-{i}" for i in range(5, 20)]
    assert all(set(f) == ENVELOPE_KEYS for f in replay)  # no {"seq","event"} on this path


def test_ws_ops_since_replays_only_newer_frames(client, clean_hub):
    for i in range(4):
        hub.publish_sync(_ev(i, incident_id=f"inc-{i}"))
    seqs = [r.seq for r in hub.recent(4)]

    with client.websocket_connect(f"/ws/ops?since={seqs[1]}") as ws:
        frames = [_recv(ws), _recv(ws)]
        assert [f["seq"] for f in frames] == seqs[2:]
        assert [f["event"]["incident_id"] for f in frames] == ["inc-2", "inc-3"]
        assert all(set(f) == {"seq", "event"} and set(f["event"]) == ENVELOPE_KEYS for f in frames)

    with client.websocket_connect(f"/ws/ops?since={hub.last_seq}") as ws:
        # Nothing newer: the replay is empty, so the first frame must be a LIVE one.
        hub.publish_sync(_ev(9, incident_id="inc-live"))
        assert _recv(ws)["incident_id"] == "inc-live"

    with client.websocket_connect("/ws/ops?since=0") as ws:
        # ?since=0 is not the same as omitting the parameter: it replays the whole buffer.
        assert len([_recv(ws) for _ in range(5)]) == 5


def test_ws_reconnect_replays_nothing_twice_and_skips_nothing(client, clean_hub):
    """The reconnect contract, checked in both directions with a sentinel bounding the replay."""
    for i in range(5):
        hub.publish_sync(_ev(i, incident_id=f"inc-{i}"))

    with client.websocket_connect("/ws/ops?since=0") as ws:
        first_session = [_recv(ws) for _ in range(5)]
    assert [f["event"]["incident_id"] for f in first_session] == [f"inc-{i}" for i in range(5)]
    cursor = first_session[-1]["seq"]  # what the client remembers across the drop

    for i in range(5, 8):  # published while the client is disconnected
        hub.publish_sync(_ev(i, incident_id=f"inc-{i}"))

    with client.websocket_connect(f"/ws/ops?since={cursor}") as ws:
        resumed = [_recv(ws) for _ in range(3)]
        # No SKIP: the first missed event (inc-5) is the first frame back.
        # No DUPLICATE: inc-4, which the client already had, is not replayed.
        assert [f["event"]["incident_id"] for f in resumed] == ["inc-5", "inc-6", "inc-7"]
        assert [f["seq"] for f in resumed] == [cursor + 1, cursor + 2, cursor + 3]

        # Bound the replay: the very next frame must be a new live one, proving the server
        # sent exactly three replay frames and not a fourth stale repeat.
        hub.publish_sync(_ev(8, incident_id="inc-sentinel"))
        sentinel = _recv(ws)
        assert sentinel.get("incident_id") == "inc-sentinel", f"unexpected extra replay: {sentinel}"

    # A second reconnect at the new cursor replays nothing at all.
    with client.websocket_connect(f"/ws/ops?since={hub.last_seq}") as ws:
        hub.publish_sync(_ev(10, incident_id="inc-after"))
        assert _recv(ws)["incident_id"] == "inc-after"


def test_live_frames_are_bare_so_a_seq_is_only_learnable_from_a_replay(client, clean_hub):
    """Pins the shipped compromise AND the limitation it creates (see the module docstring)."""
    hub.publish_sync(_ev(0, incident_id="inc-0"))

    with client.websocket_connect("/ws/ops") as ws:
        assert set(_recv(ws)) == ENVELOPE_KEYS  # the recent(15) replay is bare too
        hub.publish_sync(_ev(1, incident_id="inc-live"))
        live = _recv(ws)

    # The frozen contract (tests/system/test_contracts.py) pins this shape. Because of it a
    # client connected WITHOUT ?since= never sees a seq, so it has no cursor to resume from.
    assert set(live) == ENVELOPE_KEYS
    assert "seq" not in live
    # The wrapped-subscriber switch exists and works; main.ws_ops simply does not use it yet.
    assert "with_seq" in EventHub.subscribe.__code__.co_varnames


def test_since_replays_a_real_incident_run_exactly_once(client, clean_hub):
    """End to end on real traffic: one lifecycle's events, each replayed once, in order."""
    base = hub.last_seq
    inc = client.post("/api/v1/events", json=HUB_EVENT).json()["incident"]

    frames = hub.since(base)
    seqs = [f["seq"] for f in frames]
    assert len(frames) >= 20  # a full run publishes ~26 events
    assert seqs == list(range(base + 1, base + 1 + len(frames)))  # contiguous, in publish order
    assert len(set(seqs)) == len(seqs)  # nothing replayed twice
    assert all(set(f) == {"seq", "event"} and set(f["event"]) == ENVELOPE_KEYS for f in frames)

    types = [f["event"]["type"] for f in frames]
    assert "incident.created" in types and "agent.run.finished" in types
    assert any(f["event"]["incident_id"] == inc["id"] for f in frames)

    # Cut anywhere in the real run: the record at the cursor is never resent, the rest always is.
    mid = len(frames) // 2
    assert hub.since(seqs[mid]) == frames[mid + 1 :]
    assert hub.since(seqs[-1]) == []
    assert hub.since(base) == frames  # and re-asking with the original cursor is stable
