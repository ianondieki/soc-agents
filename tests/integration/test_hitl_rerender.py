"""Brief defect #11 is dead: a supervisor's overrides reach the customer.

Before this wave the HITL node composed the SMS/email, the supervisor "edited" it through
overrides, and ``main.py`` released the ORIGINAL draft. Now APPROVE_BROADCAST rebuilds the
``NocAlert`` envelope from the incident *as overridden*, re-renders every channel and hands
the new rows to ``outbox.release_held``; the reviewed draft is never enqueued.

Also pinned here (spec §6.5, §5.3.1, §2.1 R6/R7): reject leaves the drafts ``CANCELLED`` and
the run ``CANCELLED`` and suppresses any HELD outbox row; a GENERIC approve releases nothing;
raiser == approver is 403; the approve reason is required only behind
``HITL_APPROVE_REASON_REQUIRED``; ``hitl_tasks.{created_by, run_id, entity_*, edited}`` are
populated; and with ``ALERT_ENVELOPE_V2`` on or off the released strings are byte-identical
to today's composers on the updated row.
"""

from __future__ import annotations

import importlib
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from noc_agents.adapters.email_smtp import parse_subject_body
from noc_agents.config import get_settings
from noc_agents.db.models import AgentRunRow, BroadcastRow, HitlTaskRow, IncidentRow, OutboxRow, get_session, utcnow
from noc_agents.domain.alerts import NocAlert
from noc_agents.domain.schemas import EventIngest
from noc_agents.graph.pipeline import process_event
from noc_agents.orchestrator import outbox
from noc_agents.realtime.hub import hub
from noc_agents.services.composition import compose_email, compose_sms
from noc_agents.services.hitl import AGENT_RAISER, rerender_and_release

HUB_EVENT = {
    "site_id": "SFC-NBIE-HUB-EMB",
    "site_name": "Embakasi East Aggregation HUB",
    "site_type": "HUB",
    "region_code": "NBI_E",
    "alarm_code": "POWER_GRID_FAIL",
    "failure_domain": "POWER",
    "users_affected": 450000,
}
ATL_HUB = {  # P2 for airtel: dated ticket numbers (ATL-YYYYMMDD-NNNNN) that do not fit §6.1's ^INC\d{6}$
    "site_id": "ATL-NBI-HUB-001",
    "site_name": "Airtel Nairobi Aggregation HUB",
    "site_type": "HUB",
    "region_code": "NBI",
    "alarm_code": "POWER_GRID_FAIL",
    "failure_domain": "POWER",
    "users_affected": 450000,
}
SUPERVISOR = {"resolved_by": "Supervisor A"}
CHANNEL_KINDS = ("SMS", "EMAIL")
SENDABLE = {"PENDING", "CLAIMED", "SENT", "DELIVERED"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "rerender.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
    monkeypatch.delenv("ALERT_ENVELOPE_V2", raising=False)
    monkeypatch.delenv("HITL_APPROVE_REASON_REQUIRED", raising=False)

    import noc_agents.config as cfg
    import noc_agents.db.models as models
    import noc_agents.main as main

    cfg.clear_settings_cache()
    models._engine = None
    models.SessionLocal = None
    importlib.reload(main)
    hub._history.clear()
    with TestClient(main.app) as c:
        yield c
    hub._history.clear()


def _read(fn):
    session = get_session()
    try:
        return fn(session)
    finally:
        session.close()


def _task(task_id: str) -> HitlTaskRow:
    def read(s):
        t = s.get(HitlTaskRow, task_id)
        s.expunge(t)
        return t

    return _read(read)


def _incident_row(inc_id: str) -> IncidentRow:
    def read(s):
        inc = s.get(IncidentRow, inc_id)
        _ = inc.services_impacted  # load before detaching
        s.expunge(inc)
        return inc

    return _read(read)


def _drafts(inc_id: str) -> list[BroadcastRow]:
    def read(s):
        rows = s.scalars(select(BroadcastRow).where(BroadcastRow.incident_id == inc_id).order_by(BroadcastRow.id)).all()
        for r in rows:
            s.expunge(r)
        return rows

    return _read(read)


def _channel_rows(inc_id: str) -> list[OutboxRow]:
    def read(s):
        rows = s.scalars(
            select(OutboxRow)
            .where(OutboxRow.incident_id == inc_id, OutboxRow.kind.in_(CHANNEL_KINDS))
            .order_by(OutboxRow.created_at, OutboxRow.id)
        ).all()
        for r in rows:
            s.expunge(r)
        return rows

    return _read(read)


def _run(inc_id: str) -> AgentRunRow:
    def read(s):
        run = s.scalar(select(AgentRunRow).where(AgentRunRow.incident_id == inc_id).order_by(AgentRunRow.started_at))
        s.expunge(run)
        return run

    return _read(read)


def _payload(row: OutboxRow) -> dict:
    return json.loads(row.payload_json or "{}")


def _envelope(row: OutboxRow) -> NocAlert:
    assert row.envelope_json, f"outbox row {row.id} carries no envelope"
    return NocAlert.model_validate_json(row.envelope_json)


def _drain() -> outbox.DrainReport:
    return _read(lambda s: outbox.drain_once(s))


def _force_generic_task(client, inc_id: str) -> str:
    """Backdate the restore SLA so the monitor tick raises a GENERIC escalation task (P2)."""

    def backdate(s):
        inc = s.get(IncidentRow, inc_id)
        inc.sla_restore_due = utcnow() - timedelta(hours=1)
        s.commit()

    _read(backdate)
    tick = client.post("/api/v1/monitor/tick").json()
    assert any(r["incident_id"] == inc_id and r["action"] == "escalated" for r in tick["results"])
    (generic,) = [t for t in client.get("/api/v1/hitl/pending").json() if t["incident_id"] == inc_id and t["task_type"] == "GENERIC"]
    return generic["id"]


@pytest.fixture()
def gated(client):
    """A P2 HUB incident held at the HITL gate, with its APPROVE_BROADCAST task id."""
    inc = client.post("/api/v1/events", json=HUB_EVENT).json()["incident"]
    assert (inc["requires_hitl"], inc["hitl_state"], inc["priority"]) == (True, "PENDING", "P2")
    (task,) = client.get("/api/v1/hitl/pending").json()
    assert task["task_type"] == "APPROVE_BROADCAST"
    return inc, task["id"]


# --- what the HITL node now stores -----------------------------------------------------------------


def test_agent_raised_task_carries_raiser_run_and_envelope(client, gated):
    inc, ab_id = gated
    task = _task(ab_id)
    run = _run(inc["id"])

    # An agent raised this task: no human raiser exists, so the raiser is the agent principal.
    assert task.created_by == AGENT_RAISER == "agent:SupervisorAgent"
    assert (task.run_id, task.entity_type, task.entity_id, task.edited) == (run.id, "incident", inc["id"], 0)

    payload = task.proposed_payload
    row = _incident_row(inc["id"])
    cfg = get_settings().operator
    # Flag off: the proposed strings are today's composers, byte for byte, and the drafts match them.
    assert payload["sms"] == compose_sms(row) and payload["email"] == compose_email(row, cfg)
    assert payload["sms"].startswith("[P2] ")
    assert {d.message for d in _drafts(inc["id"]) if d.channel == "SMS"} == {payload["sms"]}
    assert {d.message for d in _drafts(inc["id"]) if d.channel == "EMAIL"} == {payload["email"]}
    assert {d.status for d in _drafts(inc["id"])} == {"PENDING_HITL"}
    # The envelope is stored beside the strings and is the v1 alert of this incident.
    envelope = NocAlert.model_validate(payload["envelope"])
    assert envelope.alert_id == payload["alert_id"] and envelope.sequence == 1 and envelope.references == []
    assert envelope.incident.incident_number == inc["incident_number"]
    assert envelope.classification.priority == "P2" and envelope.governance.requires_hitl is True
    assert envelope.governance.hitl_task_id == ab_id and envelope.governance.approved_by is None
    assert [a for a in payload["audiences"]] == ["RNIO", "FIELD_ENGINEER", "MSP", "MANAGEMENT"]

    # Nothing for the broadcast reaches the outbox while the task is open (M1: no unapproved send).
    assert _channel_rows(inc["id"]) == []
    assert _drain().sent == 0


# --- the defect: overrides must reach the customer ---------------------------------------------------


def test_priority_override_rerenders_and_the_old_draft_never_ships(client, gated):
    inc, ab_id = gated
    proposed = _task(ab_id).proposed_payload
    old_sms, old_email, old_alert_id = proposed["sms"], proposed["email"], proposed["alert_id"]
    assert old_sms.startswith("[P2] ") and old_email.startswith("Subject: [P2] ")

    r = client.post(f"/api/v1/hitl/{ab_id}/approve", json={**SUPERVISOR, "reason": "customer impact is P1", "overrides": {"priority": "P1"}})
    assert r.status_code == 200

    row = _incident_row(inc["id"])
    assert row.priority == "P1"
    new_sms, new_email = compose_sms(row), compose_email(row, get_settings().operator)
    assert new_sms.startswith("[P1] ") and new_sms != old_sms

    # 1. The wording that reached the outbox is the re-rendered one — every channel, every audience.
    rows = _channel_rows(inc["id"])
    assert sorted(r.kind for r in rows) == ["EMAIL", "SMS", "SMS", "SMS", "SMS"]
    for r in rows:
        p = _payload(r)
        if r.kind == "SMS":
            assert p["message"] == new_sms, r.idempotency_key
        else:
            assert p["subject"].startswith("[P1] ") and (p["subject"], p["body"]) == parse_subject_body(new_email), r.idempotency_key
    # 2. The old draft is in NO outbox row at all — sendable or otherwise.
    assert all(old_sms not in (r.payload_json or "") for r in rows)
    assert all("[P2]" not in (r.payload_json or "") for r in rows)
    # 3. Every released row is approved by the human and sendable; nothing else sits HELD/PENDING.
    assert {r.status for r in rows} <= SENDABLE
    assert {(r.requires_hitl, r.approved_by, r.hitl_task_id, r.alert_id is not None) for r in rows} == {(1, "Supervisor A", ab_id, True)}
    assert all(r.approved_at is not None for r in rows)
    # 4. The new envelope supersedes the reviewed one and records the approval.
    for r in rows:
        env = _envelope(r)
        assert env.sequence == 2 and env.references == [old_alert_id] and env.alert_id == r.alert_id
        assert env.alert_id != old_alert_id
        assert env.classification.priority == "P1" and env.classification.severity == "EXTREME"
        assert env.governance.requires_hitl is True and env.governance.approved_by == "Supervisor A"
        assert env.governance.approved_at is not None and env.governance.hitl_task_id == ab_id
        assert env.idempotency_seed == f"{inc['id']}|ALERT|2"
        assert [a.audience for a in env.audiences] == ["RNIO", "FE", "MSP", "MANAGEMENT"]  # the reviewed audience set
    # 5. The drafts on the timeline carry the approved wording, not the reviewed one.
    drafts = _drafts(inc["id"])
    assert {d.message for d in drafts if d.channel == "SMS"} == {new_sms}
    assert {d.message for d in drafts if d.channel == "EMAIL"} == {new_email}
    assert {d.status for d in drafts} <= {"QUEUED", "SENT"} and "PENDING_HITL" not in {d.status for d in drafts}
    # 6. After a drain everything sendable went out with the NEW text and the old text still appears nowhere.
    _drain()
    rows = _channel_rows(inc["id"])
    assert {r.status for r in rows} == {"SENT"}
    assert all(_payload(r).get("message", new_sms) == new_sms for r in rows if r.kind == "SMS")
    assert all(old_sms not in (r.payload_json or "") for r in rows)
    assert {d.status for d in _drafts(inc["id"])} == {"SENT"}

    # 7. The task records the decision: reason stored, edited flag set, proposed vs released kept apart.
    task = _task(ab_id)
    assert (task.status, task.resolved_by, task.reason, task.edited) == ("APPROVED", "Supervisor A", "customer impact is P1", 1)
    payload = task.proposed_payload
    assert payload["sms"] == old_sms and payload["email"] == old_email  # what the approver saw, for the audit
    assert payload["released"]["sms"] == new_sms and payload["released"]["email"] == new_email
    assert (payload["released"]["priority"], payload["released"]["sequence"], payload["released"]["edited"]) == ("P1", 2, True)
    assert payload["released"]["alert_id"] == rows[0].alert_id

    run = _run(inc["id"])
    assert (run.status, run.error_summary) == ("SUCCEEDED", None)
    after = client.get(f"/api/v1/incidents/{inc['id']}").json()
    assert (after["requires_hitl"], after["hitl_state"], after["priority"]) == (False, "APPROVED", "P1")


def test_approve_without_changes_still_rerenders_and_is_not_an_edit(client, gated):
    inc, ab_id = gated
    proposed = _task(ab_id).proposed_payload
    assert client.post(f"/api/v1/hitl/{ab_id}/approve", json=SUPERVISOR).status_code == 200

    rows = _channel_rows(inc["id"])
    assert sorted(r.kind for r in rows) == ["EMAIL", "SMS", "SMS", "SMS", "SMS"]
    assert {_payload(r)["message"] for r in rows if r.kind == "SMS"} == {proposed["sms"]}  # unchanged wording
    assert {_envelope(r).sequence for r in rows} == {2}  # but a fresh, approved envelope
    assert {_envelope(r).references[0] for r in rows} == {proposed["alert_id"]}
    task = _task(ab_id)
    assert (task.status, task.edited, task.reason) == ("APPROVED", 0, None)
    assert task.proposed_payload["released"]["edited"] is False
    _drain()
    assert {r.status for r in _channel_rows(inc["id"])} == {"SENT"}


def test_assignee_and_msp_override_reach_the_released_wording(client, gated):
    inc, ab_id = gated
    old_sms = _task(ab_id).proposed_payload["sms"]
    assert "Owner:EGYPRO" in old_sms
    r = client.post(
        f"/api/v1/hitl/{ab_id}/approve",
        json={**SUPERVISOR, "overrides": {"assignee": "ADRIAN", "msp_name": "ADRIAN"}},
    )
    assert r.status_code == 200
    rows = _channel_rows(inc["id"])
    sms = {_payload(r)["message"] for r in rows if r.kind == "SMS"}
    assert sms == {compose_sms(_incident_row(inc["id"]))} and all("Owner:ADRIAN" in s for s in sms)
    assert all("Owner:EGYPRO" not in (r.payload_json or "") for r in rows)
    assert {_envelope(r).facts.msp_code for r in rows} == {"ADRIAN"}
    assert {a.recipients_ref for r in rows for a in _envelope(r).audiences if a.audience == "MSP"} == {"msp_contacts.ADRIAN"}
    assert _task(ab_id).edited == 1


# --- reject, GENERIC, raiser ----------------------------------------------------------------------


def test_reject_leaves_drafts_cancelled_run_cancelled_and_suppresses_held_rows(client, gated):
    inc, ab_id = gated
    old_alert_id = _task(ab_id).proposed_payload["alert_id"]

    # A HELD row for the reviewed alert, as the §6.5 draft-time flow will write it: it must never be claimable.
    def hold(s):
        outbox.enqueue(
            s,
            kind="SMS",
            idempotency_key=f"SMS:{inc['id']}|ALERT|1:RNIO",
            payload={"operator_id": "safaricom", "incident_number": inc["incident_number"], "audience": "RNIO", "message": "[P2] draft", "broadcast_ids": []},
            incident_id=inc["id"],
            hitl_task_id=ab_id,
            alert_id=old_alert_id,
            requires_hitl=True,
            held=True,
            operator_id="safaricom",
        )
        s.commit()

    _read(hold)
    assert [r.status for r in _channel_rows(inc["id"])] == ["HELD"]

    r = client.post(f"/api/v1/hitl/{ab_id}/reject", json={"resolved_by": "Duty Manager", "reason": "wrong MSP"})
    assert r.status_code == 200

    assert {d.status for d in _drafts(inc["id"])} == {"CANCELLED"}  # §2.1 R7: the tested draft status
    run = _run(inc["id"])
    assert (run.status, run.error_summary) == ("CANCELLED", "HITL rejected: wrong MSP")
    (held,) = _channel_rows(inc["id"])
    assert held.status == "SUPPRESSED" and held.last_error == "hitl_rejected: wrong MSP"
    assert _task(ab_id).edited == 0
    assert _drain().claimed == 0  # nothing of this incident is claimable
    assert {r.status for r in _channel_rows(inc["id"])} == {"SUPPRESSED"}
    after = client.get(f"/api/v1/incidents/{inc['id']}").json()
    assert (after["requires_hitl"], after["hitl_state"]) == (False, "REJECTED")


def test_generic_approve_releases_nothing(client, gated):
    inc, ab_id = gated
    gen_id = _force_generic_task(client, inc["id"])
    # The monitor raises GENERIC tasks without stamping created_by (not this wave's file): the
    # raiser rule fails open on NULL, which is the documented behaviour for such rows.
    assert _task(gen_id).created_by is None

    r = client.post(f"/api/v1/hitl/{gen_id}/approve", json={**SUPERVISOR, "reason": "seen", "overrides": {"priority": "P1"}})
    assert r.status_code == 200

    assert _channel_rows(inc["id"]) == []  # nothing enqueued
    assert {d.status for d in _drafts(inc["id"])} == {"PENDING_HITL"}
    assert _run(inc["id"]).status == "WAITING_HITL"
    assert (_task(gen_id).status, _task(gen_id).edited, _task(gen_id).reason) == ("APPROVED", 0, "seen")
    assert (_task(ab_id).status, _task(ab_id).edited) == ("PENDING", 0)
    assert client.get(f"/api/v1/incidents/{inc['id']}").json()["priority"] == "P2"  # overrides ignored
    assert _drain().sent == 0


def test_raiser_cannot_decide_their_own_task(client, gated):
    inc, ab_id = gated

    def raised_by_supervisor_a(s):
        s.get(HitlTaskRow, ab_id).created_by = "Supervisor A"
        s.commit()

    _read(raised_by_supervisor_a)

    approve = client.post(f"/api/v1/hitl/{ab_id}/approve", json={**SUPERVISOR, "overrides": {"priority": "P1"}})
    assert approve.status_code == 403
    reject = client.post(f"/api/v1/hitl/{ab_id}/reject", json={**SUPERVISOR, "reason": "wording"})
    assert reject.status_code == 403
    assert (_task(ab_id).status, _task(ab_id).resolved_by) == ("PENDING", None)
    assert {d.status for d in _drafts(inc["id"])} == {"PENDING_HITL"} and _channel_rows(inc["id"]) == []
    assert client.get(f"/api/v1/incidents/{inc['id']}").json()["priority"] == "P2"
    # Claiming is "I am looking at this", not a decision — still allowed to the raiser.
    assert client.post(f"/api/v1/hitl/{ab_id}/claim", json=SUPERVISOR).status_code == 200

    # A different supervisor decides normally.
    assert client.post(f"/api/v1/hitl/{ab_id}/approve", json={"resolved_by": "Duty Manager", "overrides": {"priority": "P1"}}).status_code == 200
    assert {_payload(r)["message"].startswith("[P1] ") for r in _channel_rows(inc["id"]) if r.kind == "SMS"} == {True}
    assert {r.approved_by for r in _channel_rows(inc["id"])} == {"Duty Manager"}


# --- reason rules (§2.1 R6) -------------------------------------------------------------------------


def test_approve_reason_is_required_only_behind_the_flag(client, gated, monkeypatch):
    inc, ab_id = gated
    monkeypatch.setenv("HITL_APPROVE_REASON_REQUIRED", "true")
    for body in (SUPERVISOR, {**SUPERVISOR, "reason": ""}, {**SUPERVISOR, "reason": "   "}):
        r = client.post(f"/api/v1/hitl/{ab_id}/approve", json=body)
        assert (r.status_code, r.json()) == (400, {"detail": "reason required on approve"})
    assert _task(ab_id).status == "PENDING" and _channel_rows(inc["id"]) == []
    assert client.post(f"/api/v1/hitl/{ab_id}/approve", json={**SUPERVISOR, "reason": "ok"}).status_code == 200  # no length floor
    assert _task(ab_id).reason == "ok"


def test_reject_still_needs_a_reason_and_approve_does_not_by_default(client, gated):
    _inc, ab_id = gated
    assert client.post(f"/api/v1/hitl/{ab_id}/reject", json=SUPERVISOR).status_code == 400
    assert client.post(f"/api/v1/hitl/{ab_id}/approve", json=SUPERVISOR).status_code == 200  # {"resolved_by": ...} alone


# --- a profile whose numbers fit no envelope must neither crash nor lose the fix --------------------


def test_profile_without_an_envelope_still_gates_and_rerenders(client):
    """SupervisorAgent is fail-closed and airtel's dated numbers fail the envelope's pattern:
    the node must gate and draft exactly as before the envelope existed, and the approval
    must still re-render from the overridden row — with no envelope on the rows."""
    settings = get_settings("airtel")
    session = get_session()
    try:
        inc = process_event(session, settings, EventIngest(**ATL_HUB))
        session.refresh(inc)
        assert inc.operator_id == "airtel" and inc.incident_number.startswith("ATL-")
        assert inc.priority in ("P1", "P2")  # airtel's own severity floors decide which; both are gated under L2
        assert (inc.requires_hitl, inc.hitl_state) == (True, "PENDING")
        new_priority = "P2" if inc.priority == "P1" else "P1"
        task = session.scalar(select(HitlTaskRow).where(HitlTaskRow.incident_id == inc.id))
        assert task is not None and task.task_type == "APPROVE_BROADCAST" and task.created_by == AGENT_RAISER
        payload = task.proposed_payload
        assert "envelope" not in payload and "alert_id" not in payload  # fail-soft: no envelope, no crash
        assert payload["sms"] == compose_sms(inc) and payload["email"] == compose_email(inc, settings.operator)
        assert payload["audiences"] == ["RNIO", "FIELD_ENGINEER", "MSP", "MANAGEMENT"]
        assert {d.status for d in _drafts(inc.id)} == {"PENDING_HITL"} and _channel_rows(inc.id) == []
        old_sms = payload["sms"]

        # What the airtel process's approve route does after the overrides (the safaricom API
        # cannot see this task: operator scoping answers 404, tests/unit/test_operator_isolation.py).
        inc.priority = new_priority
        task.status, task.resolved_by, task.resolved_at = "APPROVED", "Supervisor A", utcnow()
        result = rerender_and_release(session, inc, settings.operator, task=task, approved_by="Supervisor A", approved_at=utcnow())
        inc_id = inc.id
        session.commit()
    finally:
        session.close()

    assert result.alert is None and result.released == 5 and result.edited is True
    new_sms = compose_sms(_incident_row(inc_id))
    assert new_sms.startswith(f"[{new_priority}] ") and new_sms != old_sms and result.sms == new_sms
    rows = _channel_rows(inc_id)
    assert sorted(r.kind for r in rows) == ["EMAIL", "SMS", "SMS", "SMS", "SMS"]
    assert {_payload(r)["message"] for r in rows if r.kind == "SMS"} == {new_sms}
    assert all(old_sms not in (r.payload_json or "") for r in rows)
    assert {(r.status, r.envelope_json, r.approved_by, r.requires_hitl) for r in rows} == {("PENDING", None, "Supervisor A", 1)}
    assert len({r.alert_id for r in rows}) == 1 and all(r.idempotency_key.split(":")[1] == f"{inc_id}|ALERT|2" for r in rows)
    assert {d.message for d in _drafts(inc_id) if d.channel == "SMS"} == {new_sms}
    _drain()
    assert {r.status for r in _channel_rows(inc_id)} == {"SENT"}


# --- the flag: both render paths release the same bytes ---------------------------------------------


def test_envelope_v2_flag_releases_byte_identical_strings(client, monkeypatch):
    monkeypatch.setenv("ALERT_ENVELOPE_V2", "true")
    inc = client.post("/api/v1/events", json=HUB_EVENT).json()["incident"]
    (task,) = client.get("/api/v1/hitl/pending").json()
    cfg = get_settings().operator
    row = _incident_row(inc["id"])
    proposed = task["proposed_payload"]
    assert proposed["sms"].encode() == compose_sms(row).encode()  # rendered from the envelope, same bytes
    assert proposed["email"].encode() == compose_email(row, cfg).encode()

    assert client.post(f"/api/v1/hitl/{task['id']}/approve", json={**SUPERVISOR, "overrides": {"priority": "P1"}}).status_code == 200
    row = _incident_row(inc["id"])
    rows = _channel_rows(inc["id"])
    assert {_payload(r)["message"] for r in rows if r.kind == "SMS"} == {compose_sms(row)}
    (mail,) = [r for r in rows if r.kind == "EMAIL"]
    assert (_payload(mail)["subject"], _payload(mail)["body"]) == parse_subject_body(compose_email(row, cfg))
    assert _payload(mail)["subject"].startswith("[P1] ")
    assert all("[P2]" not in (r.payload_json or "") for r in rows)
