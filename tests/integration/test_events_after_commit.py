"""Realtime after commit (spec §7.0.4): the UI is never told about a row the database did not keep.

``RunTracker`` buffers its ``agent.*`` events on the session (``realtime/commit_hook.py``) and
the session's ``after_commit`` listener publishes them; any rollback discards them. Pinned here:

* a run that rolls back emits ZERO events (the fail-closed path announces only the FAILED run
  it persisted in its fresh transaction — and that row is durable when announced);
* a committed run emits each of its 26 golden events EXACTLY ONCE, in publish order, each one
  durable at announce, with the six-key envelope untouched (the global ``seq`` is a sibling
  field of the ring-buffer record, never an envelope key);
* a savepoint release does not flush, a session closed without a commit does not leak its
  buffer into its next transaction;
* ``EventHub.since(seq)`` and ``/ws/ops?since=N`` replay only the newer records, wrapped as
  ``{"seq": N, "event": <envelope>}``; ``recent(10)`` / ``recent(15)`` callers are unaffected,
  and the live ``/ws/ops`` frame stays the bare envelope the frozen contract test pins.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from noc_agents.db.models import AgentRunRow, IncidentRow, get_session, new_id, utcnow
from noc_agents.domain.schemas import EventIngest
from noc_agents.graph.instrumentation import RunTracker
from noc_agents.graph.pipeline import process_event
from noc_agents.realtime.commit_hook import pending_events
from noc_agents.realtime.hub import EventHub, HistoryRecord, RealtimeEvent, hub
from test_golden_sequence import ENVELOPE_KEYS, GOLDEN_FULL_HITL, HUB_EVENT, _run_events, _shape  # same folder


@pytest.fixture()
def clean_hub():
    hub._history.clear()
    yield hub
    hub._history.clear()


def boom(*_args, **_kwargs):
    raise RuntimeError("boom")


def _in_new_session(read):
    other = get_session()
    try:
        return read(other)
    finally:
        other.close()


def _new_run(session, settings) -> AgentRunRow:
    run = AgentRunRow(
        id=new_id(),
        operator_id=settings.operator.operator_id,
        graph_name="incident_lifecycle",
        trigger="TEST",
        status="RUNNING",
        started_at=utcnow(),
    )
    session.add(run)
    session.flush()
    return run


def _ev(i: int, **extra) -> RealtimeEvent:
    return RealtimeEvent(type="test.event", operator_id="safaricom", payload={"i": i}, **extra)


# --- rollback: zero events ------------------------------------------------------------------


def test_rolled_back_run_emits_zero_events(tmp_db, clean_hub):
    settings, session = tmp_db
    run = _new_run(session, settings)
    tracker = RunTracker(session, run)
    for node, agent in (("INGEST", "IngestCorrelationAgent"), ("CORRELATE", "IngestCorrelationAgent")):
        step = tracker.start_step(node, agent, "x")
        tracker.complete_step(step, output_summary="ok")
    tracker.finish_run("SUCCEEDED")

    # Inside the transaction: the five events are parked on the session, none has left.
    assert [e.type for e in pending_events(session)] == [
        "agent.step.started",
        "agent.step.completed",
        "agent.step.started",
        "agent.step.completed",
        "agent.run.finished",
    ]
    assert _run_events(run.id) == []

    session.rollback()

    assert pending_events(session) == []
    assert _run_events(run.id) == []
    # Discarded, not deferred: a later, unrelated commit on the same session publishes nothing.
    session.execute(text("SELECT 1"))
    session.commit()
    assert _run_events(run.id) == []
    assert _in_new_session(lambda s: s.get(AgentRunRow, run.id)) is None


def test_fail_closed_run_announces_only_the_failed_run_it_persisted(tmp_db, clean_hub, monkeypatch):
    """The bug this module fixes: TICKET raises, the transaction rolls back, and the UI must NOT
    have been told that INGEST..SEVERITY completed. The FAILED run, persisted by the fail-closed
    path in a fresh transaction, is still announced — and is durable when announced."""
    settings, session = tmp_db
    monkeypatch.setattr("noc_agents.agents.ticket.next_incident_number", boom)

    durable_when_announced: list[bool] = []
    publish_sync = EventHub.publish_sync

    def spy(self, event):
        if event.type == "agent.run.finished":
            durable_when_announced.append(
                _in_new_session(lambda s: getattr(s.get(AgentRunRow, event.run_id), "status", None)) == "FAILED"
            )
        publish_sync(self, event)

    monkeypatch.setattr(EventHub, "publish_sync", spy)

    with pytest.raises(RuntimeError, match="boom"):
        process_event(session, settings, EventIngest(**HUB_EVENT))

    (run,) = _in_new_session(lambda s: s.scalars(select(AgentRunRow)).all())
    assert (run.status, run.current_node, run.error_summary) == ("FAILED", "TICKET", "RuntimeError: boom")
    assert _in_new_session(lambda s: s.scalars(select(IncidentRow)).all()) == []

    events = _run_events(run.id)
    assert [_shape(e) for e in events] == [("agent.run.finished", 5, "TICKET", None, "FAILED", None, False)]
    assert events[0]["payload"]["error"] == "RuntimeError: boom"
    assert durable_when_announced == [True]
    assert [e["type"] for e in hub._history] == ["agent.run.finished"]  # nothing else leaked either
    assert pending_events(session) == []


# --- commit: each event exactly once, in publish order, durable at announce ------------------


def test_committed_run_emits_each_event_exactly_once_in_publish_order(tmp_db, clean_hub, monkeypatch):
    settings, session = tmp_db

    publish_order: list[tuple] = []
    durable: list[bool] = []
    publish_sync = EventHub.publish_sync

    def spy(self, event):
        if event.run_id is not None and event.type.startswith("agent."):
            publish_order.append(_shape(event.to_dict()))
            durable.append(_in_new_session(lambda s: s.get(AgentRunRow, event.run_id)) is not None)
        publish_sync(self, event)

    monkeypatch.setattr(EventHub, "publish_sync", spy)

    inc = process_event(session, settings, EventIngest(**HUB_EVENT))
    run = _in_new_session(lambda s: s.scalar(select(AgentRunRow).where(AgentRunRow.incident_id == inc.id)))
    events = _run_events(run.id)

    shapes = [_shape(e) for e in events]
    assert shapes == GOLDEN_FULL_HITL  # the 26 literals, order and per-run seq untouched
    assert len(shapes) == len(set(shapes)) == 26  # exactly once each
    assert publish_order == GOLDEN_FULL_HITL[:-1]  # the agent.* events reached the hub in this order
    assert all(durable), durable  # the run row was readable elsewhere at every announce
    assert pending_events(session) == []

    # The envelope is still six keys; the global seq is a sibling field of the stored record.
    for e in events:
        assert set(e) == ENVELOPE_KEYS, e["type"]
        assert isinstance(e, HistoryRecord) and isinstance(e.seq, int)
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == 26


def test_savepoint_release_does_not_flush(tmp_db, clean_hub):
    settings, session = tmp_db
    run = _new_run(session, settings)
    tracker = RunTracker(session, run)
    step = tracker.start_step("INGEST", "IngestCorrelationAgent", "x")
    tracker.complete_step(step, output_summary="ok")

    with session.begin_nested():  # RELEASE SAVEPOINT fires after_commit too — the rows are not durable yet
        session.execute(text("SELECT 1"))
    assert len(pending_events(session)) == 2
    assert _run_events(run.id) == []

    session.commit()
    assert [e["type"] for e in _run_events(run.id)] == ["agent.step.started", "agent.step.completed"]
    assert pending_events(session) == []


def test_session_closed_without_commit_does_not_leak_its_buffer(tmp_db, clean_hub):
    settings, session = tmp_db
    run = _new_run(session, settings)
    tracker = RunTracker(session, run)
    tracker.start_step("INGEST", "IngestCorrelationAgent", "x")
    session.close()  # no commit, no rollback: session.info survives close()
    assert pending_events(session) == []

    other = _new_run(session, settings)  # the same Session object, reused
    session.commit()
    assert _run_events(run.id) == []
    assert _in_new_session(lambda s: s.get(AgentRunRow, other.id)) is not None


# --- EventHub: since(seq) and the unchanged recent(n) ----------------------------------------


def test_since_replays_only_newer_records(clean_hub):
    for i in range(5):
        hub.publish_sync(_ev(i))
    records = hub.recent(5)
    seqs = [r.seq for r in records]
    assert seqs == sorted(seqs) and len(set(seqs)) == 5
    assert seqs[-1] == hub.last_seq

    newer = hub.since(seqs[1])
    assert [f["seq"] for f in newer] == seqs[2:]  # strictly newer than the given seq
    assert [f["event"]["payload"]["i"] for f in newer] == [2, 3, 4]
    for f in newer:
        assert set(f) == {"seq", "event"}
        assert set(f["event"]) == ENVELOPE_KEYS  # seq is never an envelope key
    assert hub.since(seqs[-1]) == []
    assert [f["event"]["payload"]["i"] for f in hub.since(0)] == [0, 1, 2, 3, 4]


def test_recent_callers_are_unaffected(clean_hub):
    for i in range(20):
        hub.publish_sync(_ev(i))
    ten, fifteen = hub.recent(10), hub.recent(15)
    assert [e["payload"]["i"] for e in ten] == list(range(10, 20))
    assert [e["payload"]["i"] for e in fifteen] == list(range(5, 20))
    for e in ten + fifteen:
        assert isinstance(e, dict) and set(e) == ENVELOPE_KEYS
    assert [e["payload"]["i"] for e in hub.recent()] == list(range(20))  # default n=20 unchanged

    assert EventHub()._history.maxlen == 2000  # constructor default raised from 100
    small = EventHub(history=3)
    for i in range(5):
        small.publish_sync(_ev(i))
    assert [e["payload"]["i"] for e in small.recent(10)] == [2, 3, 4]
    assert [f["event"]["payload"]["i"] for f in small.since(0)] == [2, 3, 4]  # only what is retained


def test_subscriber_with_seq_gets_wrapped_frames_and_the_default_subscriber_stays_bare(clean_hub):
    async def scenario():
        bare, wrapped = hub.subscribe(), hub.subscribe(with_seq=True)
        try:
            hub.publish_sync(_ev(1))  # on the loop thread: the direct _offer path
            return bare.get_nowait(), wrapped.get_nowait()
        finally:
            hub.unsubscribe(bare)
            hub.unsubscribe(wrapped)

    b, w = asyncio.run(scenario())
    assert set(b) == ENVELOPE_KEYS  # today's subscribers (SSE, /ws/ops live) see no change
    assert set(w) == {"seq", "event"} and w["seq"] == hub.last_seq and w["event"] == b
    assert not hub._subs and not hub._wrapped


# --- /ws/ops?since=N over the real app --------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "events.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")

    import noc_agents.config as cfg
    import noc_agents.db.models as models
    import noc_agents.main as main

    cfg.clear_settings_cache()
    models._engine = None
    models.SessionLocal = None
    importlib.reload(main)
    assert main.hub is hub  # the app publishes through the same singleton the tests fill
    with TestClient(main.app) as c:
        yield c


def test_ws_ops_since_replays_only_newer_frames(client, clean_hub):
    for i in range(3):
        hub.publish_sync(_ev(i, incident_id=f"inc-{i}"))
    s0, s1, s2 = (r.seq for r in hub.recent(3))

    with client.websocket_connect(f"/ws/ops?since={s0}") as ws:
        first, second = ws.receive_json(), ws.receive_json()
        assert (first["seq"], second["seq"]) == (s1, s2)
        assert (first["event"]["incident_id"], second["event"]["incident_id"]) == ("inc-1", "inc-2")
        assert set(first["event"]) == ENVELOPE_KEYS

        hub.publish_sync(_ev(3, incident_id="inc-3"))  # live: published from the test thread
        live = ws.receive_json()
        # The live frame is the bare envelope: pinned by the frozen contract test
        # (tests/system/test_contracts.py::test_ws_ops_replays_recent_and_delivers_live_events).
        assert live["incident_id"] == "inc-3" and set(live) == ENVELOPE_KEYS

    with client.websocket_connect(f"/ws/ops?since={hub.last_seq}") as ws:  # nothing newer: no replay
        hub.publish_sync(_ev(4, incident_id="inc-4"))
        assert ws.receive_json()["incident_id"] == "inc-4"


def test_ws_ops_without_since_replays_recent_15_as_before(client, clean_hub):
    for i in range(20):
        hub.publish_sync(_ev(i, incident_id=f"inc-{i}"))

    with client.websocket_connect("/ws/ops") as ws:
        replay = [ws.receive_json() for _ in range(15)]
        assert [f["incident_id"] for f in replay] == [f"inc-{i}" for i in range(5, 20)]
        assert all(set(f) == ENVELOPE_KEYS for f in replay)  # the recent(15) replay is unchanged
