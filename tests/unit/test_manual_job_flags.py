"""Every scheduled job honours its own flag when run by hand (CONFORMANCE A-10).

``POST /api/v1/scheduler/run/{job}`` calls :func:`run_job` directly, so the loop's own
``job_enabled`` check is not in the path. Each job function therefore has to re-check its
flag itself. Two did not: ``outbox_dispatch`` drained the queue during a freeze -- with
``EMAIL_ENABLED=false`` that marks queued mail SENT via the mock provider, so it is never
delivered -- and ``monitor_tick`` wrote chase notes. This file runs EVERY registered job with
every flag explicitly false against a database that has real work waiting for each of the ten
jobs registered today, and proves nothing moves:

* ``outbox_dispatch`` -- a PENDING email; ``monitor_tick`` -- a breached open incident;
* ``weather_regions`` -- the catalogue's region centroids, with a fake provider standing in
  for the network (it records any call, so a regression cannot reach the internet);
* ``pir_autoopen`` -- a P2 restored an hour ago with no review;
* ``regulatory_sweep`` -- a DRAFT notice one hour from its deadline;
* ``complaints_followup`` -- a complaint past its follow-up date;
* ``maintenance_plan_due`` -- a monthly plan written 40 days ago, never completed;
* ``maintenance_window_sweep`` -- a SCHEDULED window whose night ended an hour ago;
* ``capacity_scan`` -- seven days of evening busy hours at 82 % on one cell;
* ``housekeeping`` -- a SENT email past the 90-day payload-archive age (a retention candidate).

"Nothing moves" is checked on every table in the database (row for row, every column), less
the run's own bookkeeping, plus the housekeeping backup directory.

The positive controls matter as much as the negative: the same seeds with each flag UNSET
(outbox, monitor) or switched on (every lane) make that job write, so "nothing changed" below
is a statement about the flags, not about a fixture that happened to have nothing to do. A
job registered after these ten is still run and still held to the whole-database snapshot,
but has no seeded work here until someone adds one.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import func, inspect, select, text

from noc_agents.adapters.weather import WeatherError
from noc_agents.db.models import IncidentRow, OutboxRow, WorkNoteRow, get_session, new_id, utcnow
from noc_agents.db.models_capacity import DEFAULT_METRIC, CapacityObservationRow
from noc_agents.db.models_complaints import RelationshipComplaintRow
from noc_agents.db.models_maintenance import MaintenancePlanRow, MaintenanceWindowRow
from noc_agents.db.models_regulatory import RegulatoryNotificationRow
from noc_agents.domain.schemas import EventIngest
from noc_agents.graph.pipeline import process_event
from noc_agents.orchestrator.outbox import enqueue
from noc_agents.orchestrator.contract import SUCCEEDED
from noc_agents.pollers import weather as weather_poller
from noc_agents.scheduler.loop import SCHEDULED_JOBS, job_card, run_job

HUB_EVENT = {
    "site_id": "SFC-NBIE-HUB-EMB",
    "site_name": "Embakasi East Aggregation HUB",
    "site_type": "HUB",
    "region_code": "NBI_E",
    "alarm_code": "POWER_GRID_FAIL",
    "failure_domain": "POWER",
    "users_affected": 450000,
}

#: Second keys some lanes carry on top of their card flag. Pinned false too, so "every flag
#: off" really is every flag (conftest already pins these; restating it keeps the file honest
#: if conftest ever changes).
EXTRA_OFF = ("HOUSEKEEPING_APPLY", "EMAIL_ENABLED", "SCHEDULER_ENABLED")


def _seed(session, settings) -> str:
    """A breached open incident (monitor work) and a PENDING email (outbox work)."""
    inc = process_event(session, settings, EventIngest(**HUB_EVENT))
    session.refresh(inc)
    inc.sla_restore_due = utcnow() - timedelta(hours=1)
    inc.next_update_at = utcnow() - timedelta(minutes=1)
    enqueue(
        session,
        kind="EMAIL",
        idempotency_key="a10-manual-run-probe",
        payload={"operator_id": settings.operator.operator_id, "to": ["noc@example.invalid"], "subject": "probe", "body": "probe"},
        operator_id=settings.operator.operator_id,
        incident_id=inc.id,
    )
    session.commit()
    return inc.id


#: Sites the lane seeds sit on. Different sites on purpose, so one lane's seed cannot change
#: another's verdict (a maintenance window on the capacity cell's site would exclude samples).
PIR_SITE = "SFC-NBI-BTS-001"
PLAN_SITE = "SFC-NBIE-HUB-EMB"
WINDOW_SITE = "SFC-NBI-BTS-002"
CAPACITY_SITE, CAPACITY_CELL = "SFC-NBIE-ENB-KAY12", "KAY12-L1800-1"


def _seed_lane_work(session, settings, anchor_incident_id: str) -> None:
    """Real work for each of the eight lane jobs (one row, or one small set, per lane).

    Written straight to the tables the jobs read, so the seed does not depend on any lane
    being switched on to create it. Each lane's positive control below proves its seed is
    work that job really does when its flag is on.
    """
    now = utcnow()
    op = settings.operator.operator_id
    # pir_autoopen: a P2 restored an hour ago and no review yet. P1/P2 is the first
    # spec 5.3.18 trigger rule, so no other fact is needed for it to qualify.
    session.add(
        IncidentRow(
            operator_id=op,
            incident_number="INC-A10-PIR",
            status="RESTORED",
            priority="P2",
            users_affected=5000,
            site_id=PIR_SITE,
            site_type="BTS",
            region_code="NBI",
            correlation_fingerprint="fp-a10-pir",
            failure_domain="POWER",
            alarm_code="PWR_MAINS_FAIL",
            failure_time=now - timedelta(hours=3),
            restored_at=now - timedelta(hours=1),
            sla_restore_due=now + timedelta(hours=1),
            created_at=now - timedelta(hours=3),
            updated_at=now - timedelta(hours=1),
        )
    )
    # regulatory_sweep: an open DRAFT notice one hour from its deadline, so both the 12 h
    # and the 2 h countdown thresholds are crossed and neither is marked fired yet.
    session.add(
        RegulatoryNotificationRow(
            operator_id=op,
            kind="CA_OUTAGE_24H",
            incident_id=anchor_incident_id,
            clock_started_at=now - timedelta(hours=23),
            due_at=now + timedelta(hours=1),
            status="DRAFT",
        )
    )
    # complaints_followup: an OPEN complaint a day past its follow-up date.
    session.add(
        RelationshipComplaintRow(
            operator_id=op,
            filed_by="A-10 probe analyst",
            filed_at=now - timedelta(days=8),
            subject_type="VENDOR",
            category="NO_SHOW",
            description="Field engineer did not arrive for the A-10 probe",
            severity="LOW",
            status="OPEN",
            assigned_manager="A-10 probe manager",
            follow_up_due_at=now - timedelta(days=1),
            retention_until=now + timedelta(days=700),
        )
    )
    # maintenance_plan_due: a 30-day plan written 40 days ago with no completion recorded,
    # so its first occurrence fell due ten days ago (basis: plan created).
    session.add(
        MaintenancePlanRow(
            operator_id=op,
            site_id=PLAN_SITE,
            task_type="GENERATOR_EXERCISE",
            interval_days=30,
            standard_ref="A-10 probe",
            created_at=now - timedelta(days=40),
        )
    )
    # maintenance_window_sweep: a SCHEDULED window whose night ended an hour ago.
    session.add(
        MaintenanceWindowRow(
            operator_id=op,
            scope="SITE",
            scope_ref=WINDOW_SITE,
            starts_at=now - timedelta(hours=6),
            ends_at=now - timedelta(hours=1),
            uid=f"{new_id()}@noc.local",
            organizer="noc@example.invalid",
            attendees_ref="audiences.FE_ONCALL",
            status="SCHEDULED",
        )
    )
    # capacity_scan: seven days of three evening busy hours at 82 % on one cell, the
    # sustained case tests/unit/test_capacity.py pins (15-17 UTC = 18-20 EAT, one EAT day).
    for day in range(1, 8):
        base = (now - timedelta(days=day)).replace(hour=0, minute=0, second=0, microsecond=0)
        for hour in (15, 16, 17):
            session.add(
                CapacityObservationRow(
                    operator_id=op,
                    site_id=CAPACITY_SITE,
                    cell_id=CAPACITY_CELL,
                    metric=DEFAULT_METRIC,
                    value=82.0,
                    busy_hour_at=base + timedelta(hours=hour),
                    source="CSV",
                )
            )
    # housekeeping: a SENT email untouched for 100 days, past outbox_terminal's 90-day
    # payload-archive age in config/retention.yaml -- a retention candidate.
    old = now - timedelta(days=100)
    session.add(
        OutboxRow(
            operator_id=op,
            kind="EMAIL",
            idempotency_key="a10-retention-candidate",
            payload_json=json.dumps({"operator_id": op, "to": ["noc@example.invalid"], "subject": "old", "body": "old"}),
            status="SENT",
            attempts=1,
            provider="smtp",
            sent_at=old,
            created_at=old,
            updated_at=old,
        )
    )
    session.commit()


class _NoNetworkWeather:
    """Stands in for the weather provider: records every forecast request and fails it, so a
    weather job that ran would write its failure markers and never reach the network."""

    source = "OPEN_METEO"

    def __init__(self) -> None:
        self.calls: list[tuple[float, float]] = []

    def forecast(self, lat, lon, **_kwargs):
        self.calls.append((lat, lon))
        raise WeatherError("network", "A-10 probe: no network in tests", source=self.source)


@pytest.fixture()
def fake_weather(monkeypatch):
    fake = _NoNetworkWeather()
    monkeypatch.setattr(weather_poller, "provider_from_env", lambda *_a, **_k: fake)
    return fake


#: What ``run_job`` writes whatever the job does: the run, its step, the job's circuit state.
#: The step's own ``audit_events`` row (graph/instrumentation.complete_step, action
#: ``step.<status>``) is bookkeeping too and is filtered out below; every other audit row --
#: a transfer record, a maintenance proposal, the housekeeping report -- counts as a write.
RUN_BOOKKEEPING = frozenset({"agent_runs", "agent_run_steps", "scheduled_job_state"})


def _snapshot():
    """Every row of every table, every column, read on a fresh session (less the run's own
    bookkeeping). Whole-database rather than a hand-picked list, so a lane that writes
    somewhere nobody thought to look is still caught."""
    s = get_session()
    try:
        out = {}
        for table in sorted(inspect(s.get_bind()).get_table_names()):
            if table in RUN_BOOKKEEPING:
                continue
            sql = f'SELECT * FROM "{table}"'
            if table == "audit_events":
                sql += " WHERE action NOT LIKE 'step.%'"
            out[table] = sorted(tuple(repr(v) for v in row) for row in s.execute(text(sql)).all())
        return out
    finally:
        s.close()


def _changed(before, after) -> list[str]:
    return sorted(t for t in set(before) | set(after) if before.get(t) != after.get(t))


def _backups(tmp_path) -> list[str]:
    """housekeeping's daily backup lands next to the database file (tmp_db: tmp_path/test.db)."""
    folder = tmp_path / "backups"
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def test_every_job_skips_and_writes_nothing_when_its_flag_is_false(tmp_db, tmp_path, monkeypatch, fake_weather):
    settings, session = tmp_db
    inc_id = _seed(session, settings)
    _seed_lane_work(session, settings, inc_id)
    for card in SCHEDULED_JOBS:
        monkeypatch.setenv(card.enabled_env, "false")
    for name in EXTRA_OFF:
        monkeypatch.setenv(name, "false")

    before = _snapshot()
    backups_before = _backups(tmp_path)
    s = get_session()
    try:
        probe = s.scalar(select(OutboxRow).where(OutboxRow.idempotency_key == "a10-manual-run-probe"))
        assert probe.status == "PENDING", "seed must leave mail waiting"
    finally:
        s.close()

    for card in SCHEDULED_JOBS:
        # reset_circuit=True is exactly what the on-demand route passes.
        outcome = run_job(card, settings, reset_circuit=True)
        assert outcome.status == SUCCEEDED, (card.name, outcome.error)
        # Each job says it is off, naming its flag -- zero counts would read as "looked and
        # found nothing", which is a different statement from "did not look".
        assert card.enabled_env in outcome.summary, (card.name, outcome.summary)
        after = _snapshot()
        assert after == before, f"{card.name} wrote to {_changed(before, after)} with {card.enabled_env}=false"
        assert _backups(tmp_path) == backups_before, f"{card.name} wrote a backup with {card.enabled_env}=false"
    assert fake_weather.calls == [], "the weather job asked for a forecast with WEATHER_ENABLED=false"


def test_outbox_and_monitor_skip_with_their_flag_false(tmp_db, monkeypatch):
    """The two jobs A-10 was about, each alone: only its own flag off, everything else as default."""
    settings, session = tmp_db
    _seed(session, settings)
    before = _snapshot()

    monkeypatch.setenv("OUTBOX_DISPATCH_ENABLED", "false")
    out = run_job(job_card("outbox_dispatch"), settings, reset_circuit=True)
    assert (out.status, out.summary) == (SUCCEEDED, "outbox_dispatch skipped: OUTBOX_DISPATCH_ENABLED is off")

    monkeypatch.setenv("SCHEDULER_MONITOR_ENABLED", "false")
    mon = run_job(job_card("monitor_tick"), settings, reset_circuit=True)
    assert (mon.status, mon.summary) == (SUCCEEDED, "monitor_tick skipped: SCHEDULER_MONITOR_ENABLED is off")

    assert _snapshot() == before


@pytest.mark.parametrize("unset", [True, False], ids=["unset", "explicit-true"])
def test_outbox_and_monitor_still_run_when_the_flag_is_unset_or_true(tmp_db, monkeypatch, unset):
    """Positive control: both cards default ON, so an UNSET flag must behave as before."""
    settings, session = tmp_db
    inc_id = _seed(session, settings)
    for name in ("OUTBOX_DISPATCH_ENABLED", "SCHEDULER_MONITOR_ENABLED"):
        if unset:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, "true")

    out = run_job(job_card("outbox_dispatch"), settings, reset_circuit=True)
    assert out.status == SUCCEEDED and "skipped" not in out.summary, out.summary
    s = get_session()
    try:
        probe = s.scalar(select(OutboxRow).where(OutboxRow.idempotency_key == "a10-manual-run-probe"))
        assert probe.status != "PENDING", "the drain must have claimed the waiting email"
    finally:
        s.close()

    mon = run_job(job_card("monitor_tick"), settings, reset_circuit=True)
    assert mon.status == SUCCEEDED and "skipped" not in mon.summary, mon.summary
    s = get_session()
    try:
        chase = s.scalar(
            select(func.count()).select_from(WorkNoteRow).where(WorkNoteRow.incident_id == inc_id, WorkNoteRow.source == "monitor")
        )
        assert chase >= 1, "the breached incident must have been chased"
    finally:
        s.close()


#: The table each lane job writes when its seed is real work and its flag is on. This is the
#: other half of the all-flags-off test: a seed that stopped qualifying would turn "nothing
#: moved" into "nothing to move", and this is what catches that.
LANE_WRITES = {
    "weather_regions": "external_signals",  # failure markers from the fake provider
    "pir_autoopen": "post_incident_reviews",
    "regulatory_sweep": "regulatory_notifications",  # countdown thresholds marked fired
    "complaints_followup": "outbox",  # the manager reminder
    "maintenance_plan_due": "maintenance_tasks",
    "maintenance_window_sweep": "maintenance_windows",  # SCHEDULED -> COMPLETED
    "capacity_scan": "capacity_advisories",
    "housekeeping": "audit_events",  # its report row (dry run: HOUSEKEEPING_APPLY stays false)
}


@pytest.mark.parametrize("name", sorted(LANE_WRITES))
def test_each_lane_seed_is_real_work_when_its_flag_is_on(tmp_db, monkeypatch, fake_weather, name):
    """Positive control, one lane at a time: its own flag on, every other lane as conftest
    pins it (off). The same seed and the same snapshot as the all-flags-off test."""
    settings, session = tmp_db
    inc_id = _seed(session, settings)
    _seed_lane_work(session, settings, inc_id)
    card = job_card(name)
    assert card is not None, f"{name} is not registered"
    monkeypatch.setenv(card.enabled_env, "true")

    before = _snapshot()
    outcome = run_job(card, settings, reset_circuit=True)

    assert outcome.status == SUCCEEDED, (name, outcome.error)
    assert "skipped" not in outcome.summary, outcome.summary
    changed = _changed(before, _snapshot())
    assert LANE_WRITES[name] in changed, f"{name} with {card.enabled_env}=true changed only {changed}"
    if name == "weather_regions":
        assert fake_weather.calls, "the weather job ran but asked for no forecast"
    if name == "housekeeping":
        # The seeded retention candidate is found (dry run: counted, not archived).
        assert "would archive 1 payload" in outcome.summary, outcome.summary
