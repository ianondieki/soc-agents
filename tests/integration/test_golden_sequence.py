"""Golden event/DB sequence for one alarm through process_event.

Pins what an operator and the UI actually observe: the order and payloads of the
realtime events, the step rows (input/output/rationale/tools/confidence), the run
row, the audit rows and the side tables. Ids, timestamps and durations are ignored;
everything else is literal, so any change to node order, agent names, statuses,
payload keys or commit-visible results fails here.

Durability is checked through a SECOND Session on the same DATABASE_URL: the writing
Session sees its own uncommitted rows, so reading back through it would not notice a
dropped or mis-ordered session.commit().
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from sqlalchemy import select

from noc_agents.db.models import (
    AgentRunRow,
    AuditRow,
    BroadcastRow,
    HitlTaskRow,
    IncidentBriefRow,
    IncidentRow,
    ShiftLedgerRow,
    WorkNoteRow,
    get_session,
    new_id,
    utcnow,
)
from noc_agents.domain.schemas import EventIngest
from noc_agents.graph.instrumentation import RunTracker
from noc_agents.graph.pipeline import process_event
from noc_agents.realtime.hub import EventHub, hub

ENVELOPE_KEYS = {"type", "operator_id", "payload", "incident_id", "run_id", "ts"}
PAYLOAD_KEYS = {
    "agent.step.started": {"seq", "incident_number", "run_id", "node", "agent", "input"},
    "agent.step.completed": {
        "seq",
        "incident_number",
        "run_id",
        "node",
        "agent",
        "status",
        "rationale",
        "output",
        "duration_ms",
    },
    "agent.run.finished": {"seq", "incident_number", "run_id", "status", "error"},
    "incident.created": {"incident_number", "priority", "site_id", "region_code", "requires_hitl"},
    "incident.merged": {"incident_number"},
    "incident.cascade_child": {"parent", "child_site", "child_sites_down"},
    "email.sent": {"incident_number", "mode", "to", "detail", "status"},
}

HUB_EVENT = dict(
    site_id="SFC-NBIE-HUB-EMB",
    site_name="Embakasi East Aggregation HUB",
    site_type="HUB",
    region_code="NBI_E",
    alarm_code="POWER_GRID_FAIL",
    failure_domain="POWER",
    users_affected=450000,
    access_notes="Genset not started",
)
BTS_EVENT = dict(
    site_id="SFC-MTK-BTS-MCH04",
    site_name="Machakos Town BTS",
    site_type="BTS",
    region_code="MTK",
    alarm_code="SITE_DOWN",
    failure_domain="POWER",
    users_affected=3200,
)
PARENT_HUB_EVENT = dict(
    site_id="SFC-NBIW-HUB-WLG",
    site_name="Westlands Aggregation HUB",
    site_type="HUB",
    region_code="NBI_W",
    alarm_code="POWER_GRID_FAIL",
    failure_domain="POWER",
    users_affected=400000,
)
CHILD_EVENT = dict(
    site_id="SFC-NBIW-ENB-CBD07",
    site_type="ENODEB",
    region_code="NBI_W",
    alarm_code="SITE_DOWN",
    failure_domain="POWER",
    users_affected=9000,
    parent_hub_id="SFC-NBIW-HUB-WLG",
)

# (type, seq, node, agent, status, incident_number, incident_id_is_set)
GOLDEN_FULL_HITL = [
    ("agent.step.started", 1, "INGEST", "IngestCorrelationAgent", None, None, False),
    ("agent.step.completed", 1, "INGEST", "IngestCorrelationAgent", "SUCCEEDED", None, False),
    ("agent.step.started", 2, "CORRELATE", "IngestCorrelationAgent", None, None, False),
    ("agent.step.completed", 2, "CORRELATE", "IngestCorrelationAgent", "SUCCEEDED", None, False),
    ("agent.step.started", 3, "ENRICH", "EnrichmentAgent", None, None, False),
    ("agent.step.completed", 3, "ENRICH", "EnrichmentAgent", "SUCCEEDED", None, False),
    ("agent.step.started", 4, "SEVERITY", "SeverityImpactAgent", None, None, False),
    ("agent.step.completed", 4, "SEVERITY", "SeverityImpactAgent", "SUCCEEDED", None, False),
    ("agent.step.started", 5, "TICKET", "TicketingAgent", None, None, False),
    ("agent.step.completed", 5, "TICKET", "TicketingAgent", "SUCCEEDED", "INC000001", True),
    ("agent.step.started", 6, "ASSIGN", "DispatchAssignmentAgent", None, "INC000001", True),
    ("agent.step.completed", 6, "ASSIGN", "DispatchAssignmentAgent", "SUCCEEDED", "INC000001", True),
    ("agent.step.started", 7, "HITL", "SupervisorAgent", None, "INC000001", True),
    ("agent.step.completed", 7, "HITL", "SupervisorAgent", "WAITING_HITL", "INC000001", True),
    ("agent.step.started", 8, "BROADCAST", "BroadcastCommsAgent", None, "INC000001", True),
    ("agent.step.completed", 8, "BROADCAST", "BroadcastCommsAgent", "WAITING_HITL", "INC000001", True),
    ("agent.step.started", 9, "EXEC_BRIEF", "ExecutiveBriefingAgent", None, "INC000001", True),
    ("agent.step.completed", 9, "EXEC_BRIEF", "ExecutiveBriefingAgent", "SUCCEEDED", "INC000001", True),
    ("agent.step.started", 10, "LEDGER", "ShiftLedgerAgent", None, "INC000001", True),
    ("agent.step.completed", 10, "LEDGER", "ShiftLedgerAgent", "SUCCEEDED", "INC000001", True),
    ("agent.step.started", 11, "RECURRENCE", "RecurrenceProblemAgent", None, "INC000001", True),
    ("agent.step.completed", 11, "RECURRENCE", "RecurrenceProblemAgent", "SUCCEEDED", "INC000001", True),
    ("agent.step.started", 12, "MONITOR", "WorklogMonitorAgent", None, "INC000001", True),
    ("agent.step.completed", 12, "MONITOR", "WorklogMonitorAgent", "SUCCEEDED", "INC000001", True),
    ("agent.run.finished", 12, None, None, "WAITING_HITL", "INC000001", True),
    ("incident.created", None, None, None, None, "INC000001", True),
]
# P4 auto-broadcast: identical to the HITL run except HITL/BROADCAST/run status are SUCCEEDED.
GOLDEN_FULL_AUTO = [
    (t, seq, node, agent, "SUCCEEDED" if status == "WAITING_HITL" else status, num, has_id)
    for (t, seq, node, agent, status, num, has_id) in GOLDEN_FULL_HITL
]
GOLDEN_MERGE = [
    ("agent.step.started", 1, "INGEST", "IngestCorrelationAgent", None, None, False),
    ("agent.step.completed", 1, "INGEST", "IngestCorrelationAgent", "SUCCEEDED", None, False),
    ("agent.step.started", 2, "CORRELATE", "IngestCorrelationAgent", None, None, False),
    ("agent.step.completed", 2, "CORRELATE", "IngestCorrelationAgent", "SUCCEEDED", None, False),
    ("agent.run.finished", 2, None, None, "SUCCEEDED", None, True),
    ("incident.merged", None, None, None, None, "INC000001", True),
]
# Same two steps + run.finished as the merge run; only the terminal event differs
# (GOLDEN_MERGE[:-1] drops its incident.merged).
GOLDEN_CASCADE = GOLDEN_MERGE[:-1] + [
    ("incident.cascade_child", None, None, None, None, None, True),
]

HITL_INPUT_SUMMARIES = [
    "site=SFC-NBIE-HUB-EMB",
    "fp=SFC-NBIE-HUB-EMB|POWER_GRID_FAIL|POWER",
    "SFC-NBIE-HUB-EMB",
    "users=450000",
    "P2",
    "POWER",
    "L2_GUARDED",
    "channels=SMS,EMAIL",
    "P2",
    "excel",
    "SFC-NBIE-HUB-EMB|POWER_GRID_FAIL|POWER",
    "sla_watch",
]
AUTO_INPUT_SUMMARIES = [
    "site=SFC-MTK-BTS-MCH04",
    "fp=SFC-MTK-BTS-MCH04|SITE_DOWN|POWER",
    "SFC-MTK-BTS-MCH04",
    "users=3200",
    "P4",
    "POWER",
    "L2_GUARDED",
    "channels=SMS,EMAIL",
    "P4",
    "excel",
    "SFC-MTK-BTS-MCH04|SITE_DOWN|POWER",
    "sla_watch",
]

# node -> tools_called, exactly as persisted
TOOLS_SHARED = {
    "INGEST": [{"name": "normalize_event", "ok": True, "latency_ms": 1}],
    "ENRICH": [
        {"name": "lookup_site", "ok": True, "latency_ms": 3},
        {"name": "estimate_users_affected", "ok": True, "latency_ms": 1},
        {"name": "classify_tt", "ok": True, "latency_ms": 1},
    ],
    "SEVERITY": [{"name": "priority_engine", "ok": True, "latency_ms": 1}],
    "TICKET": [{"name": "create_incident", "ok": True, "latency_ms": 5}],
    "ASSIGN": [{"name": "assign_incident", "ok": True, "latency_ms": 2}],
    "EXEC_BRIEF": [{"name": "upsert_status_brief", "ok": True, "latency_ms": 2}],
    "LEDGER": [{"name": "outbox.enqueue", "ok": True, "latency_ms": 2, "error": None}],
    "RECURRENCE": [{"name": "count_recurrence", "ok": True, "latency_ms": 2}],
    "MONITOR": [{"name": "flag_sla_watch", "ok": True, "latency_ms": 1}],
}
TOOLS_HITL_RUN = {
    **TOOLS_SHARED,
    "CORRELATE": [{"name": "find_open_by_fingerprint", "ok": True, "latency_ms": 2}],
    "HITL": [{"name": "create_hitl_task", "ok": True, "latency_ms": 1}],
    "BROADCAST": [{"name": "draft_broadcast", "ok": True, "latency_ms": 2}],
}
TOOLS_AUTO_RUN = {
    **TOOLS_SHARED,
    "CORRELATE": [{"name": "find_open_by_fingerprint", "ok": True, "latency_ms": 2}],
    "HITL": [],
    "BROADCAST": [
        {"name": "render_sms", "ok": True, "latency_ms": 1},
        {"name": "render_email", "ok": True, "latency_ms": 1},
        {"name": "outbox.enqueue", "ok": True, "latency_ms": 2, "error": None},
    ],
}
CONFIDENCE = {"ENRICH": 0.85, "SEVERITY": 0.95}

LEDGER_FILE_RE = re.compile(r"^ledger_\d{4}-\d{2}-\d{2}_(DAY|NIGHT)\.xlsx$")
HITL_TASK_RE = re.compile(r"^HITL task [0-9a-f-]{36}$")


@pytest.fixture()
def clean_hub():
    """Make the measured window explicit; hub._history is a shared deque(maxlen=100)."""
    hub._history.clear()
    yield hub
    hub._history.clear()


def _history() -> list[dict]:
    return list(hub._history)


def _run_events(run_id: str) -> list[dict]:
    return [e for e in _history() if e.get("run_id") == run_id]


def _shape(event: dict) -> tuple:
    p = event["payload"]
    return (
        event["type"],
        p.get("seq"),
        p.get("node"),
        p.get("agent"),
        p.get("status"),
        p.get("incident_number"),
        event["incident_id"] is not None,
    )


def _check_envelopes(events: list[dict]) -> None:
    for e in events:
        assert set(e) == ENVELOPE_KEYS, e["type"]
        assert set(e["payload"]) == PAYLOAD_KEYS[e["type"]], e["type"]
        assert isinstance(e["ts"], str) and e["ts"].endswith("Z")


def _run_ids(session) -> set[str]:
    return {r.id for r in session.scalars(select(AgentRunRow)).all()}


def _audit_ids(session) -> set[str]:
    return {a.id for a in session.scalars(select(AuditRow)).all()}


def _note_ids(session) -> set[str]:
    return {n.id for n in session.scalars(select(WorkNoteRow)).all()}


def _new_audit(session, before: set[str]) -> list[tuple[str, str, str]]:
    rows = [a for a in session.scalars(select(AuditRow)).all() if a.id not in before]
    return sorted((a.actor, a.action, a.entity_id) for a in rows)


def _new_notes(session, before: set[str]) -> list[WorkNoteRow]:
    return [n for n in session.scalars(select(WorkNoteRow)).all() if n.id not in before]


def _in_other_session(read):
    """Run `read(session)` on a fresh Session — the writing Session's uncommitted rows are invisible."""
    other = get_session()
    try:
        return read(other)
    finally:
        other.close()


def _visible_in_other_session(incident_id: str | None) -> bool:
    """Read the incident through a fresh Session — uncommitted rows are invisible there."""
    if incident_id is None:
        return False
    return _in_other_session(lambda s: s.get(IncidentRow, incident_id)) is not None


def _the_run(session, before: set[str]) -> AgentRunRow:
    new = _run_ids(session) - before
    assert len(new) == 1
    return session.get(AgentRunRow, new.pop())


def _check_steps_against_events(run: AgentRunRow, events: list[dict]) -> None:
    """Each WS step payload mirrors its step row: input[:120], output[:160], full rationale."""
    started = {e["payload"]["seq"]: e["payload"] for e in events if e["type"] == "agent.step.started"}
    completed = {e["payload"]["seq"]: e["payload"] for e in events if e["type"] == "agent.step.completed"}
    for s in run.steps:
        assert started[s.seq]["input"] == (s.input_summary or "")[:120], s.node_name
        assert completed[s.seq]["output"] == (s.output_summary or "")[:160], s.node_name
        assert completed[s.seq]["rationale"] == (s.rationale or ""), s.node_name


def test_golden_full_lifecycle_with_hitl(tmp_db, clean_hub, monkeypatch):
    settings, session = tmp_db
    audit_before, run_before = _audit_ids(session), _run_ids(session)

    # R5 (§2.1, §7.0.4): EVERY event of the run announces a durable row. The commit must come
    # first, so when an event is published its row is already readable from a second,
    # independent Session: a step event finds its step row (and, once completed, the announced
    # status), agent.run.finished finds the run row in the announced status, incident.created
    # finds the incident. Before §7.0.4 only incident.created could pass this check — the
    # agent.* events were published from inside the open transaction.
    # Patched on the class, not on the `hub` singleton: monkeypatch undoes an instance
    # patch by re-setting the attribute, which would leave a shadowing entry in hub.__dict__.
    announced: list[tuple[str | None, str, bool]] = []  # (run_id, type, durable at announce)
    publish_sync = EventHub.publish_sync

    def durable_at_announce(event) -> bool:
        p = event.payload
        if event.type == "incident.created":
            return _visible_in_other_session(event.incident_id)

        def read(s) -> bool:
            run = s.get(AgentRunRow, event.run_id)
            if run is None:
                return False
            if event.type == "agent.run.finished":
                return run.status == p["status"]
            step = next((x for x in run.steps if x.seq == p["seq"]), None)
            if step is None:
                return False
            return event.type == "agent.step.started" or step.status == p["status"]

        return _in_other_session(read)

    def spy(self, event):
        announced.append((event.run_id, event.type, durable_at_announce(event)))
        publish_sync(self, event)

    monkeypatch.setattr(EventHub, "publish_sync", spy)

    inc = process_event(session, settings, EventIngest(**HUB_EVENT))
    run = _the_run(session, run_before)
    events = _run_events(run.id)

    seen = [(t, durable) for (rid, t, durable) in announced if rid == run.id]
    assert [t for t, _ in seen] == [t for (t, *_rest) in GOLDEN_FULL_HITL]  # all 26, announced once each
    assert all(durable for _, durable in seen), [t for t, durable in seen if not durable]
    assert _visible_in_other_session(inc.id)  # final commit reached the database

    assert [_shape(e) for e in events] == GOLDEN_FULL_HITL
    _check_envelopes(events)
    assert events[-1]["payload"] == {
        "incident_number": "INC000001",
        "priority": "P2",
        "site_id": "SFC-NBIE-HUB-EMB",
        "region_code": "NBI_E",
        "requires_hitl": True,
    }
    assert events[-2]["payload"]["error"] is None

    # --- run + step rows ---
    assert (run.status, run.current_node, run.error_summary) == ("WAITING_HITL", "MONITOR", None)
    assert run.incident_id == inc.id
    assert [(s.seq, s.node_name, s.agent_name, s.status) for s in run.steps] == [
        (seq, node, agent, status)
        for (t, seq, node, agent, status, _n, _i) in GOLDEN_FULL_HITL
        if t == "agent.step.completed"
    ]
    assert [s.input_summary for s in run.steps] == HITL_INPUT_SUMMARIES
    _check_steps_against_events(run, events)

    by_node = {s.node_name: s for s in run.steps}
    for node, tools in TOOLS_HITL_RUN.items():
        assert by_node[node].tools_called == tools, node
        assert by_node[node].confidence == CONFIDENCE.get(node, 0.9), node

    assert by_node["INGEST"].output_summary == (
        "normalized event fingerprint=SFC-NBIE-HUB-EMB|POWER_GRID_FAIL|POWER"
    )
    assert by_node["INGEST"].rationale == "Normalized alarm payload; ready for correlation window check"
    assert by_node["CORRELATE"].output_summary == "no open duplicate — new incident candidate"
    assert by_node["CORRELATE"].rationale == "Fingerprint unique in correlation window; no parent HUB merge"
    assert by_node["ENRICH"].output_summary == "users_est=450000, region=NBI_E, hub=True, class=CRITICAL"
    assert by_node["ENRICH"].rationale.startswith("CMDB/mock enrich: Nairobi East; FE on-call=FE-NBI-E-01; ")
    assert by_node["SEVERITY"].output_summary == "priority=P2 mpesa_risk=True"
    assert by_node["SEVERITY"].rationale.startswith(
        "operator=safaricom; users=450000→P2; site_type=HUB floor="
    )
    assert by_node["TICKET"].output_summary == "created INC000001 category=POWER_GRID"
    assert by_node["TICKET"].rationale == (
        "Unique incident number; TT fields filled as NOC UI would: "
        "category=POWER_GRID, class=CRITICAL, tech=4G; HUB auto-ticket=yes"
    )
    assert by_node["ASSIGN"].output_summary == "MSP:EGYPRO"
    assert by_node["ASSIGN"].rationale.startswith("region=NBI_E (")
    assert HITL_TASK_RE.match(by_node["HITL"].output_summary)
    assert by_node["HITL"].rationale == "P2 under L2_GUARDED requires human approval before external blast"
    assert by_node["BROADCAST"].output_summary == "broadcasts drafted; waiting HITL"
    assert by_node["BROADCAST"].rationale == (
        "External wording held for supervisor/duty manager — approve HITL to send Gmail"
    )
    assert by_node["EXEC_BRIEF"].output_summary == "exec brief published"
    assert by_node["EXEC_BRIEF"].rationale == "Proactive brief to reduce management call volume into NOC"
    assert LEDGER_FILE_RE.match(by_node["LEDGER"].output_summary)
    assert by_node["LEDGER"].rationale == "Shift failure ledger row appended for supervisor scan"
    assert by_node["RECURRENCE"].output_summary == "count=1 threshold=3"
    assert by_node["RECURRENCE"].rationale == "Below recurrence threshold — no problem record"
    assert by_node["MONITOR"].output_summary == "sla timers armed"
    assert by_node["MONITOR"].rationale == "Note-interval chase scheduled per priority/region multiplier"

    # --- audit rows: entity_id is "" until TICKET binds the incident ---
    assert _new_audit(session, audit_before) == sorted(
        [
            ("IngestCorrelationAgent", "step.succeeded", ""),
            ("IngestCorrelationAgent", "step.succeeded", ""),
            ("EnrichmentAgent", "step.succeeded", ""),
            ("SeverityImpactAgent", "step.succeeded", ""),
            ("TicketingAgent", "step.succeeded", inc.id),
            ("DispatchAssignmentAgent", "step.succeeded", inc.id),
            ("SupervisorAgent", "step.waiting_hitl", inc.id),
            ("BroadcastCommsAgent", "step.waiting_hitl", inc.id),
            ("ExecutiveBriefingAgent", "step.succeeded", inc.id),
            ("ShiftLedgerAgent", "step.succeeded", inc.id),
            ("RecurrenceProblemAgent", "step.succeeded", inc.id),
            ("WorklogMonitorAgent", "step.succeeded", inc.id),
        ]
    )

    # --- side tables ---
    tasks = session.scalars(select(HitlTaskRow)).all()
    assert [(t.task_type, t.status) for t in tasks] == [("APPROVE_BROADCAST", "PENDING")]
    assert isinstance(tasks[0].proposed_payload["sms"], str)
    broadcasts = session.scalars(select(BroadcastRow)).all()
    assert sorted((b.channel, b.audience, b.status) for b in broadcasts) == [
        ("EMAIL", "FIELD_ENGINEER", "PENDING_HITL"),
        ("EMAIL", "MANAGEMENT", "PENDING_HITL"),
        ("EMAIL", "MSP", "PENDING_HITL"),
        ("EMAIL", "RNIO", "PENDING_HITL"),
        ("SMS", "FIELD_ENGINEER", "PENDING_HITL"),
        ("SMS", "MANAGEMENT", "PENDING_HITL"),
        ("SMS", "MSP", "PENDING_HITL"),
        ("SMS", "RNIO", "PENDING_HITL"),
    ]
    assert len(session.scalars(select(IncidentBriefRow)).all()) == 1
    assert len(session.scalars(select(ShiftLedgerRow)).all()) == 1
    notes = session.scalars(select(WorkNoteRow)).all()
    assert [(n.author, n.source) for n in notes] == [("WorklogMonitorAgent", "agent")]
    assert notes[0].body.startswith("Monitoring started. SLA ack due ")

    # ledger written to the isolated LEDGER_DIR, never the project folder
    ledger_dir = Path(os.environ["LEDGER_DIR"]) / "safaricom"
    assert (ledger_dir / by_node["LEDGER"].output_summary).exists()


def test_golden_full_lifecycle_auto_broadcast(tmp_db, clean_hub):
    settings, session = tmp_db
    audit_before, run_before = _audit_ids(session), _run_ids(session)

    inc = process_event(session, settings, EventIngest(**BTS_EVENT))
    run = _the_run(session, run_before)
    events = _run_events(run.id)

    assert [_shape(e) for e in events] == GOLDEN_FULL_AUTO
    _check_envelopes(events)
    assert events[-1]["payload"] == {
        "incident_number": "INC000001",
        "priority": "P4",
        "site_id": "SFC-MTK-BTS-MCH04",
        "region_code": "MTK",
        "requires_hitl": False,
    }

    # email.sent carries no run_id, so it is located in the full history instead
    full = _history()
    emails = [e for e in full if e["type"] == "email.sent"]
    assert len(emails) == 1
    assert emails[0]["incident_id"] == inc.id
    assert emails[0]["payload"] == {
        "incident_number": "INC000001",
        "mode": "mock",
        "to": [],
        "detail": "No DEMO_EMAIL_TO / GMAIL_ADDRESS — email body stored only (mock)",
        "status": "SENT",
    }
    _check_envelopes(emails)
    # R4 (§2.1): the mock send runs in the post-commit outbox drain, so email.sent is published
    # after agent.run.finished — never from inside the BROADCAST node.
    run_finished = full.index(next(e for e in events if e["type"] == "agent.run.finished"))
    assert full.index(emails[0]) > run_finished

    assert (run.status, run.current_node, run.error_summary) == ("SUCCEEDED", None, None)
    assert run.incident_id == inc.id
    assert [s.input_summary for s in run.steps] == AUTO_INPUT_SUMMARIES
    _check_steps_against_events(run, events)

    by_node = {s.node_name: s for s in run.steps}
    for node, tools in TOOLS_AUTO_RUN.items():
        assert by_node[node].tools_called == tools, node
        assert by_node[node].confidence == CONFIDENCE.get(node, 0.9), node

    assert by_node["HITL"].output_summary == "auto-approved under autonomy policy"
    assert by_node["HITL"].rationale == "P4 allowed auto-broadcast at L2_GUARDED"
    assert by_node["BROADCAST"].output_summary == (
        "queued 3 outbox rows for ['RNIO', 'FIELD_ENGINEER']; email=PENDING"
    )
    assert by_node["BROADCAST"].rationale == (
        "P4 auto-send under L2_GUARDED (approved_by=policy:L2_GUARDED); dispatcher transmits after commit"
    )

    assert _new_audit(session, audit_before) == sorted(
        [
            ("IngestCorrelationAgent", "step.succeeded", ""),
            ("IngestCorrelationAgent", "step.succeeded", ""),
            ("EnrichmentAgent", "step.succeeded", ""),
            ("SeverityImpactAgent", "step.succeeded", ""),
            ("TicketingAgent", "step.succeeded", inc.id),
            ("DispatchAssignmentAgent", "step.succeeded", inc.id),
            ("SupervisorAgent", "step.succeeded", inc.id),
            ("BroadcastCommsAgent", "step.succeeded", inc.id),
            ("ExecutiveBriefingAgent", "step.succeeded", inc.id),
            ("ShiftLedgerAgent", "step.succeeded", inc.id),
            ("RecurrenceProblemAgent", "step.succeeded", inc.id),
            ("WorklogMonitorAgent", "step.succeeded", inc.id),
        ]
    )

    assert not session.scalars(select(HitlTaskRow)).all()
    broadcasts = session.scalars(select(BroadcastRow)).all()
    assert sorted((b.channel, b.audience, b.status) for b in broadcasts) == [
        ("EMAIL", "FIELD_ENGINEER", "SENT"),
        ("EMAIL", "RNIO", "SENT"),
        ("SMS", "FIELD_ENGINEER", "SENT"),
        ("SMS", "RNIO", "SENT"),
    ]
    notes = session.scalars(select(WorkNoteRow)).all()
    assert [(n.author, n.source) for n in notes] == [
        ("WorklogMonitorAgent", "agent"),
        ("BroadcastCommsAgent", "email"),  # R4: written by the post-commit drain, after the MONITOR note
    ]


def test_step_payload_truncation_boundaries(tmp_db, clean_hub):
    """Real pipeline summaries are short, so the WS caps are exercised directly here."""
    settings, session = tmp_db
    long_input = "site=" + "X" * 200
    long_output = "out=" + "Y" * 300
    long_rationale = "because " + "Z" * 300

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
    tracker = RunTracker(session, run)
    step = tracker.start_step("INGEST", "IngestCorrelationAgent", long_input)
    tracker.complete_step(step, output_summary=long_output, rationale=long_rationale)
    # Phase 1 §7.0.4 (addendum A6): events are buffered on the session and flushed after
    # commit, so this test — which drives RunTracker directly rather than through the
    # runner — must commit before the events exist. The truncation assertions below are
    # unchanged; only the mechanics of when the events are published moved.
    session.commit()

    started, completed = _run_events(run.id)
    _check_envelopes([started, completed])
    assert started["payload"]["input"] == long_input[:120] != long_input
    assert completed["payload"]["output"] == long_output[:160] != long_output
    assert completed["payload"]["rationale"] == long_rationale  # rationale is never truncated
    # The step row keeps the full text; only the broadcast payload is capped.
    assert (step.input_summary, step.output_summary) == (long_input, long_output)


def test_golden_merge_short_circuit(tmp_db, clean_hub):
    settings, session = tmp_db
    first = process_event(session, settings, EventIngest(**HUB_EVENT))
    run_before, audit_before, notes_before = _run_ids(session), _audit_ids(session), _note_ids(session)
    hub._history.clear()

    second = process_event(session, settings, EventIngest(**HUB_EVENT))
    run = _the_run(session, run_before)
    events = _run_events(run.id)

    assert [_shape(e) for e in events] == GOLDEN_MERGE
    _check_envelopes(events)
    assert events[-1]["payload"] == {"incident_number": "INC000001"}

    assert second.id == first.id
    assert len(session.scalars(select(IncidentRow)).all()) == 1
    assert first.child_sites_down == 0
    assert (run.status, run.current_node, run.incident_id) == ("SUCCEEDED", None, first.id)

    by_node = {s.node_name: s for s in run.steps}
    assert [(s.seq, s.node_name, s.status) for s in run.steps] == [
        (1, "INGEST", "SUCCEEDED"),
        (2, "CORRELATE", "SUCCEEDED"),
    ]
    assert [s.input_summary for s in run.steps] == HITL_INPUT_SUMMARIES[:2]
    assert by_node["CORRELATE"].output_summary == "merged into INC000001"
    assert by_node["CORRELATE"].rationale == "Duplicate within 15m window — idempotent merge"
    assert by_node["CORRELATE"].tools_called == [
        {"name": "find_open_by_fingerprint", "ok": True, "latency_ms": 2}
    ]

    # entity_id is "" on both rows: complete_step runs before run.incident_id is set
    assert _new_audit(session, audit_before) == [
        ("IngestCorrelationAgent", "step.succeeded", ""),
        ("IngestCorrelationAgent", "step.succeeded", ""),
    ]
    new_notes = _new_notes(session, notes_before)
    assert [(n.author, n.author_role, n.source, n.body) for n in new_notes] == [
        (
            "IngestCorrelationAgent",
            "AGENT",
            "agent",
            "Correlated duplicate alarm POWER_GRID_FAIL into open ticket.",
        )
    ]
    # The merge short-circuit must commit: the incident row was already durable from the first
    # run, so the merge note is what proves this run's write reached the database.
    assert _in_other_session(lambda s: s.get(WorkNoteRow, new_notes[0].id)) is not None


def test_golden_cascade_child_short_circuit(tmp_db, clean_hub):
    settings, session = tmp_db
    parent = process_event(session, settings, EventIngest(**PARENT_HUB_EVENT))
    run_before, audit_before, notes_before = _run_ids(session), _audit_ids(session), _note_ids(session)
    hub._history.clear()

    returned = process_event(session, settings, EventIngest(**CHILD_EVENT))
    run = _the_run(session, run_before)
    events = _run_events(run.id)

    assert [_shape(e) for e in events] == GOLDEN_CASCADE
    _check_envelopes(events)
    assert events[-1]["payload"] == {
        "parent": "INC000001",
        "child_site": "SFC-NBIW-ENB-CBD07",
        "child_sites_down": 1,
    }

    assert returned.id == parent.id
    assert parent.child_sites_down == 1
    # The cascade short-circuit must commit, not just flush: read the counter back fresh.
    assert _in_other_session(lambda s: s.get(IncidentRow, parent.id).child_sites_down) == 1
    assert len(session.scalars(select(IncidentRow)).all()) == 1
    assert (run.status, run.current_node, run.incident_id) == ("SUCCEEDED", None, parent.id)

    by_node = {s.node_name: s for s in run.steps}
    assert [(s.seq, s.node_name, s.status) for s in run.steps] == [
        (1, "INGEST", "SUCCEEDED"),
        (2, "CORRELATE", "SUCCEEDED"),
    ]
    assert [s.input_summary for s in run.steps] == [
        "site=SFC-NBIW-ENB-CBD07",
        "fp=SFC-NBIW-ENB-CBD07|SITE_DOWN|POWER",
    ]
    assert by_node["CORRELATE"].output_summary == "cascade child under INC000001"
    assert by_node["CORRELATE"].rationale == (
        "Site feeds parent HUB SFC-NBIW-HUB-WLG; linked as child note instead of "
        "new major (cascade control)"
    )
    assert by_node["CORRELATE"].tools_called == [{"name": "link_parent_hub", "ok": True, "latency_ms": 2}]

    assert _new_audit(session, audit_before) == [
        ("IngestCorrelationAgent", "step.succeeded", ""),
        ("IngestCorrelationAgent", "step.succeeded", ""),
    ]
    new_notes = _new_notes(session, notes_before)
    assert [(n.author, n.author_role, n.source, n.body) for n in new_notes] == [
        (
            "IngestCorrelationAgent",
            "AGENT",
            "cascade",
            "Cascade child alarm: SFC-NBIW-ENB-CBD07 (SITE_DOWN/POWER) linked under HUB major "
            "INC000001. Child count now 1.",
        )
    ]
