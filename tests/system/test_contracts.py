"""REST contract tests: the shapes the React UI and the workflow graph depend on.

These pin structure (keys, order, vocabulary), not business outcomes, so that any
refactor that renames a node, reorders the graph or drops a payload key fails here
rather than in the browser.
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

NODE_IDS = [
    "INGEST",
    "CORRELATE",
    "ENRICH",
    "SEVERITY",
    "TICKET",
    "ASSIGN",
    "HITL",
    "BROADCAST",
    "EXEC_BRIEF",
    "LEDGER",
    "RECURRENCE",
    "MONITOR",
]
NODE_LABELS = [
    "Ingest",
    "Correlate",
    "Enrich",
    "Severity",
    "Ticket",
    "Assign",
    "HITL Gate",
    "Broadcast",
    "Exec Brief",
    "Shift Ledger",
    "Recurrence",
    "Monitor",
]
NODE_AGENTS = [
    "IngestCorrelationAgent",
    "IngestCorrelationAgent",
    "EnrichmentAgent",
    "SeverityImpactAgent",
    "TicketingAgent",
    "DispatchAssignmentAgent",
    "SupervisorAgent",
    "BroadcastCommsAgent",
    "ExecutiveBriefingAgent",
    "ShiftLedgerAgent",
    "RecurrenceProblemAgent",
    "WorklogMonitorAgent",
]
NODE_STATUS_VOCABULARY = {"succeeded", "waiting_hitl", "running", "failed", "pending"}

AGENT_CATALOG = [
    ("SupervisorAgent", "Routes lifecycle and HITL gates"),
    ("IngestCorrelationAgent", "Normalize + dedupe alarms"),
    ("EnrichmentAgent", "Site/region CMDB + user estimate"),
    ("SeverityImpactAgent", "P1–P4 + HUB floors + M-PESA tag"),
    ("TicketingAgent", "Unique INC + narrative fields"),
    ("DispatchAssignmentAgent", "FE vs MSP matrix"),
    ("BroadcastCommsAgent", "RNIO/FE/MSP notifications"),
    ("ExecutiveBriefingAgent", "Exec brief to cut phone spam"),
    ("ShiftLedgerAgent", "Excel shift ledger"),
    ("RecurrenceProblemAgent", "Chronic site problems"),
    ("WorklogMonitorAgent", "Notes + SLA watch"),
    ("ShiftHandoverAgent", "Day/night handover package"),
]

STEP_KEYS = {
    "id",
    "seq",
    "node_name",
    "agent_name",
    "status",
    "started_at",
    "finished_at",
    "duration_ms",
    "input_summary",
    "output_summary",
    "rationale",
    "tools_called",
    "confidence",
}
RUN_KEYS = {
    "id",
    "incident_id",
    "operator_id",
    "graph_name",
    "trigger",
    "status",
    "started_at",
    "finished_at",
    "current_node",
    "error_summary",
    "steps",
}

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


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "contracts.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
    import importlib

    import noc_agents.config as cfg
    import noc_agents.db.models as models
    import noc_agents.main as main

    cfg.clear_settings_cache()
    models._engine = None
    models.SessionLocal = None
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c


@pytest.fixture()
def hub_incident(client):
    r = client.post("/api/v1/events", json=HUB_EVENT)
    assert r.status_code == 200
    return r.json()["incident"]


def test_workflow_nodes_edges_contract(client, hub_incident):
    wf = client.get(f"/api/v1/incidents/{hub_incident['id']}/workflow").json()
    assert wf["incident_id"] == hub_incident["id"]
    assert wf["run_id"]

    assert [n["id"] for n in wf["nodes"]] == NODE_IDS
    assert [n["label"] for n in wf["nodes"]] == NODE_LABELS
    assert [n["agent"] for n in wf["nodes"]] == NODE_AGENTS
    for node in wf["nodes"]:
        assert set(node) == {"id", "label", "agent", "status"}

    assert wf["edges"] == [
        {"source": a, "target": b} for a, b in zip(NODE_IDS, NODE_IDS[1:])
    ]
    assert len(wf["edges"]) == 11


def test_workflow_node_status_vocabulary(client, hub_incident):
    wf = client.get(f"/api/v1/incidents/{hub_incident['id']}/workflow").json()
    statuses = {n["id"]: n["status"] for n in wf["nodes"]}
    assert set(statuses.values()) <= NODE_STATUS_VOCABULARY
    # P2 at L2_GUARDED holds the external blast: HITL + BROADCAST wait, the rest complete.
    assert statuses["HITL"] == "waiting_hitl"
    assert statuses["BROADCAST"] == "waiting_hitl"
    assert all(statuses[n] == "succeeded" for n in NODE_IDS if n not in ("HITL", "BROADCAST"))


def test_workflow_of_merged_incident_is_partial(client, hub_incident):
    """Duplicate alarm: the merge run is newest, so the UI graph shows two done nodes
    and the ten untouched ones fall back to the literal "pending" default."""
    r = client.post("/api/v1/events", json=HUB_EVENT)
    assert r.status_code == 200
    assert r.json()["incident"]["id"] == hub_incident["id"]

    wf = client.get(f"/api/v1/incidents/{hub_incident['id']}/workflow").json()
    assert [s["node_name"] for s in wf["steps"]] == ["INGEST", "CORRELATE"]
    assert {n["id"]: n["status"] for n in wf["nodes"]} == {
        "INGEST": "succeeded",
        "CORRELATE": "succeeded",
        "ENRICH": "pending",
        "SEVERITY": "pending",
        "TICKET": "pending",
        "ASSIGN": "pending",
        "HITL": "pending",
        "BROADCAST": "pending",
        "EXEC_BRIEF": "pending",
        "LEDGER": "pending",
        "RECURRENCE": "pending",
        "MONITOR": "pending",
    }
    assert [n["id"] for n in wf["nodes"]] == NODE_IDS


def _add_run(incident_id: str, graph_name: str, minutes_later: int):
    from datetime import timedelta

    from noc_agents.db.models import AgentRunRow, get_session, new_id, utcnow

    session = get_session()
    try:
        run = AgentRunRow(
            id=new_id(),
            incident_id=incident_id,
            operator_id="safaricom",
            graph_name=graph_name,
            trigger="API",
            status="SUCCEEDED",
            started_at=utcnow() + timedelta(minutes=minutes_later),
            finished_at=utcnow() + timedelta(minutes=minutes_later),
        )
        session.add(run)
        session.commit()
        return run.id
    finally:
        session.close()


def test_workflow_and_timeline_prefer_the_lifecycle_run(client, hub_incident):
    """Stage C10: a newer run of another graph (monitor / assist) must not blank the UI graph."""
    before = client.get(f"/api/v1/incidents/{hub_incident['id']}/workflow").json()
    assist_id = _add_run(hub_incident["id"], "llm_assist", minutes_later=5)

    wf = client.get(f"/api/v1/incidents/{hub_incident['id']}/workflow").json()
    assert wf["run_id"] == before["run_id"] != assist_id
    assert [s["node_name"] for s in wf["steps"]] == NODE_IDS
    assert {n["id"]: n["status"] for n in wf["nodes"]} == {n["id"]: n["status"] for n in before["nodes"]}

    tl = client.get(f"/api/v1/incidents/{hub_incident['id']}/timeline").json()
    agent_steps = [i for i in tl if i["kind"] == "agent_step"]
    assert [i["title"].split(" · ")[1] for i in agent_steps] == NODE_IDS  # same run as /workflow
    assert set(agent_steps[0]) == {"kind", "ts", "title", "status", "detail"}


def test_workflow_falls_back_to_latest_run_without_a_lifecycle_run(client):
    from noc_agents.db.models import IncidentRow, get_session

    session = get_session()
    try:
        inc = IncidentRow(
            operator_id="safaricom",
            incident_number="INC999999",
            site_id="SFC-X",
            region_code="NBI_E",
            correlation_fingerprint="fp",
        )
        session.add(inc)
        session.commit()
        inc_id = inc.id
    finally:
        session.close()
    only_run = _add_run(inc_id, "llm_assist", minutes_later=0)

    wf = client.get(f"/api/v1/incidents/{inc_id}/workflow").json()
    assert wf["run_id"] == only_run
    assert wf["steps"] == []
    assert {n["status"] for n in wf["nodes"]} == {"pending"}
    assert [i for i in client.get(f"/api/v1/incidents/{inc_id}/timeline").json() if i["kind"] == "agent_step"] == []


def test_workflow_steps_shape(client, hub_incident):
    wf = client.get(f"/api/v1/incidents/{hub_incident['id']}/workflow").json()
    assert [s["node_name"] for s in wf["steps"]] == NODE_IDS
    assert [s["seq"] for s in wf["steps"]] == list(range(1, 13))
    for step in wf["steps"]:
        assert set(step) == STEP_KEYS
        assert isinstance(step["tools_called"], list)
        assert all(isinstance(t, dict) for t in step["tools_called"])


def test_runs_item_shape(client, hub_incident):
    runs = client.get("/api/v1/runs").json()
    assert isinstance(runs, list) and runs
    run = runs[0]
    # Deliberately exact, stricter than the spec's superset rule: RunOut is what the run
    # drawer renders, so a new field must be re-baselined here (and in the UI) on purpose.
    assert set(run) == RUN_KEYS
    assert isinstance(run["steps"], list)
    assert set(run["steps"][0]) == STEP_KEYS

    one = client.get(f"/api/v1/runs/{run['id']}").json()
    assert set(one) == RUN_KEYS
    assert [s["node_name"] for s in one["steps"]] == NODE_IDS


def test_agents_catalog_unique_names(client):
    agents = client.get("/api/v1/agents").json()
    assert [(a["name"], a["mission"]) for a in agents] == AGENT_CATALOG
    assert len({a["name"] for a in agents}) == len(AGENT_CATALOG) == 12
    for a in agents:
        assert a["status"] == "ready"
        assert isinstance(a["mission"], str) and a["mission"]


def test_post_events_returns_incident_envelope(client):
    r = client.post("/api/v1/events", json=HUB_EVENT)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"incident"}
    inc = body["incident"]
    assert {
        "id",
        "incident_number",
        "priority",
        "responsible_msp",
        "msp_name",
        "radio_oem",
        "escalated_at",
        "expected_resolution_at",
        "failure_time",
        "requires_hitl",
        "hitl_state",
        "status",
    } <= set(inc)
    # Synchronous: the incident is already readable when the POST returns.
    assert client.get(f"/api/v1/incidents/{inc['id']}").json()["id"] == inc["id"]


def _receive_json(ws, timeout: float = 10.0) -> dict:
    """ws.receive_json() blocks forever when nothing arrives; fail fast instead of hanging CI."""
    box: list[dict] = []
    reader = threading.Thread(target=lambda: box.append(ws.receive_json()), daemon=True)
    reader.start()
    reader.join(timeout)
    if not box:
        raise AssertionError(f"no websocket message within {timeout}s")
    return box[0]


def test_ws_ops_replays_recent_and_delivers_live_events(client):
    """Stage C8: the WS subscriber lives on the app loop; a POST handled in a worker thread
    publishes through call_soon_threadsafe and the socket receives the run's events live."""
    envelope = {"type", "operator_id", "payload", "incident_id", "run_id", "ts"}
    with client.websocket_connect("/ws/ops") as ws:
        inc = client.post("/api/v1/events", json=HUB_EVENT).json()["incident"]
        seen: list[dict] = []
        for _ in range(80):  # replay (<=15) + one full run (~26 events); count- and time-bounded
            msg = _receive_json(ws)
            assert set(msg) == envelope
            seen.append(msg)
            if msg["type"] == "incident.created" and msg["incident_id"] == inc["id"]:
                break
        else:
            raise AssertionError(f"incident.created never arrived; got {[m['type'] for m in seen]}")
    live = [m for m in seen if m["run_id"] and m["incident_id"] == inc["id"]]
    assert [m["type"] for m in live][-2:] == ["agent.run.finished", "incident.created"]
    assert any(m["type"] == "agent.step.started" and m["payload"]["node"] == "TICKET" for m in seen)


def test_hitl_pending_item_shape(client, hub_incident):
    pending = client.get("/api/v1/hitl/pending").json()
    assert pending
    task = pending[0]
    assert {
        "id",
        "incident_id",
        "incident_number",
        "priority",
        "site_id",
        "task_type",
        "status",
        "claimed_by",
        "proposed_payload",
    } <= set(task)
    assert task["incident_id"] == hub_incident["id"]
    assert task["task_type"] == "APPROVE_BROADCAST"
    assert task["status"] == "PENDING"
    assert isinstance(task["proposed_payload"]["sms"], str)
