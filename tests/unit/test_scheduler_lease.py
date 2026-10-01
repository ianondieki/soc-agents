"""Scheduler with a database lease (spec §4.4 / §7.0.3).

Proved here, in order:

* two schedulers on one database → exactly one ticker (deterministic, and under a thread race);
* a stale lease (holder stopped renewing) is taken over once the 30 s TTL passes, and a clean
  release hands it over at once — a ``--reload`` restart mid-tick cannot deadlock;
* a raising job → FAILED run row (graph_name / trigger="SCHEDULE") + FAILED step,
  ``consecutive_failures`` incremented, circuit open at 3 with ``scheduler.job_failed``, and
  the loop then skips it;
* ``POST /api/v1/scheduler/run/{job}`` resets the circuit (and is admin-only once auth is on);
* two monitor ticks → one chase note; ``next_update_at`` is honoured and re-armed from
  ``sla_minutes[P].note_interval``; the manual tick still works;
* with ``SCHEDULER_ENABLED`` unset nothing starts; with it set the lifespan ticks and releases.
"""

from __future__ import annotations

import importlib
import os
import threading
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from noc_agents.api import auth
from noc_agents.db.models import (
    AgentRunRow,
    HitlTaskRow,
    IncidentRow,
    ScheduledJobStateRow,
    SchedulerLeaseRow,
    WorkNoteRow,
    get_session,
    utcnow,
)
from noc_agents.domain.schemas import EventIngest
from noc_agents.graph.pipeline import process_event
from noc_agents.realtime.hub import hub
from noc_agents.scheduler import JobCard, JobResult
from noc_agents.scheduler.loop import (
    LEASE_NAME,
    LEASE_TTL,
    SCHEDULED_JOBS,
    Scheduler,
    job_card,
    run_job,
    status_payload,
)
from noc_agents.services.worklog_monitor import chase_silent_incidents

HUB_EVENT = {
    "site_id": "SFC-NBIE-HUB-EMB",
    "site_name": "Embakasi East Aggregation HUB",
    "site_type": "HUB",
    "region_code": "NBI_E",
    "alarm_code": "POWER_GRID_FAIL",
    "failure_domain": "POWER",
    "users_affected": 450000,
}
SECRET = "scheduler-test-secret"
SCHED_ENV = ("SCHEDULER_ENABLED", "SCHEDULER_TICK_SECONDS", "AUTH_DISABLED", "NOC_ENV", "NOC_SESSION_SECRET")


def boom(session, settings) -> JobResult:
    raise RuntimeError("boom")


def ok(session, settings) -> JobResult:
    return JobResult(summary="fine")


BOOM = JobCard("boom_job", 1, boom, "BOOM_JOB_ENABLED", "WorklogMonitorAgent", "monitor")
FINE = JobCard("boom_job", 1, ok, "BOOM_JOB_ENABLED", "WorklogMonitorAgent", "monitor")  # same name: same state row


def _read(fn):
    s = get_session()
    try:
        return fn(s)
    finally:
        s.close()


def _lease():
    def read(s):
        row = s.get(SchedulerLeaseRow, LEASE_NAME)
        return (row.owner, row.expires_at) if row else None

    return _read(read)


def _state(name):
    def read(s):
        row = s.get(ScheduledJobStateRow, name)
        return (row.consecutive_failures, row.circuit_open, row.last_status) if row else None

    return _read(read)


def _runs(graph_name):
    def read(s):
        rows = s.scalars(select(AgentRunRow).where(AgentRunRow.graph_name == graph_name).order_by(AgentRunRow.started_at)).all()
        return [(r.trigger, r.status, r.error_summary, r.current_node, [(st.seq, st.node_name, st.agent_name, st.status) for st in r.steps]) for r in rows]

    return _read(read)


def _chase_notes(incident_id):
    return _read(lambda s: s.scalars(select(WorkNoteRow).where(WorkNoteRow.incident_id == incident_id, WorkNoteRow.source == "monitor")).all())


@pytest.fixture()
def clean_hub():
    hub._history.clear()
    yield hub
    hub._history.clear()


# --- the lease ------------------------------------------------------------------------------


def test_two_schedulers_on_one_db_yield_one_ticker(tmp_db):
    a = Scheduler(jobs=(), owner="proc-a")
    b = Scheduler(jobs=(), owner="proc-b")
    now = utcnow()

    ra, rb = a.tick(now), b.tick(now)
    assert (ra.held, rb.held) == (True, False)
    assert _lease() == ("proc-a", now + LEASE_TTL)

    # Renewal keeps it with A; B stays a bystander on every tick while the lease is live.
    for s in (5, 10, 15, 25):
        assert a.tick(now + timedelta(seconds=s)).held is True
        assert b.tick(now + timedelta(seconds=s)).held is False
    assert _lease() == ("proc-a", now + timedelta(seconds=25) + LEASE_TTL)
    assert (a.holding, b.holding) == (True, False)


def test_two_schedulers_racing_threads_never_both_hold(tmp_db):
    a = Scheduler(jobs=(), owner="race-a")
    b = Scheduler(jobs=(), owner="race-b")
    start = threading.Barrier(2)
    held: dict[str, int] = {"race-a": 0, "race-b": 0}
    errors: list[BaseException] = []

    def hammer(sched: Scheduler) -> None:
        try:
            start.wait()
            for _ in range(25):
                if sched.acquire():  # real clock: the TTL (30 s) dwarfs the run, so one owner keeps it
                    held[sched.owner] += 1
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(s,)) for s in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    winners = [o for o, n in held.items() if n > 0]
    assert len(winners) == 1, held  # exactly one process ever held the lease
    assert held[winners[0]] == 25  # and it renewed every time


def test_stale_lease_is_taken_over_and_a_clean_release_hands_over_at_once(tmp_db):
    a = Scheduler(jobs=(), owner="old-proc")
    b = Scheduler(jobs=(), owner="new-proc")
    t0 = utcnow()
    assert a.tick(t0).held is True

    # A stops renewing (crashed, or hard-killed by the reloader). Inside the TTL B is still refused…
    assert b.tick(t0 + timedelta(seconds=29)).held is False
    # …and the moment the lease is stale, B takes it over without waiting on anyone.
    assert b.tick(t0 + timedelta(seconds=31)).held is True
    assert _lease() == ("new-proc", t0 + timedelta(seconds=31) + LEASE_TTL)
    # A coming back (uvicorn --reload restarted it) finds a live lease held by B and yields.
    assert a.tick(t0 + timedelta(seconds=32)).held is False
    assert a.holding is False

    # Clean shutdown releases: the next process takes over on its very next tick, not after 30 s.
    assert b.release(t0 + timedelta(seconds=40)) is True
    assert _lease() == ("new-proc", t0 + timedelta(seconds=40))
    assert a.tick(t0 + timedelta(seconds=41)).held is True
    assert _lease()[0] == "old-proc"
    # Releasing a lease you do not hold is a no-op, never a takeover.
    assert b.release(t0 + timedelta(seconds=42)) is False
    assert _lease()[0] == "old-proc"


# --- run_job, failures and the circuit ---------------------------------------------------------


def test_raising_job_records_failed_run_and_opens_the_circuit_at_three(tmp_db, clean_hub):
    settings, _ = tmp_db

    for i in (1, 2, 3):
        outcome = run_job(BOOM, settings)
        assert (outcome.status, outcome.error, outcome.consecutive_failures, outcome.circuit_open) == (
            "FAILED",
            "RuntimeError: boom",
            i,
            i >= 3,
        )
        assert _state("boom_job") == (i, 1 if i >= 3 else 0, "FAILED")

    runs = _runs("monitor")
    assert [(t, st, err, node) for (t, st, err, node, _steps) in runs] == [("SCHEDULE", "FAILED", "RuntimeError: boom", "boom_job")] * 3
    assert [steps for (*_, steps) in runs] == [[(1, "boom_job", "WorklogMonitorAgent", "FAILED")]] * 3

    failed = [e for e in hub._history if e["type"] == "scheduler.job_failed"]
    assert len(failed) == 1  # published when the circuit opens, not on every failure
    assert failed[0]["payload"] == {
        "job": "boom_job",
        "run_id": outcome.run_id,  # the third run: the one that opened the circuit
        "error": "RuntimeError: boom",
        "consecutive_failures": 3,
        "circuit_open": True,
    }
    finished = [e for e in hub._history if e["type"] == "agent.run.finished"]
    assert [e["payload"]["status"] for e in finished] == ["FAILED"] * 3

    # The loop now skips the job: a tick that holds the lease runs nothing for it.
    sched = Scheduler(jobs=(BOOM,), owner="ticker")
    report = sched.tick()
    assert report.held is True
    assert report.ran == [] and report.skipped == {"boom_job": "circuit_open"}
    assert len(_runs("monitor")) == 3


def test_manual_run_resets_the_circuit(tmp_db, clean_hub):
    settings, _ = tmp_db
    for _ in range(3):
        run_job(BOOM, settings)
    assert _state("boom_job") == (3, 1, "FAILED")

    outcome = run_job(FINE, settings, reset_circuit=True)
    assert (outcome.status, outcome.summary, outcome.consecutive_failures, outcome.circuit_open) == ("SUCCEEDED", "fine", 0, False)
    assert _state("boom_job") == (0, 0, "SUCCEEDED")
    runs = _runs("monitor")
    assert [(t, st) for (t, st, *_r) in runs][-1] == ("SCHEDULE", "SUCCEEDED")
    assert runs[-1][-1] == [(1, "boom_job", "WorklogMonitorAgent", "SUCCEEDED")]

    # A reset that fails again starts the count over at 1: the reset happened even though the job raised.
    outcome = run_job(BOOM, settings, reset_circuit=True)
    assert (outcome.consecutive_failures, outcome.circuit_open) == (1, False)
    assert _state("boom_job") == (1, 0, "FAILED")


def test_tick_runs_due_jobs_once_per_interval_and_disabled_jobs_never(tmp_db, monkeypatch):
    calls: list[str] = []

    def counting(session, settings) -> JobResult:
        calls.append("x")
        return JobResult(summary=f"call {len(calls)}")

    card = JobCard("count_job", 60, counting, "COUNT_JOB_ENABLED", "WorklogMonitorAgent", "monitor")
    sched = Scheduler(jobs=(card,), owner="ticker")
    t0 = utcnow()

    assert [o.job for o in sched.tick(t0).ran] == ["count_job"]
    assert sched.tick(t0 + timedelta(seconds=30)).skipped == {"count_job": "not_due"}
    assert [o.job for o in sched.tick(t0 + timedelta(seconds=61)).ran] == ["count_job"]
    assert calls == ["x", "x"]
    assert len(_runs("monitor")) == 2

    monkeypatch.setenv("COUNT_JOB_ENABLED", "false")
    assert sched.tick(t0 + timedelta(seconds=200)).skipped == {"count_job": "disabled"}
    assert calls == ["x", "x"]


# --- the monitor on the scheduler -------------------------------------------------------------


def _open_incident(session, settings, **overrides) -> IncidentRow:
    inc = process_event(session, settings, EventIngest(**{**HUB_EVENT, **overrides}))
    session.refresh(inc)
    return inc


def test_two_monitor_ticks_produce_one_chase_note(tmp_db, clean_hub):
    settings, session = tmp_db
    monitor = job_card("monitor_tick")
    assert monitor is not None and (monitor.interval_s, monitor.graph_name, monitor.agent) == (60, "monitor", "WorklogMonitorAgent")

    # Breach path: restore SLA already passed (what the HITL tests force via POST /monitor/tick).
    breached = _open_incident(session, settings)
    breached.sla_restore_due = utcnow() - timedelta(hours=1)
    # Silence path: 45 min without a vendor note (>= the P2 interval of 30, < the 2x escalation
    # bar), the owner's next update overdue, and no SLA breach (ack/restore due are still ahead).
    silent = _open_incident(session, settings, site_id="SFC-NBIE-HUB-RUA", site_name="Ruai HUB", alarm_code="GENSET_FAIL")
    silent.created_at = utcnow() - timedelta(minutes=45)
    silent.next_update_at = utcnow() - timedelta(minutes=1)
    session.commit()

    first = run_job(monitor, settings)
    assert (first.status, first.summary) == ("SUCCEEDED", "chased=2 notes=2 escalated=1 tasks=1")
    second = run_job(monitor, settings)
    # The breached incident is re-evaluated (still breached; its GENERIC is open so no new task);
    # the silent one is not due again before next_update_at. Neither gets a second note.
    assert (second.status, second.summary) == ("SUCCEEDED", "chased=1 notes=0 escalated=1 tasks=0")

    assert len(_chase_notes(breached.id)) == 1
    assert len(_chase_notes(silent.id)) == 1
    assert [(t, st) for (t, st, *_r) in _runs("monitor")] == [("SCHEDULE", "SUCCEEDED")] * 2
    assert [steps for (*_, steps) in _runs("monitor")] == [[(1, "monitor_tick", "WorklogMonitorAgent", "SUCCEEDED")]] * 2

    # next_update_at is re-armed from sla_minutes[P2].note_interval (30 min × NBI_E 1.0), not +15 m.
    session.refresh(silent)
    assert silent.next_update_at is not None
    assert timedelta(minutes=29) < silent.next_update_at - utcnow() <= timedelta(minutes=30)

    # One GENERIC task on the breached P2, one hitl.created for it, one monitor.chase per note.
    tasks = _read(lambda s: s.scalars(select(HitlTaskRow).where(HitlTaskRow.task_type == "GENERIC")).all())
    assert [t.incident_id for t in tasks] == [breached.id]
    created = [e for e in hub._history if e["type"] == "hitl.created"]
    assert [(e["incident_id"], e["payload"]["incident_number"], e["payload"]["task_type"]) for e in created] == [
        (breached.id, breached.incident_number, "GENERIC")
    ]
    assert len([e for e in hub._history if e["type"] == "monitor.chase"]) == 2

    # After the window a fresh chase note is due again on the breached incident.
    def age_note(s):
        (note,) = s.scalars(select(WorkNoteRow).where(WorkNoteRow.incident_id == breached.id, WorkNoteRow.source == "monitor")).all()
        note.created_at = utcnow() - timedelta(minutes=31)
        s.commit()

    _read(age_note)
    third = run_job(monitor, settings)
    assert third.summary == "chased=1 notes=1 escalated=1 tasks=0"
    assert len(_chase_notes(breached.id)) == 2


def test_manual_chase_still_reports_a_deduped_breach_and_reraises_a_rejected_escalation(tmp_db):
    """The manual tick's contract: a breached incident stays in the results on every pass and a
    rejected GENERIC is re-raised on the next one — the note alone is deduped."""
    settings, session = tmp_db
    inc = _open_incident(session, settings)
    inc.sla_restore_due = utcnow() - timedelta(hours=1)
    session.commit()

    (r1,) = chase_silent_incidents(session, settings.operator)
    assert (r1.action, r1.note_written, r1.task_created) == ("escalated", True, True)
    (r2,) = chase_silent_incidents(session, settings.operator)
    assert (r2.action, r2.note_written, r2.task_created) == ("escalated", False, False)

    task = session.scalar(select(HitlTaskRow).where(HitlTaskRow.incident_id == inc.id, HitlTaskRow.task_type == "GENERIC"))
    task.status = "REJECTED"
    session.commit()
    (r3,) = chase_silent_incidents(session, settings.operator)
    assert (r3.action, r3.note_written, r3.task_created) == ("escalated", False, True)
    assert len(_chase_notes(inc.id)) == 1


# --- over HTTP: lifespan, status and the on-demand route ----------------------------------------


@pytest.fixture()
def make_client(tmp_path, monkeypatch):
    """Reload ``main`` under the env the test set and hand back a live client (lifespan running)."""
    clients: list[TestClient] = []

    def _make(name: str = "app") -> TestClient:
        db = tmp_path / f"{name}.db"
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
        clients.append(c)
        return c

    yield _make

    for c in clients:
        c.__exit__(None, None, None)
    hub._history.clear()
    auth.reset_sessions()
    for key in SCHED_ENV:
        os.environ.pop(key, None)
    import noc_agents.db.models as models
    import noc_agents.main as main

    models._engine = None
    models.SessionLocal = None
    importlib.reload(main)


def test_with_scheduler_disabled_nothing_starts(make_client, monkeypatch):
    monkeypatch.delenv("SCHEDULER_ENABLED", raising=False)
    client = make_client()
    import noc_agents.main as main

    assert main.app.state.scheduler is None
    assert _lease() is None  # no process touched the lease table

    status = client.get("/api/v1/scheduler/status").json()
    assert status == {
        "enabled": False,
        "lease_owner": None,
        "lease_expires_at": None,
        "seconds_since_tick": None,
        "jobs": [
            {
                "name": "outbox_dispatch",
                "interval_s": 5,
                "enabled": True,
                "last_started_at": None,
                "last_status": None,
                "consecutive_failures": 0,
                "circuit_open": False,
            },
            {
                "name": "monitor_tick",
                "interval_s": 60,
                "enabled": True,
                "last_started_at": None,
                "last_status": None,
                "consecutive_failures": 0,
                "circuit_open": False,
            },
            {
                # Phase 2 HITL escalation ladder (§6.5). HITL_ESCALATION_ENABLED, OFF: the card
                # carries default_enabled=False like every feature lane below.
                "name": "hitl_escalation",
                "interval_s": 60,
                "enabled": False,
                "last_started_at": None,
                "last_status": None,
                "consecutive_failures": 0,
                "circuit_open": False,
            },
            {
                # Phase 3 weather lane. "enabled": False with WEATHER_ENABLED unset, unlike
                # the two jobs above: its card sets default_enabled=False, so the status
                # surface never claims a feature lane is on while its flag is absent.
                "name": "weather_regions",
                "interval_s": 900,
                "enabled": False,
                "last_started_at": None,
                "last_status": None,
                "consecutive_failures": 0,
                "circuit_open": False,
            },
            {
                # Phase 3 early warning (§5.3.13): KMD CAP warnings. WEATHER_ENABLED, OFF.
                "name": "kmd_cap",
                "interval_s": 1800,
                "enabled": False,
                "last_started_at": None,
                "last_status": None,
                "consecutive_failures": 0,
                "circuit_open": False,
            },
            {
                # Phase 3 early warning (§5.3.13): GloFAS river discharge. WEATHER_ENABLED, OFF.
                "name": "flood_daily",
                "interval_s": 86400,
                "enabled": False,
                "last_started_at": None,
                "last_status": None,
                "consecutive_failures": 0,
                "circuit_open": False,
            },
            {
                # Phase 4 PIR lane (§7.7.3). Same default_enabled=False reasoning.
                "name": "pir_autoopen",
                "interval_s": 300,
                "enabled": False,
                "last_started_at": None,
                "last_status": None,
                "consecutive_failures": 0,
                "circuit_open": False,
            },
            {
                # Phase 4 regulatory clock (§7.6). Sweeps deadlines and announces
                # countdowns; it can never send a notice on its own.
                "name": "regulatory_sweep",
                "interval_s": 300,
                "enabled": False,
                "last_started_at": None,
                "last_status": None,
                "consecutive_failures": 0,
                "circuit_open": False,
            },
            {
                # Phase 4 vendor scorecards (§7.6.4). Computes; never publishes.
                "name": "scorecard_close",
                "interval_s": 3600,
                "enabled": False,
                "last_started_at": None,
                "last_status": None,
                "consecutive_failures": 0,
                "circuit_open": False,
            },
            {
                # Phase 4 Lane 4C memory (§7.11.5). Advisory only.
                "name": "memory_consolidate",
                "interval_s": 300,
                "enabled": False,
                "last_started_at": None,
                "last_status": None,
                "consecutive_failures": 0,
                "circuit_open": False,
            },
            {
                # Phase 5 complaint follow-up (§7.8.3).
                "name": "complaints_followup",
                "interval_s": 3600,
                "enabled": False,
                "last_started_at": None,
                "last_status": None,
                "consecutive_failures": 0,
                "circuit_open": False,
            },
            {
                # Phase 5 maintenance planner (§7.5). Proposes; never schedules.
                "name": "maintenance_plan_due",
                "interval_s": 3600,
                "enabled": False,
                "last_started_at": None,
                "last_status": None,
                "consecutive_failures": 0,
                "circuit_open": False,
            },
            {
                # Phase 5 maintenance window sweep (§7.5).
                "name": "maintenance_window_sweep",
                "interval_s": 300,
                "enabled": False,
                "last_started_at": None,
                "last_status": None,
                "consecutive_failures": 0,
                "circuit_open": False,
            },
            {
                # Phase 5 capacity scan (§7.5.3). Advises; never acts.
                "name": "capacity_scan",
                "interval_s": 3600,
                "enabled": False,
                "last_started_at": None,
                "last_status": None,
                "consecutive_failures": 0,
                "circuit_open": False,
            },
            {
                # Phase 4 housekeeping (§5.3.22). The ONLY scheduled job that can delete
                # rows, so "enabled": False with the flag unset matters more here than
                # anywhere else -- and even enabling it only permits a dry run, because
                # deleting for real needs HOUSEKEEPING_APPLY as well.
                "name": "housekeeping",
                "interval_s": 86400,
                "enabled": False,
                "last_started_at": None,
                "last_status": None,
                "consecutive_failures": 0,
                "circuit_open": False,
            },
        ],
    }
    assert [c.name for c in SCHEDULED_JOBS] == [
        "outbox_dispatch", "monitor_tick", "hitl_escalation", "weather_regions", "kmd_cap", "flood_daily",
        "pir_autoopen", "regulatory_sweep", "scorecard_close", "memory_consolidate",
        "complaints_followup", "maintenance_plan_due", "maintenance_window_sweep",
        "capacity_scan", "housekeeping",
    ]


def test_with_scheduler_enabled_the_lifespan_ticks_and_releases_on_shutdown(make_client, monkeypatch):
    monkeypatch.setenv("SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("SCHEDULER_TICK_SECONDS", "0.2")
    client = make_client()
    import noc_agents.main as main

    sched = main.app.state.scheduler
    assert sched is not None and sched.running

    deadline = time.time() + 10
    while time.time() < deadline and not (sched.holding and _lease() is not None):
        time.sleep(0.05)
    lease = _lease()
    assert lease is not None and lease[0] == sched.owner

    status = client.get("/api/v1/scheduler/status").json()
    assert status["enabled"] is True
    assert status["lease_owner"] == sched.owner
    assert status["lease_expires_at"].endswith("Z")
    assert 0 <= status["seconds_since_tick"] < 30
    by_name = {j["name"]: j for j in status["jobs"]}
    # Both jobs ran at least once on the first tick: SCHEDULE runs under their own graph names.
    # Wait for the run to REACH a terminal status, not merely to exist. The run row is
    # inserted as RUNNING the moment the job starts and updated when it finishes, so a loop
    # that stops at "a row is present" can read RUNNING and fail the assert below -- a real
    # flake, seen once in a full-suite run and reproducible under load.
    def _settled(graph_name):
        runs = _runs(graph_name)
        return bool(runs) and runs[0][1] in ("SUCCEEDED", "FAILED")

    deadline = time.time() + 10
    while time.time() < deadline and not (_settled("outbox") and _settled("monitor")):
        time.sleep(0.05)
    assert _runs("outbox")[0][:2] == ("SCHEDULE", "SUCCEEDED")
    assert _runs("monitor")[0][:2] == ("SCHEDULE", "SUCCEEDED")
    assert by_name["outbox_dispatch"]["circuit_open"] is False and by_name["monitor_tick"]["circuit_open"] is False

    # Shutdown (what uvicorn's graceful path runs): the task ends and the lease is released.
    client.__exit__(None, None, None)
    assert sched.running is False
    owner, expires_at = _lease()
    assert owner == sched.owner and expires_at <= utcnow()
    assert client.get("/api/v1/scheduler/status").status_code == 200  # the route itself needs no loop


def test_run_route_runs_on_demand_resets_the_circuit_and_404s_unknown_jobs(make_client, monkeypatch):
    monkeypatch.delenv("SCHEDULER_ENABLED", raising=False)
    client = make_client()
    from noc_agents.config import get_settings

    # Open the circuit on the real monitor job by name, then ask the route to run it.
    broken = JobCard("monitor_tick", 60, boom, "SCHEDULER_MONITOR_ENABLED", "WorklogMonitorAgent", "monitor")
    for _ in range(3):
        run_job(broken, get_settings())
    assert _state("monitor_tick") == (3, 1, "FAILED")
    before = {j["name"]: j for j in client.get("/api/v1/scheduler/status").json()["jobs"]}["monitor_tick"]
    assert (before["consecutive_failures"], before["circuit_open"], before["last_status"]) == (3, True, "FAILED")

    r = client.post("/api/v1/scheduler/run/monitor_tick")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["job"], body["status"], body["consecutive_failures"], body["circuit_open"]) == ("monitor_tick", "SUCCEEDED", 0, False)
    assert body["summary"] == "chased=0 notes=0 escalated=0 tasks=0"
    after = {j["name"]: j for j in client.get("/api/v1/scheduler/status").json()["jobs"]}["monitor_tick"]
    assert (after["consecutive_failures"], after["circuit_open"], after["last_status"]) == (0, False, "SUCCEEDED")
    assert after["last_started_at"].endswith("Z")

    assert client.post("/api/v1/scheduler/run/no_such_job").status_code == 404
    # The manual monitor tick is untouched.
    assert client.post("/api/v1/monitor/tick").json() == {"chased": 0, "results": []}


def test_run_route_is_admin_only_once_auth_is_on(make_client, monkeypatch):
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("NOC_SESSION_SECRET", SECRET)
    client = make_client()

    assert client.post("/api/v1/scheduler/run/monitor_tick").status_code == 401
    client.cookies.set(auth.SESSION_COOKIE, auth.sign_session({"sub": "u-1", "role": "shift_supervisor"}, SECRET))
    assert client.post("/api/v1/scheduler/run/monitor_tick").status_code == 403
    client.cookies.set(auth.SESSION_COOKIE, auth.sign_session({"sub": "u-2", "role": "admin"}, SECRET))
    assert client.post("/api/v1/scheduler/run/monitor_tick").status_code == 200
    # Status is a read-only health view: open, like /health.
    assert client.get("/api/v1/scheduler/status").status_code == 200


def test_status_payload_reads_the_db_not_this_process(tmp_db):
    """Any process answers for the one that ticks: the payload comes from the lease row."""
    settings, session = tmp_db
    other = Scheduler(jobs=(), owner="the-real-ticker")
    t0 = utcnow()
    assert other.tick(t0).held is True

    payload = status_payload(session, now=t0 + timedelta(seconds=7))
    assert payload["lease_owner"] == "the-real-ticker"
    assert payload["seconds_since_tick"] == 7.0
    assert payload["lease_expires_at"].replace(tzinfo=None) == t0 + LEASE_TTL  # stamped Z for the wire
