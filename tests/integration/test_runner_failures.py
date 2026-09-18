"""Per-step error isolation in the orchestrator runner.

Fail-closed agents (INGEST .. BROADCAST) fail the whole run: the transaction is rolled
back, a FAILED run row plus the failing step are persisted, agent.run.finished FAILED
is published and the exception reaches the caller. Fail-soft agents (EXEC_BRIEF ..
MONITOR) only fail their own step; the run continues and commits as usual.
"""

from __future__ import annotations

import builtins
import dataclasses
import importlib
from unittest import mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from noc_agents.db.models import (
    AgentRunRow,
    BroadcastRow,
    HitlTaskRow,
    IncidentRow,
    ProblemRow,
    ShiftLedgerRow,
    get_session,
)
from noc_agents.domain.schemas import EventIngest
from noc_agents.graph.pipeline import process_event
from noc_agents.graph.workflow_nodes import graph_status_map
from noc_agents.orchestrator import runner
from noc_agents.realtime.hub import hub
from test_golden_sequence import BTS_EVENT, GOLDEN_FULL_HITL, HUB_EVENT, _run_events, _shape  # same folder


@pytest.fixture()
def clean_hub():
    hub._history.clear()
    yield hub
    hub._history.clear()


def boom(*_args, **_kwargs):
    raise RuntimeError("boom")


def _replace_card(monkeypatch, node_id: str, **changes) -> None:
    """Swap one NodeCard's callables for this test (the runner binds NODE_CARDS at import)."""
    cards = tuple(dataclasses.replace(c, **changes) if c.node_id == node_id else c for c in runner.NODE_CARDS)
    monkeypatch.setattr(runner, "NODE_CARDS", cards)


def _in_new_session(read):
    other = get_session()
    try:
        return read(other)
    finally:
        other.close()


def _only_run(session) -> AgentRunRow:
    runs = session.scalars(select(AgentRunRow)).all()
    assert len(runs) == 1
    runs[0].steps  # load now: the helper closes the session before the caller looks
    return runs[0]


def _run_for(incident_id: str):
    def read(session):
        run = session.scalar(select(AgentRunRow).where(AgentRunRow.incident_id == incident_id))
        run.steps
        return run

    return _in_new_session(read)


def _steps(run: AgentRunRow) -> list[tuple[int, str, str, str]]:
    return [(s.seq, s.node_name, s.agent_name, s.status) for s in run.steps]


# --- fail-closed --------------------------------------------------------------------------


def test_fail_closed_ticket_error_persists_failed_run(tmp_db, clean_hub, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setattr("noc_agents.agents.ticket.next_incident_number", boom)

    with pytest.raises(RuntimeError, match="boom"):
        process_event(session, settings, EventIngest(**HUB_EVENT))

    run = _in_new_session(_only_run)
    assert (run.status, run.current_node, run.error_summary, run.incident_id) == (
        "FAILED",
        "TICKET",
        "RuntimeError: boom",
        None,
    )
    assert run.finished_at is not None
    assert _steps(run) == [(5, "TICKET", "TicketingAgent", "FAILED")]  # earlier steps rolled back
    failed = run.steps[0]
    assert failed.input_summary == "P2"
    assert failed.rationale == "RuntimeError: boom"
    assert failed.tools_called == []
    assert failed.confidence is None
    assert isinstance(failed.duration_ms, int)
    assert _in_new_session(lambda s: s.scalars(select(IncidentRow)).all()) == []

    # Phase 1 §7.0.4 (addendum A5). This assertion used to read
    #     GOLDEN_FULL_HITL[:9] + [("agent.run.finished", ...FAILED...)]
    # i.e. it PINNED THE BUG: nine agent.step.* events were announced to the UI for steps
    # that the rollback then discarded, so operators saw four nodes "complete" and no rows
    # survived. Events are now buffered and flushed after commit, and the rollback discards
    # them — so a failed run announces exactly one thing: that it failed.
    events = _run_events(run.id)
    assert [_shape(e) for e in events] == [
        ("agent.run.finished", 5, "TICKET", None, "FAILED", None, False)  # node: additive key on failure
    ]
    assert events[-1]["payload"] == {
        "seq": 5,
        "incident_number": None,
        "run_id": run.id,
        "status": "FAILED",
        "error": "RuntimeError: boom",
        "node": "TICKET",
    }
    assert not [e for e in hub._history if e["type"] == "incident.created"]

    # The same session is usable afterwards, and the rolled-back INC number is reused.
    monkeypatch.undo()
    inc = process_event(session, settings, EventIngest(**HUB_EVENT))
    assert inc.incident_number == "INC000001"


def test_fail_closed_broadcast_error(tmp_db, clean_hub, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setattr("noc_agents.agents.broadcast.dispatch_incident_email", boom)

    with pytest.raises(RuntimeError, match="boom"):
        process_event(session, settings, EventIngest(**BTS_EVENT))  # P4 -> auto branch sends email

    run = _in_new_session(_only_run)
    assert (run.status, run.current_node, run.incident_id) == ("FAILED", "BROADCAST", None)
    assert _steps(run) == [(8, "BROADCAST", "BroadcastCommsAgent", "FAILED")]
    for table in (IncidentRow, BroadcastRow, HitlTaskRow):
        assert _in_new_session(lambda s, t=table: s.scalars(select(t)).all()) == [], table.__name__


def test_fail_soft_db_error_escalates_to_fail_closed(tmp_db, clean_hub, monkeypatch):
    def db_gone(state, ctx):
        raise OperationalError("SELECT 1", {}, Exception("database is locked"))

    settings, session = tmp_db
    _replace_card(monkeypatch, "MONITOR", run=db_gone)

    with pytest.raises(OperationalError):
        process_event(session, settings, EventIngest(**HUB_EVENT))

    run = _in_new_session(_only_run)
    assert (run.status, run.current_node) == ("FAILED", "MONITOR")
    assert run.error_summary.startswith("OperationalError")  # the real cause, not PendingRollbackError
    assert _steps(run) == [(12, "MONITOR", "WorklogMonitorAgent", "FAILED")]
    assert _in_new_session(lambda s: s.scalars(select(IncidentRow)).all()) == []


# --- fail-soft ----------------------------------------------------------------------------


def test_fail_soft_ledger_error_keeps_the_run_going(tmp_db, clean_hub, monkeypatch):
    settings, session = tmp_db
    # RuntimeError on purpose (not OSError): this exercises the runner's isolation, not the agent's.
    # Phase 1 moved the xlsx append out of the node into the outbox dispatcher, so the old patch
    # target (agents.ledger.write_excel_row) no longer exists there. Retargeted to the pure render
    # the node DOES call, which keeps this test's intent exactly: a node that raises must be
    # absorbed fail-soft by the runner. Not a re-baseline — every assertion below is unchanged.
    monkeypatch.setattr("noc_agents.agents.ledger.ledger_row_cells", boom)

    inc = process_event(session, settings, EventIngest(**HUB_EVENT))

    assert inc.incident_number == "INC000001"
    run = _in_new_session(_only_run)
    assert (run.status, run.incident_id, run.error_summary) == ("WAITING_HITL", inc.id, None)
    by_node = {s.node_name: s for s in run.steps}
    assert [(s.seq, s.node_name, s.status) for s in run.steps] == [
        (seq, node, "FAILED" if node == "LEDGER" else status)
        for (t, seq, node, _agent, status, _n, _i) in GOLDEN_FULL_HITL
        if t == "agent.step.completed"
    ]
    ledger = by_node["LEDGER"]
    assert ledger.output_summary == "LEDGER failed (fail-soft); run continued"
    assert ledger.rationale == "RuntimeError: boom"
    assert ledger.confidence is None
    (tool,) = ledger.tools_called
    assert isinstance(tool.pop("latency_ms"), int)
    assert tool == {"name": "append_excel_row", "ok": False, "error": "RuntimeError: boom"}
    assert by_node["MONITOR"].status == "SUCCEEDED"
    assert _in_new_session(lambda s: s.scalars(select(ShiftLedgerRow)).all()) == []

    statuses = {n["id"]: n["status"] for n in graph_status_map({"LEDGER": ledger.status.lower()})}
    assert statuses["LEDGER"] == "failed"

    expected = [
        (t, seq, node, agent, "FAILED" if (node == "LEDGER" and t == "agent.step.completed") else status, n, i)
        for (t, seq, node, agent, status, n, i) in GOLDEN_FULL_HITL
    ]
    assert [_shape(e) for e in _run_events(run.id)] == expected


def test_ledger_node_does_no_file_io_so_a_locked_workbook_cannot_reach_the_run(tmp_db, clean_hub):
    """Phase 1 replaced Stage C7's test with a stronger, structural one.

    C7 asserted that the LEDGER *agent* absorbed a PermissionError from the xlsx (step
    SUCCEEDED, tool ok=False, DB row still written). That scenario can no longer occur:
    the xlsx append moved out of the node into the outbox dispatcher (§5.3.9), so the node
    performs no file I/O at all and a workbook open in Excel is structurally incapable of
    affecting the incident run.

    The old test absorbed the error and the ledger line was **lost forever**. The dispatcher
    now retries it, so the behaviour is strictly better — proved end to end by
    ``tests/unit/test_outbox.py::test_locked_workbook_is_the_dispatchers_problem_not_the_runs``,
    which blocks the workbook, shows the run untouched, then unblocks it and shows the row
    actually appended on the next drain.

    What is asserted here is the invariant that makes all of that true: the node touches no
    file. If someone reintroduces file I/O into the LEDGER node, this fails loudly.
    """
    settings, session = tmp_db
    opened: list[str] = []

    real_open = builtins.open

    def tracking_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    with mock.patch.object(builtins, "open", tracking_open):
        inc = process_event(session, settings, EventIngest(**HUB_EVENT))

    # No .xlsx was opened anywhere during the run: the append is a queued outbox row.
    assert [p for p in opened if p.lower().endswith(".xlsx")] == []

    run = _in_new_session(_only_run)
    assert (run.status, run.incident_id, run.error_summary) == ("WAITING_HITL", inc.id, None)
    ledger = {s.node_name: s for s in run.steps}["LEDGER"]
    assert ledger.status == "SUCCEEDED"
    assert ledger.tools_called == [{"name": "outbox.enqueue", "ok": True, "latency_ms": 2, "error": None}]

    # The DB ledger row is still written inside the node, exactly as before.
    rows = _in_new_session(lambda s: s.scalars(select(ShiftLedgerRow)).all())
    assert [(r.incident_number, r.priority) for r in rows] == [(inc.incident_number, "P2")]


def test_fail_soft_recurrence_error(tmp_db, clean_hub, monkeypatch):
    settings, session = tmp_db
    base = EventIngest(
        site_id="SFC-RFT-HUB-ELD",
        site_name="Eldoret Rift HUB",
        site_type="HUB",
        region_code="RFT",
        alarm_code="GENSET_FAIL",
        failure_domain="POWER",
        users_affected=160000,
    )
    # Distinct alarm codes: identical events would merge at CORRELATE and never reach RECURRENCE.
    for i in range(2):
        process_event(session, settings, base.model_copy(update={"alarm_code": f"GENSET_FAIL_{i}"}))
    monkeypatch.setattr("noc_agents.agents.recurrence.next_problem_number", boom)

    inc = process_event(session, settings, base.model_copy(update={"alarm_code": "GENSET_FAIL_2"}))

    run = _run_for(inc.id)
    by_node = {s.node_name: s for s in run.steps}
    assert run.status == "WAITING_HITL"
    assert by_node["RECURRENCE"].status == "FAILED"
    assert by_node["RECURRENCE"].rationale == "RuntimeError: boom"
    assert by_node["MONITOR"].status == "SUCCEEDED"
    assert _in_new_session(lambda s: s.get(IncidentRow, inc.id).problem_id) is None
    assert _in_new_session(lambda s: s.scalars(select(ProblemRow)).all()) == []


def test_fail_soft_input_summary_error(tmp_db, clean_hub, monkeypatch):
    settings, session = tmp_db
    _replace_card(monkeypatch, "EXEC_BRIEF", input_summary=boom)

    inc = process_event(session, settings, EventIngest(**HUB_EVENT))

    run = _in_new_session(_only_run)
    by_node = {s.node_name: s for s in run.steps}
    assert run.status == "WAITING_HITL" and run.incident_id == inc.id
    assert by_node["EXEC_BRIEF"].status == "FAILED"
    assert by_node["EXEC_BRIEF"].input_summary == ""
    assert by_node["EXEC_BRIEF"].rationale == "RuntimeError: boom"
    assert by_node["MONITOR"].status == "SUCCEEDED"


# --- over HTTP ------------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "failures.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")

    import noc_agents.config as cfg
    import noc_agents.db.models as models
    import noc_agents.main as main

    cfg.clear_settings_cache()
    models._engine = None
    models.SessionLocal = None
    importlib.reload(main)
    # raise_server_exceptions=False: we want the HTTP 500 the browser would see, not the exception
    with TestClient(main.app, raise_server_exceptions=False) as c:
        yield c


def test_post_events_returns_500_and_the_app_keeps_serving(client, monkeypatch):
    monkeypatch.setattr("noc_agents.agents.ticket.next_incident_number", boom)
    r = client.post("/api/v1/events", json=HUB_EVENT)
    assert r.status_code == 500
    assert client.get("/api/v1/incidents").json() == []

    runs = client.get("/api/v1/runs").json()
    assert [(x["status"], x["current_node"], x["error_summary"]) for x in runs] == [
        ("FAILED", "TICKET", "RuntimeError: boom")
    ]
    assert [(s["node_name"], s["status"]) for s in runs[0]["steps"]] == [("TICKET", "FAILED")]

    monkeypatch.undo()
    r = client.post("/api/v1/events", json=HUB_EVENT)
    assert r.status_code == 200
    assert r.json()["incident"]["incident_number"] == "INC000001"
    assert len(client.get("/api/v1/incidents").json()) == 1
