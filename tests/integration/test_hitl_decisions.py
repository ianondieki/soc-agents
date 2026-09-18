"""Stage C1-C4: HITL decisions are task-type aware, compare-and-set, and keep the incident
scalars derived from the task table.

APPROVE_BROADCAST (the gating task) is the only type whose approval releases the held drafts
and finishes the WAITING_HITL run; rejecting it cancels the drafts and the run. A monitor
GENERIC task records the decision only. A resolved task answers 409 to any further decision.
"""

from __future__ import annotations

import importlib
import threading
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from noc_agents.db.models import AgentRunRow, BroadcastRow, HitlTaskRow, IncidentRow, WorkNoteRow, get_session, utcnow
from noc_agents.realtime.hub import hub

HUB_EVENT = {
    "site_id": "SFC-NBIE-HUB-EMB",
    "site_name": "Embakasi East Aggregation HUB",
    "site_type": "HUB",
    "region_code": "NBI_E",
    "alarm_code": "POWER_GRID_FAIL",
    "failure_domain": "POWER",
    "users_affected": 450000,
}
SUPERVISOR = {"resolved_by": "Supervisor A"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "hitl.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")

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


def _incident(client, inc_id: str) -> dict:
    return client.get(f"/api/v1/incidents/{inc_id}").json()


def _tasks(inc_id: str) -> list[tuple[str, str]]:
    return _read(
        lambda s: [
            (t.task_type, t.status)
            for t in s.scalars(select(HitlTaskRow).where(HitlTaskRow.incident_id == inc_id).order_by(HitlTaskRow.created_at))
        ]
    )


def _broadcast_statuses(inc_id: str) -> set[str]:
    return _read(lambda s: {b.status for b in s.scalars(select(BroadcastRow).where(BroadcastRow.incident_id == inc_id))})


def _lifecycle_run(inc_id: str) -> tuple[str, str | None, list[str]]:
    def read(s):
        run = s.scalar(select(AgentRunRow).where(AgentRunRow.incident_id == inc_id).order_by(AgentRunRow.started_at))
        return run.status, run.error_summary, [st.status for st in run.steps]

    return _read(read)


def _notes(inc_id: str) -> list[str]:
    return _read(
        lambda s: [
            n.body
            for n in s.scalars(select(WorkNoteRow).where(WorkNoteRow.incident_id == inc_id, WorkNoteRow.source == "hitl"))
        ]
    )


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
    assert (inc["requires_hitl"], inc["hitl_state"]) == (True, "PENDING")
    (task,) = client.get("/api/v1/hitl/pending").json()
    assert task["task_type"] == "APPROVE_BROADCAST"
    return inc, task["id"]


# --- C1: task typing ----------------------------------------------------------------------


def test_approving_generic_task_records_decision_only(client, gated):
    inc, ab_id = gated
    gen_id = _force_generic_task(client, inc["id"])
    assert _tasks(inc["id"]) == [("APPROVE_BROADCAST", "PENDING"), ("GENERIC", "PENDING")]
    running_before = client.get("/api/v1/metrics/summary").json()["agents_running"]

    r = client.post(f"/api/v1/hitl/{gen_id}/approve", json={**SUPERVISOR, "overrides": {"priority": "P1"}})
    assert r.status_code == 200

    assert _tasks(inc["id"]) == [("APPROVE_BROADCAST", "PENDING"), ("GENERIC", "APPROVED")]
    assert _broadcast_statuses(inc["id"]) == {"PENDING_HITL"}  # nothing released
    assert _lifecycle_run(inc["id"])[0] == "WAITING_HITL"
    assert client.get("/api/v1/metrics/summary").json()["agents_running"] == running_before
    after = _incident(client, inc["id"])
    assert (after["requires_hitl"], after["hitl_state"], after["priority"]) == (True, "PENDING", "P2")  # overrides ignored
    assert _notes(inc["id"]) == ["HITL approved (GENERIC)."]
    ev = [e for e in hub._history if e["type"] == "hitl.approved"][-1]
    assert ev["payload"] == {
        "task_id": gen_id,
        "resolved_by": "Supervisor A",
        "task_type": "GENERIC",
        "incident_number": inc["incident_number"],
    }
    assert [t["id"] for t in client.get("/api/v1/hitl/pending").json()] == [ab_id]  # AB still in the inbox


def test_approving_broadcast_task_releases_drafts_and_finishes_run(client, gated):
    inc, ab_id = gated
    r = client.post(f"/api/v1/hitl/{ab_id}/approve", json={**SUPERVISOR, "overrides": {"priority": "P1"}})
    assert r.status_code == 200

    assert _tasks(inc["id"]) == [("APPROVE_BROADCAST", "APPROVED")]
    assert "PENDING_HITL" not in _broadcast_statuses(inc["id"])
    status, error, steps = _lifecycle_run(inc["id"])
    assert (status, error) == ("SUCCEEDED", None)
    assert steps.count("WAITING_HITL") == 2  # step rows untouched (HITL, BROADCAST)
    after = _incident(client, inc["id"])
    assert (after["requires_hitl"], after["hitl_state"], after["priority"]) == (False, "APPROVED", "P1")
    assert _notes(inc["id"]) == ["HITL approved broadcast/assignment."]
    assert client.get("/api/v1/hitl/pending").json() == []
    ev = [e for e in hub._history if e["type"] == "hitl.approved"][-1]
    assert ev["payload"]["task_type"] == "APPROVE_BROADCAST"


def test_invalid_priority_override_is_400_and_leaves_task_open(client, gated):
    inc, ab_id = gated
    r = client.post(f"/api/v1/hitl/{ab_id}/approve", json={**SUPERVISOR, "overrides": {"priority": "P9"}})
    assert r.status_code == 400
    assert _tasks(inc["id"]) == [("APPROVE_BROADCAST", "PENDING")]
    assert _broadcast_statuses(inc["id"]) == {"PENDING_HITL"}
    assert client.post(f"/api/v1/hitl/{ab_id}/approve", json=SUPERVISOR).status_code == 200


# --- C2: compare-and-set --------------------------------------------------------------------


def test_claim_then_approve_by_same_user_still_200(client, gated):
    _inc, ab_id = gated
    assert client.post(f"/api/v1/hitl/{ab_id}/claim", json=SUPERVISOR).status_code == 200
    assert client.post(f"/api/v1/hitl/{ab_id}/approve", json=SUPERVISOR).status_code == 200


def test_decisions_on_a_resolved_task_are_409(client, gated):
    inc, ab_id = gated
    assert client.post(f"/api/v1/hitl/{ab_id}/approve", json=SUPERVISOR).status_code == 200

    again = client.post(f"/api/v1/hitl/{ab_id}/approve", json=SUPERVISOR)
    assert (again.status_code, again.json()) == (409, {"detail": "task already APPROVED"})
    assert client.post(f"/api/v1/hitl/{ab_id}/reject", json={**SUPERVISOR, "reason": "late"}).status_code == 409
    assert client.post(f"/api/v1/hitl/{ab_id}/claim", json=SUPERVISOR).status_code == 409
    assert _tasks(inc["id"]) == [("APPROVE_BROADCAST", "APPROVED")]
    assert _incident(client, inc["id"])["hitl_state"] == "APPROVED"
    assert _notes(inc["id"]) == ["HITL approved broadcast/assignment."]  # no second note


def test_concurrent_approvals_only_one_wins(client, gated):
    """Six supervisors (or one double-click) approve the same open task at the same instant:
    the conditional UPDATE lets exactly one through; the rest answer 409 and run no side effects."""
    inc, ab_id = gated
    n = 6
    barrier = threading.Barrier(n)
    codes: list[int] = []
    lock = threading.Lock()

    def approve() -> None:
        barrier.wait()
        r = client.post(f"/api/v1/hitl/{ab_id}/approve", json=SUPERVISOR)
        with lock:
            codes.append(r.status_code)

    threads = [threading.Thread(target=approve, daemon=True) for _ in range(n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(60)
    assert sorted(codes) == [200] + [409] * (n - 1)

    assert _tasks(inc["id"]) == [("APPROVE_BROADCAST", "APPROVED")]
    assert _notes(inc["id"]) == ["HITL approved broadcast/assignment."]  # the release path ran once
    assert "PENDING_HITL" not in _broadcast_statuses(inc["id"])
    assert _lifecycle_run(inc["id"])[0] == "SUCCEEDED"
    assert len([e for e in hub._history if e["type"] == "hitl.approved" and e["payload"]["task_id"] == ab_id]) == 1
    assert _incident(client, inc["id"])["hitl_state"] == "APPROVED"


def test_reject_then_approve_is_409(client, gated):
    inc, ab_id = gated
    assert client.post(f"/api/v1/hitl/{ab_id}/reject", json={**SUPERVISOR, "reason": "wording"}).status_code == 200
    r = client.post(f"/api/v1/hitl/{ab_id}/approve", json=SUPERVISOR)
    assert (r.status_code, r.json()) == (409, {"detail": "task already REJECTED"})
    assert _broadcast_statuses(inc["id"]) == {"CANCELLED"}  # the rejected wording never goes out
    assert client.post("/api/v1/hitl/unknown-task/approve", json=SUPERVISOR).status_code == 404  # unchanged


# --- C4: reject finalises -------------------------------------------------------------------


def test_rejecting_broadcast_task_cancels_drafts_and_run(client, gated):
    inc, ab_id = gated
    running_before = client.get("/api/v1/metrics/summary").json()["agents_running"]
    assert client.post(f"/api/v1/hitl/{ab_id}/reject", json={"resolved_by": "Duty Manager", "reason": "wrong MSP"}).status_code == 200

    assert _tasks(inc["id"]) == [("APPROVE_BROADCAST", "REJECTED")]
    assert _broadcast_statuses(inc["id"]) == {"CANCELLED"}
    status, error, steps = _lifecycle_run(inc["id"])
    assert (status, error) == ("CANCELLED", "HITL rejected: wrong MSP")
    assert steps.count("WAITING_HITL") == 2  # nodes keep waiting_hitl in the workflow graph
    after = _incident(client, inc["id"])
    assert (after["requires_hitl"], after["hitl_state"]) == (False, "REJECTED")
    assert client.get("/api/v1/metrics/summary").json()["agents_running"] == running_before - 1
    assert _notes(inc["id"]) == ["HITL rejected: wrong MSP"]

    finished = [e for e in hub._history if e["type"] == "agent.run.finished" and e["payload"]["status"] == "CANCELLED"]
    (ev,) = finished
    assert ev["incident_id"] == inc["id"] and ev["run_id"]
    assert ev["payload"] == {
        "seq": 12,
        "incident_number": inc["incident_number"],
        "run_id": ev["run_id"],
        "status": "CANCELLED",
        "error": "HITL rejected: wrong MSP",
    }
    rejected = [e for e in hub._history if e["type"] == "hitl.rejected"][-1]
    assert rejected["payload"] == {
        "task_id": ab_id,
        "reason": "wrong MSP",
        "task_type": "APPROVE_BROADCAST",
        "incident_number": inc["incident_number"],
    }
    # /runs and the workflow graph still render: CANCELLED is a plain terminal status string.
    run = [r for r in client.get("/api/v1/runs").json() if r["incident_id"] == inc["id"]][0]
    assert run["status"] == "CANCELLED"
    wf = client.get(f"/api/v1/incidents/{inc['id']}/workflow").json()
    assert {n["id"]: n["status"] for n in wf["nodes"]}["HITL"] == "waiting_hitl"
    # Broadcasts show as CANCELLED on the timeline
    assert {i["status"] for i in client.get(f"/api/v1/incidents/{inc['id']}/timeline").json() if i["kind"] == "broadcast"} == {"CANCELLED"}


def test_rejecting_generic_task_keeps_broadcast_gate(client, gated):
    inc, ab_id = gated
    gen_id = _force_generic_task(client, inc["id"])
    assert client.post(f"/api/v1/hitl/{gen_id}/reject", json={**SUPERVISOR, "reason": "no action"}).status_code == 200
    assert _tasks(inc["id"]) == [("APPROVE_BROADCAST", "PENDING"), ("GENERIC", "REJECTED")]
    assert _broadcast_statuses(inc["id"]) == {"PENDING_HITL"}
    assert _lifecycle_run(inc["id"])[0] == "WAITING_HITL"
    after = _incident(client, inc["id"])
    assert (after["requires_hitl"], after["hitl_state"]) == (True, "PENDING")
    assert not [e for e in hub._history if e["type"] == "agent.run.finished" and e["payload"]["status"] == "CANCELLED"]


# --- C3: derived scalars ------------------------------------------------------------------------


def test_scalars_follow_the_task_table_across_multiple_tasks(client, gated):
    inc, ab_id = gated
    gen_id = _force_generic_task(client, inc["id"])

    assert client.post(f"/api/v1/hitl/{ab_id}/approve", json=SUPERVISOR).status_code == 200
    after = _incident(client, inc["id"])
    assert (after["requires_hitl"], after["hitl_state"]) == (True, "PENDING")  # GENERIC still open

    assert client.post(f"/api/v1/hitl/{gen_id}/reject", json={**SUPERVISOR, "reason": "handled"}).status_code == 200
    after = _incident(client, inc["id"])
    assert (after["requires_hitl"], after["hitl_state"]) == (False, "APPROVED")  # gating result wins

    # The next tick raises a fresh GENERIC (dedupe only looks at open ones) and the scalars follow.
    tick = client.post("/api/v1/monitor/tick").json()
    assert any(r["incident_id"] == inc["id"] for r in tick["results"])
    after = _incident(client, inc["id"])
    assert (after["requires_hitl"], after["hitl_state"]) == (True, "PENDING")
    assert client.get("/api/v1/metrics/summary").json()["hitl_pending"] == 1


def test_generic_only_incident_reports_its_own_decision(client, gated):
    inc, ab_id = gated
    gen_id = _force_generic_task(client, inc["id"])
    assert client.post(f"/api/v1/hitl/{ab_id}/reject", json={**SUPERVISOR, "reason": "hold"}).status_code == 200
    assert client.post(f"/api/v1/hitl/{gen_id}/approve", json=SUPERVISOR).status_code == 200
    after = _incident(client, inc["id"])
    assert (after["requires_hitl"], after["hitl_state"]) == (False, "REJECTED")  # gating task decides


def test_close_keeps_scalars_consistent_with_open_task(client, gated):
    inc, ab_id = gated
    r = client.post(f"/api/v1/incidents/{inc['id']}/close", json={"closed_by": "Supervisor A"})
    assert r.status_code == 200
    closed = r.json()["incident"]
    assert closed["status"] == "CLOSED"
    assert (closed["requires_hitl"], closed["hitl_state"]) == (True, "PENDING")  # task still open (cancel-on-close deferred)
    assert client.post(f"/api/v1/hitl/{ab_id}/reject", json={**SUPERVISOR, "reason": "closed"}).status_code == 200
    after = _incident(client, inc["id"])
    assert (after["status"], after["requires_hitl"], after["hitl_state"]) == ("CLOSED", False, "REJECTED")
