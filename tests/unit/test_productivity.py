"""``GET /api/v1/metrics/productivity`` — the rollup the Showcase page reads.

Three things are protected here, in the order they hurt when they break.

**1. The response shape.** The Showcase page is the one manager-facing screen, and it reads
every key below. Section 1 pins the key sets literally (not ``"x" in payload``), the node
order (the registry's), the agent order (the catalog's) and the timestamp spelling.

**2. The arithmetic is the model times the rows, nothing else.** Section 2 seeds three
alarms through the real lifecycle — a P2 HUB (held for approval), a P4 cell (auto-sent) and
the HUB again (a merge) — and checks every derived number against what those three runs
wrote: 26 steps, 2 tickets, 1 absorbed alarm, and a toil figure that is exactly
``2 × (sum of the model) + INGEST + CORRELATE``. The model is the profile's; a profile
override moves the figure and is echoed in ``assumptions``.

**3. The totals are one operator's.** Section 3 seeds the other operator's run, steps,
incident, approval and ledger row into the same database and asserts none of them reach
any number on the card — the aggregate carries no row id for a reviewer to notice is foreign.
"""

from __future__ import annotations

import importlib
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from noc_agents.api import auth
from noc_agents.db.models import (
    AgentRunRow,
    AgentRunStepRow,
    HitlTaskRow,
    IncidentRow,
    ShiftLedgerRow,
    get_session,
    new_id,
    utcnow,
)
from noc_agents.domain.schemas import EventIngest
from noc_agents.graph.pipeline import process_event
from noc_agents.orchestrator.registry import AGENT_PROFILES, NODE_CARDS
from noc_agents.orchestrator.runner import GRAPH_NAME
from noc_agents.realtime.hub import hub
from noc_agents.services import productivity as svc

HUB_EVENT = {  # P2 at L2_GUARDED: HITL and BROADCAST wait for a human
    "site_id": "SFC-NBIE-HUB-EMB",
    "site_name": "Embakasi East Aggregation HUB",
    "site_type": "HUB",
    "region_code": "NBI_E",
    "county": "Nairobi",
    "alarm_code": "POWER_GRID_FAIL",
    "failure_domain": "POWER",
    "users_affected": 450000,
}
CELL_EVENT = {  # P4: auto-sent, nothing held
    "site_id": "SFC-NBIW-ENB-CBD07",
    "site_name": "Upper Hill eNodeB 07",
    "site_type": "ENODEB",
    "region_code": "NBI_W",
    "alarm_code": "RADIO_CELL_DOWN",
    "failure_domain": "RADIO",
    "users_affected": 12000,
}

TIMESTAMP_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

TOP_KEYS = {
    "generated_at", "operator_id", "window_hours", "since", "alarms", "incidents", "steps",
    "pipeline_ms", "hitl", "broadcasts", "ticket_fields", "records", "toil", "agents",
}
ALARM_KEYS = {"processed", "incidents_created", "absorbed", "in_flight", "failed_runs", "noise_reduction_pct"}
INCIDENT_KEYS = {"created", "open", "closed", "by_priority"}
STEP_KEYS = {"total", "succeeded", "waiting_hitl", "failed", "by_node"}
NODE_KEYS = {"node", "label", "agent", "steps", "succeeded", "waiting_hitl", "failed", "avg_ms", "toil_minutes_each", "minutes_saved"}
PIPELINE_KEYS = {"runs_measured", "median", "p95", "max"}
HITL_KEYS = {"raised", "pending", "approved", "rejected", "median_decision_minutes"}
BROADCAST_KEYS = {"drafted", "sent", "held_for_approval", "queued", "suppressed", "failed", "by_channel"}
FIELD_KEYS = {"auto_filled", "per_incident", "tracked"}
RECORD_KEYS = {"ledger_rows", "exec_briefs", "problems_opened"}
TOIL_KEYS = {"model_version", "minutes_saved", "hours_saved", "human_minutes_spent", "net_minutes_saved", "minutes_saved_per_alarm", "assumptions"}
AGENT_KEYS = {"name", "mission", "nodes", "steps", "succeeded", "failed", "avg_ms", "max_ms", "last_step_at"}

MODEL_SUM = sum(svc.DEFAULT_TOIL_MINUTES.values())  # one full 12-node run, by hand
MERGE_SUM = svc.DEFAULT_TOIL_MINUTES["INGEST"] + svc.DEFAULT_TOIL_MINUTES["CORRELATE"]


def _seed(session, settings):
    hub_inc = process_event(session, settings, EventIngest(**HUB_EVENT))
    cell_inc = process_event(session, settings, EventIngest(**CELL_EVENT))
    merged = process_event(session, settings, EventIngest(**HUB_EVENT))
    assert merged.id == hub_inc.id, "the third alarm must merge into the open HUB major"
    return hub_inc, cell_inc


# ======================================================================================
# 1. shape
# ======================================================================================


def test_lifecycle_graph_literal_matches_the_runner():
    assert svc.LIFECYCLE_GRAPH == GRAPH_NAME


def test_response_shape_is_pinned(tmp_db):
    settings, session = tmp_db
    _seed(session, settings)
    out = svc.productivity(session, settings.operator)

    assert set(out) == TOP_KEYS
    assert set(out["alarms"]) == ALARM_KEYS
    assert set(out["incidents"]) == INCIDENT_KEYS
    assert set(out["steps"]) == STEP_KEYS
    assert set(out["pipeline_ms"]) == PIPELINE_KEYS
    assert set(out["hitl"]) == HITL_KEYS
    assert set(out["broadcasts"]) == BROADCAST_KEYS
    assert set(out["ticket_fields"]) == FIELD_KEYS
    assert set(out["records"]) == RECORD_KEYS
    assert set(out["toil"]) == TOIL_KEYS
    assert set(out["toil"]["assumptions"]) == {"toil_minutes", "human_minutes", "ignored_keys", "note"}
    assert out["toil"]["assumptions"]["ignored_keys"] == {"toil_minutes": [], "human_minutes": []}
    for node in out["steps"]["by_node"]:
        assert set(node) == NODE_KEYS
    for agent in out["agents"]:
        assert set(agent) == AGENT_KEYS

    # Orders are the registry's, so the UI can draw the rail and the roster without sorting.
    assert [n["node"] for n in out["steps"]["by_node"]] == [c.node_id for c in NODE_CARDS]
    assert [n["agent"] for n in out["steps"]["by_node"]] == [c.agent for c in NODE_CARDS]
    assert [a["name"] for a in out["agents"]] == [p.name for p in AGENT_PROFILES]
    assert out["operator_id"] == "safaricom"
    assert out["window_hours"] == svc.DEFAULT_WINDOW_HOURS
    assert TIMESTAMP_Z.match(out["generated_at"]) and TIMESTAMP_Z.match(out["since"])
    assert all(a["last_step_at"] is None or TIMESTAMP_Z.match(a["last_step_at"]) for a in out["agents"])
    assert out["ticket_fields"]["tracked"] == list(svc.AGENT_FILLED_FIELDS)


def test_an_empty_database_answers_with_zeros_not_errors(tmp_db):
    settings, session = tmp_db
    out = svc.productivity(session, settings.operator)
    assert out["alarms"] == {"processed": 0, "incidents_created": 0, "absorbed": 0, "in_flight": 0, "failed_runs": 0, "noise_reduction_pct": None}
    assert out["steps"]["total"] == 0 and all(n["steps"] == 0 for n in out["steps"]["by_node"])
    assert out["pipeline_ms"] == {"runs_measured": 0, "median": None, "p95": None, "max": None}
    assert out["toil"]["minutes_saved"] == 0.0 and out["toil"]["minutes_saved_per_alarm"] is None
    assert out["ticket_fields"]["per_incident"] is None
    assert len(out["agents"]) == len(AGENT_PROFILES)


# ======================================================================================
# 2. arithmetic
# ======================================================================================


def test_counts_are_what_three_alarms_wrote(tmp_db):
    settings, session = tmp_db
    hub_inc, cell_inc = _seed(session, settings)
    out = svc.productivity(session, settings.operator)

    assert out["alarms"] == {"processed": 3, "incidents_created": 2, "absorbed": 1, "in_flight": 0, "failed_runs": 0, "noise_reduction_pct": 33.3}
    assert out["incidents"]["created"] == 2 and out["incidents"]["open"] == 2 and out["incidents"]["closed"] == 0
    assert out["incidents"]["by_priority"] == {"P1": 0, "P2": 1, "P3": 0, "P4": 1}

    # 12 + 12 + 2 steps; the HUB run holds HITL and BROADCAST, the cell run completes them.
    assert out["steps"]["total"] == 26
    assert out["steps"]["waiting_hitl"] == 2 and out["steps"]["failed"] == 0
    assert out["steps"]["succeeded"] == 24
    by_node = {n["node"]: n for n in out["steps"]["by_node"]}
    assert by_node["INGEST"]["steps"] == 3 and by_node["CORRELATE"]["steps"] == 3
    assert by_node["TICKET"]["steps"] == 2 and by_node["MONITOR"]["steps"] == 2
    assert by_node["HITL"] == {**by_node["HITL"], "steps": 2, "succeeded": 1, "waiting_hitl": 1, "failed": 0}
    assert by_node["BROADCAST"]["waiting_hitl"] == 1 and by_node["BROADCAST"]["succeeded"] == 1

    assert out["pipeline_ms"]["runs_measured"] == 2  # the merge run opened no ticket
    assert out["pipeline_ms"]["median"] is not None and out["pipeline_ms"]["max"] >= out["pipeline_ms"]["median"]

    assert out["hitl"] == {"raised": 1, "pending": 1, "approved": 0, "rejected": 0, "median_decision_minutes": None}
    assert out["broadcasts"]["drafted"] >= 2
    assert out["broadcasts"]["held_for_approval"] >= 1, "the P2's drafts wait for a human"
    assert out["broadcasts"]["sent"] >= 1, "the P4's e-mail leaves (mock) under L2_GUARDED"
    assert out["broadcasts"]["drafted"] == sum(out["broadcasts"]["by_channel"].values())

    def filled(value) -> bool:  # the rule the SQL expression implements: a value a person would have typed
        return value is not None and (not isinstance(value, str) or value.strip() != "")

    expected_fields = sum(1 for inc in (hub_inc, cell_inc) for f in svc.AGENT_FILLED_FIELDS if filled(getattr(inc, f)))
    assert out["ticket_fields"]["auto_filled"] == expected_fields
    assert out["ticket_fields"]["per_incident"] >= 20, "the agents fill the ticket, not a stub of it"
    assert out["records"] == {"ledger_rows": 2, "exec_briefs": 2, "problems_opened": 0}

    agents = {a["name"]: a for a in out["agents"]}
    assert agents["IngestCorrelationAgent"]["steps"] == 6  # INGEST + CORRELATE on three runs
    assert agents["TicketingAgent"]["steps"] == 2 and agents["TicketingAgent"]["nodes"] == ["TICKET"]
    assert agents["ShiftHandoverAgent"]["steps"] == 0 and agents["ShiftHandoverAgent"]["last_step_at"] is None
    assert agents["SupervisorAgent"]["succeeded"] == 2, "a WAITING_HITL gate is work done"


def test_toil_is_the_model_times_the_steps_and_a_decision_is_charged_back(tmp_db):
    settings, session = tmp_db
    _seed(session, settings)
    out = svc.productivity(session, settings.operator)

    expected = 2 * MODEL_SUM + MERGE_SUM
    assert out["toil"]["minutes_saved"] == pytest.approx(expected)
    assert out["toil"]["hours_saved"] == pytest.approx(round(expected / 60, 2))
    assert out["toil"]["minutes_saved"] == pytest.approx(sum(n["minutes_saved"] for n in out["steps"]["by_node"]))
    assert out["toil"]["human_minutes_spent"] == 0.0 and out["toil"]["net_minutes_saved"] == pytest.approx(expected)
    assert out["toil"]["minutes_saved_per_alarm"] == pytest.approx(round(expected / 3, 1))
    assert out["toil"]["assumptions"]["toil_minutes"] == svc.DEFAULT_TOIL_MINUTES
    assert out["toil"]["assumptions"]["human_minutes"] == svc.DEFAULT_HUMAN_MINUTES
    assert out["toil"]["model_version"] == svc.TOIL_MODEL_VERSION

    # A human decides the held card three minutes after it was raised.
    task = session.scalar(select(HitlTaskRow))
    task.status = "APPROVED"
    task.resolved_by = "Grace Wanjiru"
    task.resolved_at = task.created_at + timedelta(minutes=3)
    session.commit()
    out = svc.productivity(session, settings.operator)
    assert out["hitl"] == {"raised": 1, "pending": 0, "approved": 1, "rejected": 0, "median_decision_minutes": 3.0}
    assert out["toil"]["human_minutes_spent"] == svc.DEFAULT_HUMAN_MINUTES["hitl_decision"]
    assert out["toil"]["net_minutes_saved"] == pytest.approx(expected - svc.DEFAULT_HUMAN_MINUTES["hitl_decision"])


def test_the_profile_overrides_the_model_key_by_key(tmp_db):
    settings, session = tmp_db
    _seed(session, settings)
    cfg = settings.operator.model_copy(deep=True)
    cfg.productivity.toil_minutes = {"ticket": 20}  # case-insensitive key; the rest stay default
    cfg.productivity.human_minutes = {"hitl_decision": 5}

    out = svc.productivity(session, cfg)
    assert out["toil"]["assumptions"]["toil_minutes"]["TICKET"] == 20.0
    assert out["toil"]["assumptions"]["toil_minutes"]["ENRICH"] == svc.DEFAULT_TOIL_MINUTES["ENRICH"]
    assert out["toil"]["assumptions"]["human_minutes"] == {"hitl_decision": 5.0}
    assert out["toil"]["assumptions"]["ignored_keys"] == {"toil_minutes": [], "human_minutes": []}
    assert out["toil"]["minutes_saved"] == pytest.approx(2 * (MODEL_SUM - svc.DEFAULT_TOIL_MINUTES["TICKET"] + 20) + MERGE_SUM)


def test_the_window_is_honoured_and_zero_means_everything(tmp_db):
    settings, session = tmp_db
    _seed(session, settings)
    later = utcnow() + timedelta(hours=48)

    out = svc.productivity(session, settings.operator, now=later, window_hours=24)
    assert out["alarms"]["processed"] == 0 and out["incidents"]["created"] == 0
    assert out["hitl"]["raised"] == 0 and out["records"]["ledger_rows"] == 0
    assert out["toil"]["minutes_saved"] == 0.0

    out = svc.productivity(session, settings.operator, now=later, window_hours=0)
    assert out["since"] is None and out["window_hours"] == 0
    assert out["alarms"]["processed"] == 3 and out["incidents"]["created"] == 2

    out = svc.productivity(session, settings.operator, window_hours=10 ** 9)
    assert out["window_hours"] == svc.MAX_WINDOW_HOURS


def test_a_failed_run_is_counted_and_credited_nothing(tmp_db):
    settings, session = tmp_db
    _seed(session, settings)
    before = svc.productivity(session, settings.operator)
    run = AgentRunRow(
        id=new_id(), operator_id="safaricom", graph_name=svc.LIFECYCLE_GRAPH, trigger="EVENT",
        status="FAILED", started_at=utcnow(), finished_at=utcnow(), current_node="ENRICH", error_summary="boom",
    )
    session.add(run)
    session.flush()
    session.add(AgentRunStepRow(
        id=new_id(), run_id=run.id, seq=1, node_name="ENRICH", agent_name="EnrichmentAgent", status="FAILED",
        started_at=utcnow(), finished_at=utcnow(), duration_ms=3, input_summary="", output_summary="", rationale="boom",
        tools_called=[], confidence=None,
    ))
    session.commit()

    out = svc.productivity(session, settings.operator)
    assert out["alarms"]["processed"] == 4 and out["alarms"]["failed_runs"] == 1
    assert out["alarms"]["incidents_created"] == 2 and out["alarms"]["absorbed"] == 1
    assert out["steps"]["failed"] == 1
    assert out["toil"]["minutes_saved"] == before["toil"]["minutes_saved"]
    assert {a["name"]: a["failed"] for a in out["agents"]}["EnrichmentAgent"] == 1


def test_an_unknown_profile_key_is_reported_not_silently_dropped(tmp_db, caplog):
    settings, session = tmp_db
    _seed(session, settings)
    cfg = settings.operator.model_copy(deep=True)
    cfg.productivity.toil_minutes = {"EXEC_BRIEFING": 15}  # a typo for EXEC_BRIEF
    cfg.productivity.human_minutes = {"approval": 9}  # not an action the model knows

    with caplog.at_level("WARNING", logger="noc_agents.services.productivity"):
        out = svc.productivity(session, cfg)
    assert out["toil"]["assumptions"]["ignored_keys"] == {"toil_minutes": ["EXEC_BRIEFING"], "human_minutes": ["approval"]}
    assert out["toil"]["assumptions"]["toil_minutes"]["EXEC_BRIEF"] == svc.DEFAULT_TOIL_MINUTES["EXEC_BRIEF"]
    assert out["toil"]["minutes_saved"] == pytest.approx(2 * MODEL_SUM + MERGE_SUM)
    assert any("unknown keys" in r.getMessage() for r in caplog.records)


def test_a_run_still_in_flight_is_neither_a_ticket_nor_a_duplicate(tmp_db):
    settings, session = tmp_db
    _seed(session, settings)
    before = svc.productivity(session, settings.operator)
    run = AgentRunRow(
        id=new_id(), operator_id="safaricom", graph_name=svc.LIFECYCLE_GRAPH, trigger="EVENT",
        status="RUNNING", started_at=utcnow(), current_node="ENRICH",
    )
    session.add(run)
    session.flush()
    for seq, (node, agent, status) in enumerate(
        [("INGEST", "IngestCorrelationAgent", "SUCCEEDED"), ("CORRELATE", "IngestCorrelationAgent", "SUCCEEDED"),
         ("ENRICH", "EnrichmentAgent", "STARTED")],
        start=1,
    ):
        session.add(AgentRunStepRow(
            id=new_id(), run_id=run.id, seq=seq, node_name=node, agent_name=agent, status=status,
            started_at=utcnow(), finished_at=utcnow() if status != "STARTED" else None,
            duration_ms=1 if status != "STARTED" else None, input_summary="", output_summary="", rationale="",
            tools_called=[], confidence=0.9,
        ))
    session.commit()

    out = svc.productivity(session, settings.operator)
    assert out["alarms"]["processed"] == 4 and out["alarms"]["in_flight"] == 1
    assert out["alarms"]["absorbed"] == 1 and out["alarms"]["incidents_created"] == 2
    assert out["alarms"]["noise_reduction_pct"] == 33.3, "an undecided run must not move the duplicate share"
    # Per alarm that has run: its two finished hops count, and so does the alarm itself.
    assert out["toil"]["minutes_saved_per_alarm"] == pytest.approx(round((before["toil"]["minutes_saved"] + MERGE_SUM) / 4, 1))
    assert out["steps"]["total"] == before["steps"]["total"] + 3
    # Its two finished hops are work done; its started hop is not.
    assert out["toil"]["minutes_saved"] == pytest.approx(before["toil"]["minutes_saved"] + MERGE_SUM)


# ======================================================================================
# 3. one operator's rows
# ======================================================================================


def test_the_other_operators_rows_reach_no_number(tmp_db):
    settings, session = tmp_db
    _seed(session, settings)
    before = svc.productivity(session, settings.operator)

    other_inc = IncidentRow(
        operator_id="airtel", incident_number="ATL-INC-1", site_id="ATL-NBI-HUB-001", region_code="NBI",
        correlation_fingerprint="fp", priority="P1", title="theirs", narrative="theirs",
    )
    session.add(other_inc)
    session.flush()
    run = AgentRunRow(
        id=new_id(), incident_id=other_inc.id, operator_id="airtel", graph_name=svc.LIFECYCLE_GRAPH,
        trigger="EVENT", status="SUCCEEDED", started_at=utcnow(), finished_at=utcnow(),
    )
    session.add(run)
    session.flush()
    for seq, card in enumerate(NODE_CARDS, start=1):
        session.add(AgentRunStepRow(
            id=new_id(), run_id=run.id, seq=seq, node_name=card.node_id, agent_name=card.agent, status="SUCCEEDED",
            started_at=utcnow(), finished_at=utcnow(), duration_ms=1, input_summary="", output_summary="",
            rationale="", tools_called=[], confidence=0.9,
        ))
    session.add(HitlTaskRow(operator_id="airtel", incident_id=None, task_type="APPROVE_BROADCAST", status="PENDING"))
    session.add(ShiftLedgerRow(
        operator_id="airtel", shift_id="x", shift_type="DAY", incident_number="ATL-INC-1", priority="P1",
        site="x", site_type="HUB", region_code="NBI", status="NEW",
    ))
    session.commit()

    after = svc.productivity(session, settings.operator)
    after["generated_at"] = before["generated_at"]
    after["since"] = before["since"]
    assert after == before


# ======================================================================================
# 4. over HTTP
# ======================================================================================


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "productivity.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")

    import noc_agents.config as cfg
    import noc_agents.db.models as models
    import noc_agents.main as main

    cfg.clear_settings_cache()
    models._engine = None
    models.SessionLocal = None
    importlib.reload(main)
    auth.reset_sessions()
    hub._history.clear()
    c = TestClient(main.app)
    c.__enter__()
    try:
        yield c
    finally:
        c.__exit__(None, None, None)
        hub._history.clear()
        auth.reset_sessions()
        models._engine = None
        models.SessionLocal = None
        cfg.clear_settings_cache()
        importlib.reload(main)


def test_the_route_serves_the_rollup_and_validates_the_window(client):
    assert client.post("/api/v1/events", json=HUB_EVENT).status_code == 200
    r = client.get("/api/v1/metrics/productivity")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == TOP_KEYS
    assert body["alarms"]["processed"] == 1 and body["window_hours"] == svc.DEFAULT_WINDOW_HOURS
    assert body["toil"]["minutes_saved"] == pytest.approx(MODEL_SUM)

    assert client.get("/api/v1/metrics/productivity?window_hours=0").json()["since"] is None
    assert client.get("/api/v1/metrics/productivity?window_hours=-1").status_code == 422
    assert client.get(f"/api/v1/metrics/productivity?window_hours={svc.MAX_WINDOW_HOURS + 1}").status_code == 422

    session = get_session()
    try:  # the route wrote nothing
        assert session.scalar(select(AgentRunRow).where(AgentRunRow.graph_name != svc.LIFECYCLE_GRAPH)) is None
    finally:
        session.close()
