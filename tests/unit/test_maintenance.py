"""Planned maintenance — spec §7.5, Phase 5 Lane 5A.

Four things this file exists to protect, in order of how much damage their absence does.

**A window must not be executable on the strength of the plan's approval alone.** Planned
maintenance takes live customers off air on purpose. ``APPROVE_SCHEDULE`` signs off the
programme; ``APPROVE_MAINTENANCE_WINDOW`` signs off going ahead on the night, which is the
decision that knows whether the CA reference came back, whether the customer notice went out
and what the sky looks like.
``test_a_window_is_not_executable_on_the_strength_of_the_schedule_approval_alone`` enumerates
the bypasses rather than testing the happy path twice: no card at all, an unresolved card, an
APPROVED ``APPROVE_SCHEDULE`` on every task inside the very same window, an approval of a
*different* window, an agent or an autonomy policy as the approver, and the raiser approving
their own window. Every one is refused and leaves the window PROPOSED.

**The rain guard must never report CLEAR when it has no forecast.** ``WEATHER_ENABLED``
defaults to false, so "no forecast" is the *normal* case, and a guard that maps it onto CLEAR
launders an absence of evidence into an assurance somebody acts on at 01:00 up a tower.
``test_the_rain_guard_never_says_clear_when_it_has_no_forecast`` pins the three-valued
vocabulary, and ``test_a_fresh_storm_forecast_refuses_until_a_named_human_overrides`` pins
which of the three actually refuses.

**The planned-window stop clock is a proposal and only a proposal.** Stop-clock minutes are
deducted from a vendor's SLA figure, which is commercial.
``test_a_planned_window_proposal_never_opens_a_stop_clock_event`` asserts the clock-events
table is still empty after the proposal is read — twice, and through the API as well.

**Overlap is decided, not emergent.** Windows may overlap while PROPOSED; a second one may
not reach SCHEDULED over an intersecting scope and period. Half-open intervals, so
back-to-back windows are not a clash.

No network: every weather row in this file is written directly to ``external_signals``, which
is the same cache ``pollers.weather.weather_risk_for_region`` reads.
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from noc_agents.api import auth
from noc_agents.api.deps import _owned
from noc_agents.config import get_settings
from noc_agents.db.models import ExternalSignalRow, HitlTaskRow, IncidentRow, new_id, utcnow
from noc_agents.db.models_maintenance import (
    MaintenancePlanRow,
    MaintenanceTaskRow,
    MaintenanceWindowRow,
)
from noc_agents.db.models_vendors import ClockEventRow
from noc_agents.domain.enums import HitlTaskType
from noc_agents.realtime.hub import hub
from noc_agents.services import maintenance as svc
from noc_agents.services.clock_events import list_clock_events
from noc_agents.services.hitl import sync_incident_hitl_scalars

# A November night: 00:00–05:00 EAT on 2026-11-12, stored as naive UTC (21:00–02:00 the day
# before). The three-hour offset is the point of using this instant rather than a round UTC
# number — a window written as if EAT were UTC would start at 03:00 EAT, in the morning peak.
# November is in the OND short rains, so it is also the rain-season case.
WINDOW_START = datetime(2026, 11, 11, 21, 0, 0)
WINDOW_END = datetime(2026, 11, 12, 2, 0, 0)
#: A July night: outside both MAM and OND.
DRY_START = datetime(2026, 7, 8, 21, 0, 0)
DRY_END = datetime(2026, 7, 9, 2, 0, 0)

SITE = "SFC-MTK-HUB-THK"  # region MTK, site_class HUB
SITE_NBI = "SFC-NBIE-HUB-EMB"  # region NBI_E
APPROVER = "Grace Wanjiru"  # a named human; never an agent: or policy: principal
RAISER = "Peter Kamau"


# ------------------------------------------------------------------------------- fixtures


@pytest.fixture()
def on(monkeypatch):
    """``MAINTENANCE_ENABLED=true``. Everything except the flag-off tests needs it."""
    monkeypatch.setenv("MAINTENANCE_ENABLED", "true")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A live API on its own SQLite file, with the lane armed."""
    db = tmp_path / "maintenance.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
    monkeypatch.setenv("MAINTENANCE_ENABLED", "true")

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


def _cfg():
    return get_settings().operator


def _incident(session, *, number: str = "INC000901", **overrides) -> IncidentRow:
    """An incident, for the tests that are genuinely ABOUT one: the stop-clock proposal, the
    ENRICH tag, a window raised because of an outage, and "an unrelated outage is left alone".

    Until schema_version 8 this was seeded by every test that raised a card, as the *scoping
    anchor* the card had to borrow (``hitl_tasks.incident_id`` was NOT NULL and ownership was
    derived through it). A card is owned directly now, so those tests no longer seed one — which
    makes each of them run against a database with no incident in it at all.
    """
    values = dict(
        operator_id="safaricom",
        incident_number=number,
        status="IN_PROGRESS",
        priority="P3",
        users_affected=1000,
        site_id=SITE,
        site_name="Thika Hub",
        site_type="HUB",
        region_code="MTK",
        county="Kiambu",
        correlation_fingerprint=f"fp-maint-{number}",
        failure_time=WINDOW_START + timedelta(minutes=30),
    )
    values.update(overrides)
    inc = IncidentRow(**values)
    session.add(inc)
    session.flush()
    return inc


def _plan(session, **overrides) -> MaintenancePlanRow:
    values = dict(
        task_type="GENERATOR_EXERCISE",
        site_id=SITE,
        interval_days=30,
        standard_ref="NFPA 110 practice (secondary source)",
    )
    values.update(overrides)
    return svc.create_plan(session, _cfg(), values, actor=RAISER)


def _window(session, **overrides) -> MaintenanceWindowRow:
    values = dict(scope="SITE", scope_ref=SITE, starts_at=WINDOW_START, ends_at=WINDOW_END)
    values.update(overrides)
    return svc.create_window(session, _cfg(), values, actor=RAISER)


def _approve(session, card: HitlTaskRow, *, by: str = APPROVER) -> HitlTaskRow:
    """What ``POST /api/v1/hitl/{id}/approve`` does for a task type main.py does not
    special-case: it records the decision and releases nothing."""
    card.status = "APPROVED"
    card.resolved_by = by
    card.resolved_at = utcnow()
    session.flush()
    return card


def _forecast(session, region: str, *, storm: bool, now: datetime | None = None, age_h: float = 0.0) -> ExternalSignalRow:
    """One weather row straight into the cache ``weather_risk_for_region`` reads. No network."""
    now = now or utcnow()
    fetched = now - timedelta(hours=age_h)
    row = ExternalSignalRow(
        id=new_id(),
        operator_id="safaricom",
        source="OPEN_METEO",
        source_url="https://api.open-meteo.com/test-fixture",
        external_id=f"{region}:{fetched.isoformat()}",
        region_code=region,
        fetched_at=fetched,
        valid_from=fetched,
        valid_until=fetched + timedelta(hours=1),
        stale=0,
        storm_flag=1 if storm else 0,
        payload_json="{}",
        derived_json=json.dumps({"storm_flag": bool(storm), "level": "HIGH" if storm else "LOW"}),
        created_at=fetched,
    )
    session.add(row)
    session.flush()
    return row


# ------------------------------------------------------------------ the flag is off by default


def test_the_whole_lane_is_inert_with_the_flag_off(tmp_db, monkeypatch):
    """§7.5: with ``MAINTENANCE_ENABLED`` unset the system behaves exactly as it does today."""
    monkeypatch.delenv("MAINTENANCE_ENABLED", raising=False)
    settings, session = tmp_db
    assert svc.maintenance_enabled() is False

    inc = _incident(session)
    # No window can be "live" and no stop clock can be proposed, whatever is in the tables.
    assert svc.is_planned(session, "safaricom", SITE, utcnow()) is None
    assert svc.stop_clock_proposal(session, inc) is None

    # Both jobs say so rather than quietly doing nothing.
    for job in (svc.MAINTENANCE_PLAN_DUE_JOB, svc.MAINTENANCE_WINDOW_SWEEP_JOB):
        assert job.default_enabled is False, "a feature lane's card must not claim to be enabled"
        assert job.enabled_env == "MAINTENANCE_ENABLED"
        result = job.fn(session, settings)
        assert "MAINTENANCE_ENABLED=false" in result.summary


def test_every_route_404s_with_the_flag_off(tmp_path, monkeypatch):
    """A 404 and not a 403: today's system has no ``/maintenance`` surface at all, and a 403
    would announce that the feature exists."""
    db = tmp_path / "maint_off.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.delenv("MAINTENANCE_ENABLED", raising=False)

    import noc_agents.config as cfg
    import noc_agents.db.models as models
    import noc_agents.main as main

    cfg.clear_settings_cache()
    models._engine = None
    models.SessionLocal = None
    importlib.reload(main)
    with TestClient(main.app) as c:
        for path in ("/api/v1/maintenance/plans", "/api/v1/maintenance/tasks", "/api/v1/maintenance/windows"):
            r = c.get(path)
            assert r.status_code == 404, f"{path} answered {r.status_code}"
            assert "MAINTENANCE_ENABLED" in r.text
        assert c.post("/api/v1/maintenance/plans", json={"task_type": "TOWER_VISUAL", "standard_ref": "x"}).status_code == 404
    models._engine = None
    models.SessionLocal = None
    cfg.clear_settings_cache()
    importlib.reload(main)


# ------------------------------------------------------------------------ due-date arithmetic


def test_next_due_is_measured_from_the_last_recorded_completion(tmp_db, on):
    _settings, session = tmp_db
    plan = _plan(session, interval_days=30)
    last = datetime(2026, 9, 1, 8, 0, 0)
    due, basis = svc.next_due(plan, last_completed_at=last)
    assert due == datetime(2026, 10, 1, 8, 0, 0)
    assert basis == svc.BASIS_LAST_COMPLETION


def test_next_due_with_no_completion_declares_that_it_is_an_assumption(tmp_db, on):
    """The system does not know when the generator was last exercised. It answers, and it says
    the answer is assumed — never the same statement as a measured completion."""
    _settings, session = tmp_db
    plan = _plan(session, interval_days=30)
    due, basis = svc.next_due(plan, last_completed_at=None)
    assert basis == svc.BASIS_PLAN_CREATED
    assert basis != svc.BASIS_LAST_COMPLETION
    assert due == plan.created_at + timedelta(days=30)


def test_a_consumption_driven_plan_gets_no_calendar_due_date(tmp_db, on):
    """FUEL_RUN follows the burn rate and the last fill, not a fixed cycle. Refusing to invent
    a date is the point: a fuel run on the wrong day is a wasted truck roll or a dry site."""
    _settings, session = tmp_db
    plan = _plan(session, task_type="FUEL_RUN", interval_days=None, consumption_driven=True)
    assert svc.plan_interval(plan) is None
    due, basis = svc.next_due(plan, last_completed_at=datetime(2026, 9, 1))
    assert due is None and basis == svc.BASIS_CONSUMPTION


def test_hours_and_days_add_so_a_running_hour_plan_is_expressible(tmp_db, on):
    _settings, session = tmp_db
    plan = _plan(session, interval_days=90, interval_hours=12)
    assert svc.plan_interval(plan) == timedelta(days=90, hours=12)


@pytest.mark.parametrize(
    "values, fragment",
    [
        ({"task_type": "TOWER_VISUAL", "standard_ref": "TIA-222"}, "exactly one of site_id or site_class"),
        (
            {"task_type": "TOWER_VISUAL", "standard_ref": "TIA-222", "site_id": SITE, "site_class": "HUB"},
            "exactly one of site_id or site_class",
        ),
        ({"task_type": "POLISH_THE_TOWER", "standard_ref": "x", "site_id": SITE}, "unknown task_type"),
        ({"task_type": "TOWER_VISUAL", "standard_ref": "x", "site_id": SITE}, "interval_days"),
        ({"task_type": "TOWER_VISUAL", "standard_ref": " ", "site_id": SITE, "interval_days": 365}, "standard_ref is required"),
    ],
)
def test_a_plan_that_cannot_be_acted_on_is_refused(values, fragment):
    with pytest.raises(ValueError) as err:
        svc.validate_plan(values)
    assert fragment in str(err.value)


def test_a_class_scoped_plan_resolves_through_both_site_vocabularies(tmp_db, on):
    """Planning may say HUB (the catalogue's site_class) or CRITICAL (the profile's criticality
    banding). Both are sensible sentences and both resolve."""
    _settings, session = tmp_db
    by_catalogue = _plan(session, site_id=None, site_class="HUB", task_type="TOWER_VISUAL", interval_days=365)
    by_criticality = _plan(session, site_id=None, site_class="CRITICAL", task_type="TOWER_VISUAL", interval_days=365)
    hubs = svc.plan_sites(by_catalogue, _cfg())
    critical = svc.plan_sites(by_criticality, _cfg())
    assert SITE in hubs and len(hubs) > 1
    assert SITE in critical


def test_the_proposed_assignee_is_a_role_token_and_never_a_person(tmp_db, on):
    """§7.11.8. The region roster's ``fe_oncall`` is already a token (``FE-MTK-01``)."""
    _settings, session = tmp_db
    plan = _plan(session)
    token = svc.propose_assignee(plan, SITE, _cfg())
    assert token, "the MTK region roster names an fe_oncall"
    assert token == token.upper() and " " not in token, "a token, not 'Kevin Ochieng'"
    # An owner vendor wins over the region's field engineer: it is that vendor's work.
    owned = _plan(session, owner_vendor_id="EGYPRO")
    assert svc.propose_assignee(owned, SITE, _cfg()) == "EGYPRO"


# ------------------------------------------------------------------------------- THE TWO GATES


def test_a_window_is_not_executable_on_the_strength_of_the_schedule_approval_alone(tmp_db, on):
    """THE headline property of this lane (§7.5, module docstring).

    Every bypass is enumerated. After each one the window is still PROPOSED, which is the
    status in which it cannot take a single customer off air.
    """
    _settings, session = tmp_db
    plan = _plan(session)
    window = _window(session)
    task, _created = svc.propose_task(session, plan, SITE, _cfg(), due_at=WINDOW_START)
    task.window_id = window.id
    session.flush()

    # (1) No card at all.
    with pytest.raises(svc.WindowNotApproved, match="no APPROVE_MAINTENANCE_WINDOW card"):
        svc.schedule_window(session, window, _cfg(), actor=APPROVER)
    assert window.status == "PROPOSED"

    # (2) The PROGRAMME is approved — every task in the window carries an APPROVED
    #     APPROVE_SCHEDULE card, owned by the same operator. The window is still not
    #     approved to go ahead, and this is the line the whole lane turns on.
    schedule_card = svc.request_schedule_approval(session, task, plan, _cfg())
    _approve(session, schedule_card)
    svc.mark_scheduled(session, task, actor=APPROVER)
    assert task.status == "SCHEDULED", "the programme sign-off did happen"
    with pytest.raises(svc.WindowNotApproved):
        svc.schedule_window(session, window, _cfg(), actor=APPROVER)
    assert window.status == "PROPOSED"

    # (3) Pointing the window at that same approved APPROVE_SCHEDULE card is refused on
    #     task_type: approving a programme is not approving a night.
    window.hitl_task_id = schedule_card.id
    session.flush()
    with pytest.raises(svc.WindowNotApproved, match="not a APPROVE_MAINTENANCE_WINDOW"):
        svc.schedule_window(session, window, _cfg(), actor=APPROVER)
    assert window.status == "PROPOSED"

    # (4) An APPROVE_MAINTENANCE_WINDOW card that was raised for a DIFFERENT window.
    other = _window(session, scope_ref=SITE_NBI, starts_at=DRY_START, ends_at=DRY_END)
    other_card = svc.request_window_approval(session, other, _cfg())
    _approve(session, other_card)
    window.hitl_task_id = other_card.id
    session.flush()
    with pytest.raises(svc.WindowNotApproved, match="not this one"):
        svc.schedule_window(session, window, _cfg(), actor=APPROVER)
    assert window.status == "PROPOSED"

    # (5) This window's own card, but still PENDING.
    window.hitl_task_id = None
    session.flush()
    card = svc.request_window_approval(session, window, _cfg())
    with pytest.raises(svc.WindowNotApproved, match="is PENDING, not APPROVED"):
        svc.schedule_window(session, window, _cfg(), actor=APPROVER)

    # (6) Approved — by an agent, and by an autonomy policy. Neither takes customers off air.
    for principal in ("agent:MaintenancePlanningAgent", "policy:L2_GUARDED"):
        _approve(session, card, by=principal)
        with pytest.raises(svc.WindowNotApproved, match="named human"):
            svc.schedule_window(session, window, _cfg(), actor=APPROVER)
        assert window.status == "PROPOSED"

    # (7) The raiser approving their own card (§6.5).
    card.created_by = APPROVER
    _approve(session, card, by=APPROVER)
    with pytest.raises(svc.WindowNotApproved, match="§6.5"):
        svc.schedule_window(session, window, _cfg(), actor=APPROVER)
    assert window.status == "PROPOSED"

    # And now, and only now, the window may go ahead.
    card.created_by = svc.MAINTENANCE_RAISER
    _approve(session, card, by=APPROVER)
    svc.schedule_window(session, window, _cfg(), actor=APPROVER)
    assert window.status == "SCHEDULED"
    assert window.approved_by == APPROVER
    assert card.entity_type == "maintenance_window" and card.entity_id == window.id
    # All seven refusals and the one success happened on a database with NO incident in it.
    # Until schema v8 none of it could: every card above needed an incident to be filed against.
    assert session.scalar(select(func.count()).select_from(IncidentRow)) == 0
    for raised in (schedule_card, other_card, card):
        assert (raised.incident_id, raised.operator_id) == (None, "safaricom")


# ------------------------------------------------------- who owns a card (schema_version 8)
#
# These replace the anchor behaviour. Until v8 a card had to be filed against an incident —
# the window's own, else *the operator's most recent incident of any status*, else no card at
# all (NoAnchorIncident -> 503). Each test below is one rung of that ladder, as it stands now.


def test_a_card_needs_no_incident_and_belongs_to_its_subjects_operator(tmp_db, on):
    """Rung 3 of the old ladder — "no incident anywhere, so no card" — is gone."""
    _settings, session = tmp_db
    assert session.scalar(select(func.count()).select_from(IncidentRow)) == 0
    plan = _plan(session)
    window = _window(session)
    task, _ = svc.propose_task(session, plan, SITE, _cfg(), due_at=WINDOW_START)
    task.window_id = window.id
    session.flush()

    schedule_card = svc.request_schedule_approval(session, task, plan, _cfg())
    window_card = svc.request_window_approval(session, window, _cfg())
    session.flush()

    assert (schedule_card.incident_id, schedule_card.operator_id) == (None, plan.operator_id)
    assert (window_card.incident_id, window_card.operator_id) == (None, window.operator_id)
    assert plan.operator_id == window.operator_id == "safaricom"
    for raised in (schedule_card, window_card):
        assert "anchor_incident_number" not in raised.proposed_payload
        # Reachable under the operator's scope, which is what makes it approvable at all.
        assert session.scalar(_owned(HitlTaskRow).where(HitlTaskRow.id == raised.id)) is raised


def test_an_unrelated_incident_is_left_alone_by_a_maintenance_card(tmp_db, on):
    """Rung 2 — "else the operator's most recent incident, of any status" — was the workaround,
    and it did damage: Tuesday's generator service was filed against somebody's fibre cut, and
    deciding the card on the ordinary HITL route re-derived THAT incident's ``hitl_state`` from
    a card that was never about it. The most recent incident is now nobody's anchor."""
    _settings, session = tmp_db
    bystander = _incident(session, number="INC000911", status="IN_PROGRESS")
    window = _window(session)

    card = svc.request_window_approval(session, window, _cfg())
    session.flush()

    assert card.incident_id is None
    assert session.scalars(select(HitlTaskRow).where(HitlTaskRow.incident_id == bystander.id)).all() == []
    sync_incident_hitl_scalars(session, bystander)  # what main.py runs for a task's incident on claim/approve
    assert (bystander.requires_hitl, bystander.hitl_state) == (False, "NONE")


def test_a_window_raised_because_of_an_incident_carries_that_incident_and_only_that_one(tmp_db, on):
    """Rung 1 was never a workaround and survives: ``maintenance_windows.incident_id`` says the
    window exists BECAUSE of an incident, so the card is truthfully about it. The id arrives in
    a request body, though, so it is believed only when it names an incident of the same
    operator; anything else means "not about an incident", never "about that one anyway"."""
    _settings, session = tmp_db
    cause = _incident(session, number="INC000921")
    _incident(session, number="INC000922", created_at=utcnow() + timedelta(hours=1))  # more recent; irrelevant
    theirs = _incident(session, number="ATL-000001", operator_id="airtel")

    because = _window(session, incident_id=cause.id)
    card = svc.request_window_approval(session, because, _cfg())
    assert (card.incident_id, card.operator_id) == (cause.id, "safaricom")

    for n, claimed in enumerate(("no-such-incident", theirs.id), start=1):
        window = _window(session, incident_id=claimed, starts_at=DRY_START + timedelta(days=n), ends_at=DRY_END + timedelta(days=n))
        card = svc.request_window_approval(session, window, _cfg())  # no HitlTaskOwnershipError either
        assert (card.incident_id, card.operator_id) == (None, "safaricom"), claimed
    # The other operator's incident gained nothing from being named.
    assert session.scalars(select(HitlTaskRow).where(HitlTaskRow.incident_id == theirs.id)).all() == []


def test_no_anchor_incident_is_retired_but_the_name_still_resolves(on):
    """``NoAnchorIncident`` can no longer happen, so nothing may raise it — and it must still
    EXIST, because ``api/routers/maintenance.py`` names ``svc.NoAnchorIncident`` in two
    ``except`` clauses. Python evaluates those only when some other exception reaches them, so
    deleting the class would surface as an AttributeError in production, not at import."""
    import ast
    import inspect

    assert issubclass(svc.NoAnchorIncident, RuntimeError)
    assert "NoAnchorIncident" in svc.__all__
    assert not hasattr(svc, "anchor_incident") and "anchor_incident" not in svc.__all__

    tree = ast.parse(inspect.getsource(svc))
    raised = [
        ast.unparse(node.exc) for node in ast.walk(tree) if isinstance(node, ast.Raise) and node.exc is not None
    ]
    assert not [r for r in raised if "NoAnchorIncident" in r], "the retired exception is being raised again"
    caught = [
        ast.unparse(h.type) for node in ast.walk(tree) if isinstance(node, ast.Try) for h in node.handlers if h.type
    ]
    assert not [c for c in caught if "NoAnchorIncident" in c], "nothing in the service should still expect it"

    # The router really does still name it: the day it stops, delete the class and this test.
    from noc_agents.api.routers import maintenance as routes

    assert inspect.getsource(routes).count("svc.NoAnchorIncident") == 2


def test_the_two_card_types_are_the_enum_members_the_spec_names(tmp_db, on):
    _settings, session = tmp_db
    assert svc.SCHEDULE_TASK_TYPE == HitlTaskType.APPROVE_SCHEDULE.value == "APPROVE_SCHEDULE"
    assert svc.WINDOW_TASK_TYPE == HitlTaskType.APPROVE_MAINTENANCE_WINDOW.value == "APPROVE_MAINTENANCE_WINDOW"
    assert svc.SCHEDULE_TASK_TYPE != svc.WINDOW_TASK_TYPE


def test_a_region_or_network_window_needs_a_ca_approval_reference(tmp_db, on):
    """CA licence Condition 9.1 requires prior WRITTEN Authority approval (§7.5.6). D8 exempts
    SITE scope, so the same approved card schedules a SITE window and refuses a REGION one."""
    _settings, session = tmp_db
    window = svc.create_window(
        session, _cfg(), {"scope": "REGION", "scope_ref": "MTK", "starts_at": DRY_START, "ends_at": DRY_END}, actor=RAISER
    )
    _approve(session, svc.request_window_approval(session, window, _cfg()))
    with pytest.raises(svc.CaApprovalRequired, match="Condition 9.1"):
        svc.schedule_window(session, window, _cfg(), actor=APPROVER)
    assert window.status == "PROPOSED"

    window.ca_approval_ref = "CA/NFP/2026/0417"
    session.flush()
    svc.schedule_window(session, window, _cfg(), actor=APPROVER)
    assert window.status == "SCHEDULED"


def test_a_site_window_needs_no_ca_reference(tmp_db, on):
    _settings, session = tmp_db
    window = _window(session, starts_at=DRY_START, ends_at=DRY_END)
    _approve(session, svc.request_window_approval(session, window, _cfg()))
    svc.schedule_window(session, window, _cfg(), actor=APPROVER)
    assert window.status == "SCHEDULED" and window.ca_approval_ref is None


def test_an_invite_may_not_go_out_for_a_window_nobody_approved(tmp_db, on):
    """An iMIP invite tells named engineers to be at a site at 01:00. Sending it for an
    unapproved window is how the gate gets routed around socially rather than technically."""
    _settings, session = tmp_db
    plan = _plan(session)
    window = _window(session)
    task, _ = svc.propose_task(session, plan, SITE, _cfg(), due_at=WINDOW_START)
    task.window_id = window.id
    session.flush()
    _approve(session, svc.request_schedule_approval(session, task, plan, _cfg()))
    svc.mark_scheduled(session, task, actor=APPROVER)

    with pytest.raises(svc.WindowNotApproved, match="approved for the night"):
        svc.mark_invited(session, task, outbox_id="ob-1", actor=APPROVER)
    assert task.status == "SCHEDULED"

    _approve(session, svc.request_window_approval(session, window, _cfg()))
    svc.schedule_window(session, window, _cfg(), actor=APPROVER)
    svc.mark_invited(session, task, outbox_id="ob-1", actor=APPROVER)
    assert task.status == "INVITED"


def test_moving_a_window_revokes_the_approval_to_go_ahead(tmp_db, on):
    """"Approve going ahead on Tuesday night" is not approval to go ahead on Thursday."""
    _settings, session = tmp_db
    window = _window(session, starts_at=DRY_START, ends_at=DRY_END)
    _approve(session, svc.request_window_approval(session, window, _cfg()))
    svc.schedule_window(session, window, _cfg(), actor=APPROVER)
    assert window.status == "SCHEDULED" and window.sequence == 0

    svc.reschedule_window(
        session,
        window,
        starts_at=DRY_START + timedelta(days=2),
        ends_at=DRY_END + timedelta(days=2),
        actor=APPROVER,
        reason="crew unavailable",
    )
    assert window.status == "PROPOSED"
    assert window.hitl_task_id is None and window.approved_by is None
    assert window.sequence == 1, "a client may ignore a REQUEST whose SEQUENCE has not advanced"
    with pytest.raises(svc.WindowNotApproved):
        svc.schedule_window(session, window, _cfg(), actor=APPROVER)


def test_a_raised_card_queues_nothing(tmp_db, on):
    """Deliberate departure from the handover pattern, for the reason ``services/regulatory.py``
    gives: a HELD outbox row is one generic ``release_held`` away from PENDING, and that
    releases by *incident* — so for a window that hangs off a real incident, that incident's own
    broadcast approval would promote a calendar invite for work nobody approved. The right
    number of rows before approval is zero."""
    from noc_agents.orchestrator.outbox import OutboxRow

    _settings, session = tmp_db
    plan = _plan(session)
    window = _window(session)
    task, _ = svc.propose_task(session, plan, SITE, _cfg(), due_at=WINDOW_START)
    svc.request_schedule_approval(session, task, plan, _cfg())
    svc.request_window_approval(session, window, _cfg())
    session.flush()
    assert session.query(OutboxRow).count() == 0


# --------------------------------------------------------------------------------- rain guard


def test_the_rain_guard_never_says_clear_when_it_has_no_forecast(tmp_db, on):
    """``WEATHER_ENABLED`` is off by default, so "no forecast" is the normal case — and it is
    not "no rain". Three-valued on purpose (§7.5.3 + the module docstring)."""
    _settings, session = tmp_db

    in_season = _window(session, starts_at=WINDOW_START, ends_at=WINDOW_END)  # November: OND
    verdict = svc.rain_guard(session, in_season, _cfg())
    assert verdict.verdict == svc.RAIN_NO_FORECAST_SEASON
    assert verdict.verdict != svc.RAIN_CLEAR
    assert verdict.flag == 1, "the approver must see the warning §7.5.3 asks for"
    assert verdict.blocking is False, "a guard with no data may raise its hand; it may not make policy"
    assert "no usable forecast" in verdict.reason
    assert verdict.evidence["in_rain_season"] is True

    off_season = _window(session, starts_at=DRY_START, ends_at=DRY_END)  # July
    dry = svc.rain_guard(session, off_season, _cfg())
    assert dry.verdict == svc.RAIN_NO_FORECAST
    assert dry.verdict != svc.RAIN_CLEAR, "never CLEAR: the guard did not run, it did not pass"
    assert dry.flag == 0 and dry.blocking is False
    assert "the guard did not run" in dry.reason


def test_a_fresh_forecast_with_no_storm_is_the_only_clear_verdict(tmp_db, on):
    _settings, session = tmp_db
    now = DRY_START - timedelta(minutes=5)
    _forecast(session, "MTK", storm=False, now=now)
    window = _window(session, starts_at=DRY_START, ends_at=DRY_END)
    verdict = svc.rain_guard(session, window, _cfg(), now=now)
    assert verdict.verdict == svc.RAIN_CLEAR and verdict.flag == 0 and verdict.blocking is False
    assert verdict.evidence["regions"]["MTK"]["forecast"] == "FRESH"


def test_a_stale_forecast_is_treated_as_no_forecast(tmp_db, on):
    """A four-hour-old "no storm" is not evidence about tonight. ``weather_risk_for_region``
    recomputes staleness against now, which is exactly why this guard composes it."""
    _settings, session = tmp_db
    now = WINDOW_START - timedelta(minutes=5)
    _forecast(session, "MTK", storm=False, now=now, age_h=4)
    window = _window(session, starts_at=WINDOW_START, ends_at=WINDOW_END)
    verdict = svc.rain_guard(session, window, _cfg(), now=now)
    assert verdict.verdict == svc.RAIN_NO_FORECAST_SEASON
    assert verdict.evidence["regions"]["MTK"]["forecast"] == "STALE"


def test_a_fresh_storm_forecast_refuses_until_a_named_human_overrides(tmp_db, on):
    """Evidence exists and it says no, so the default answer is no — and the override is
    audited, not a flag some code can set."""
    _settings, session = tmp_db
    now = DRY_START - timedelta(minutes=5)
    _forecast(session, "MTK", storm=True, now=now)
    window = _window(session, starts_at=DRY_START, ends_at=DRY_END)
    verdict = svc.rain_guard(session, window, _cfg(), now=now)
    assert verdict.verdict == svc.RAIN_STORM and verdict.flag == 1 and verdict.blocking is True

    _approve(session, svc.request_window_approval(session, window, _cfg(), now=now))
    with pytest.raises(svc.RainGuardBlocked, match="storm forecast"):
        svc.schedule_window(session, window, _cfg(), actor=APPROVER, now=now)
    assert window.status == "PROPOSED"
    # The flag was written when the card was raised, so the approver saw the warning; the
    # refused transition itself wrote nothing.
    assert window.rain_season_flag == 1

    # An override with no reason is still refused: the reason IS the record.
    with pytest.raises(ValueError, match="requires a reason"):
        svc.schedule_window(session, window, _cfg(), actor=APPROVER, now=now, override_rain=True)
    assert window.status == "PROPOSED"

    svc.schedule_window(
        session,
        window,
        _cfg(),
        actor=APPROVER,
        now=now,
        override_rain=True,
        override_reason="rectifier failed; site is on batteries with 4 h left",
    )
    assert window.status == "SCHEDULED"


def test_the_rain_season_is_read_in_eat_not_utc(tmp_db, on):
    """21:30 UTC on 28 February is already 1 March in Nairobi — the first night of the long
    rains. Reading the month off the stored UTC value would put it in February."""
    _settings, session = tmp_db
    window = _window(session, starts_at=datetime(2026, 2, 28, 21, 30), ends_at=datetime(2026, 3, 1, 2, 0))
    verdict = svc.rain_guard(session, window, _cfg())
    assert verdict.evidence["in_rain_season"] is True
    assert verdict.evidence["window_start_eat"].startswith("2026-03-01")


def test_a_network_window_is_flagged_by_a_storm_anywhere(tmp_db, on):
    _settings, session = tmp_db
    now = DRY_START - timedelta(minutes=5)
    for region in _cfg().regions:
        _forecast(session, region, storm=(region == "CST"), now=now)
    window = svc.create_window(
        session,
        _cfg(),
        {"scope": "NETWORK", "scope_ref": "safaricom", "starts_at": DRY_START, "ends_at": DRY_END},
        actor=RAISER,
    )
    verdict = svc.rain_guard(session, window, _cfg(), now=now)
    assert verdict.verdict == svc.RAIN_STORM and "CST" in verdict.reason


# ------------------------------------------------------------ the stop clock stays a proposal


def test_a_planned_window_proposal_never_opens_a_stop_clock_event(tmp_db, on):
    """Stop-clock minutes are deducted from a vendor's SLA figure. This system proposes; a
    named person on an operations role opens the event, and owns it (§7.6)."""
    _settings, session = tmp_db
    inc = _incident(session, failure_time=WINDOW_START + timedelta(minutes=30))
    window = _window(session)
    _approve(session, svc.request_window_approval(session, window, _cfg()))
    svc.schedule_window(session, window, _cfg(), actor=APPROVER)

    assert list_clock_events(session, inc.id) == []
    proposal = svc.stop_clock_proposal(session, inc, now=WINDOW_START + timedelta(hours=1))
    assert proposal is not None
    assert proposal["scc_code"] == "PLANNED_MAINTENANCE"
    assert proposal["status"] == "PROPOSED"
    assert proposal["requires_human"] is True
    assert proposal["opened"] is False
    assert proposal["window_id"] == window.id
    assert proposal["accept_with"]["path"] == f"/api/v1/incidents/{inc.id}/clock"

    # Nothing was written. Reading the proposal twice writes nothing twice.
    svc.stop_clock_proposal(session, inc, now=WINDOW_START + timedelta(hours=2))
    session.flush()
    assert list_clock_events(session, inc.id) == []
    assert session.query(ClockEventRow).count() == 0


def test_the_proposal_is_clipped_to_the_overlap_of_window_and_outage(tmp_db, on):
    """Proposing the whole window would deduct minutes the site was already back up."""
    _settings, session = tmp_db
    failure = WINDOW_START + timedelta(hours=1)
    restored = WINDOW_START + timedelta(hours=2)
    inc = _incident(session, failure_time=failure, restored_at=restored, status="RESTORED")
    window = _window(session)
    _approve(session, svc.request_window_approval(session, window, _cfg()))
    svc.schedule_window(session, window, _cfg(), actor=APPROVER)

    proposal = svc.stop_clock_proposal(session, inc, now=restored + timedelta(hours=6))
    assert proposal["proposed_minutes"] == 60
    assert proposal["proposed_started_at"].replace(tzinfo=None) == failure
    assert proposal["proposed_ended_at"].replace(tzinfo=None) == restored


def test_a_proposed_window_proposes_no_stop_clock(tmp_db, on):
    """A PROPOSED window is a plan, not a planned outage. Deducting its minutes would credit a
    vendor for a night nobody approved."""
    _settings, session = tmp_db
    inc = _incident(session)
    _window(session)  # left PROPOSED
    assert svc.stop_clock_proposal(session, inc, now=WINDOW_START + timedelta(hours=1)) is None
    assert svc.is_planned(session, "safaricom", SITE, WINDOW_START + timedelta(hours=1)) is None


def test_the_enrich_tag_is_evaluated_at_the_failure_time(tmp_db, on):
    """An alarm that arrived during an approved window is planned work even if somebody opens
    the ticket the next afternoon (§7.5.3)."""
    _settings, session = tmp_db
    inc = _incident(session, failure_time=WINDOW_START + timedelta(minutes=10))
    window = _window(session)
    _approve(session, svc.request_window_approval(session, window, _cfg()))
    svc.schedule_window(session, window, _cfg(), actor=APPROVER)
    tag = svc.planned_maintenance_tag(session, inc, now=WINDOW_END + timedelta(days=1))
    assert tag["planned_maintenance"] == 1 and tag["window_uid"] == window.uid


# ------------------------------------------------------------------------------ overlap rules


def test_windows_may_overlap_while_proposed_but_not_once_scheduled(tmp_db, on):
    """Planning is iterative; two crews independently taking one site off air is not."""
    _settings, session = tmp_db
    first = _window(session, starts_at=DRY_START, ends_at=DRY_END)
    second = _window(session, starts_at=DRY_START + timedelta(hours=1), ends_at=DRY_END + timedelta(hours=1))
    assert first.status == second.status == "PROPOSED", "overlapping PROPOSED windows are allowed"

    _approve(session, svc.request_window_approval(session, first, _cfg()))
    svc.schedule_window(session, first, _cfg(), actor=APPROVER)
    _approve(session, svc.request_window_approval(session, second, _cfg()))
    with pytest.raises(svc.WindowOverlapError, match="already SCHEDULED"):
        svc.schedule_window(session, second, _cfg(), actor=APPROVER)
    assert second.status == "PROPOSED"


def test_back_to_back_windows_are_not_an_overlap(tmp_db, on):
    """Half-open intervals: a window ending at 05:00 and one starting at 05:00 are how a night
    is actually split between two crews."""
    _settings, session = tmp_db
    first = _window(session, starts_at=DRY_START, ends_at=DRY_END)
    second = _window(session, starts_at=DRY_END, ends_at=DRY_END + timedelta(hours=2))
    _approve(session, svc.request_window_approval(session, first, _cfg()))
    svc.schedule_window(session, first, _cfg(), actor=APPROVER)
    _approve(session, svc.request_window_approval(session, second, _cfg()))
    svc.schedule_window(session, second, _cfg(), actor=APPROVER)
    assert first.status == second.status == "SCHEDULED"
    assert svc.intervals_overlap(DRY_START, DRY_END, DRY_END, DRY_END + timedelta(hours=2)) is False


def test_scope_containment_is_explicit(tmp_db, on):
    """NETWORK ⊃ everything; REGION ⊃ the sites in it; SITE = itself."""
    assert svc.scopes_conflict("NETWORK", "safaricom", "SITE", SITE) is True
    assert svc.scopes_conflict("SITE", SITE, "NETWORK", "safaricom") is True
    assert svc.scopes_conflict("REGION", "MTK", "SITE", SITE) is True
    assert svc.scopes_conflict("SITE", SITE, "REGION", "MTK") is True
    assert svc.scopes_conflict("REGION", "NBI_E", "SITE", SITE) is False
    assert svc.scopes_conflict("SITE", SITE, "SITE", SITE_NBI) is False
    assert svc.scopes_conflict("REGION", "MTK", "REGION", "MTK") is True
    # An unknown site id cannot be placed in a region: it conflicts with itself and NETWORK
    # only, rather than manufacturing a regional clash out of a lookup failure.
    assert svc.scopes_conflict("REGION", "MTK", "SITE", "NO-SUCH-SITE") is False
    assert svc.scopes_conflict("NETWORK", "safaricom", "SITE", "NO-SUCH-SITE") is True


def test_a_region_window_blocks_a_site_window_inside_it(tmp_db, on):
    _settings, session = tmp_db
    region = svc.create_window(
        session,
        _cfg(),
        {
            "scope": "REGION",
            "scope_ref": "MTK",
            "starts_at": DRY_START,
            "ends_at": DRY_END,
            "ca_approval_ref": "CA/NFP/2026/0417",
        },
        actor=RAISER,
    )
    _approve(session, svc.request_window_approval(session, region, _cfg()))
    svc.schedule_window(session, region, _cfg(), actor=APPROVER)

    site_window = _window(session, starts_at=DRY_START + timedelta(hours=1), ends_at=DRY_END)
    _approve(session, svc.request_window_approval(session, site_window, _cfg()))
    with pytest.raises(svc.WindowOverlapError):
        svc.schedule_window(session, site_window, _cfg(), actor=APPROVER)


def test_planned_minutes_counts_an_overlapped_hour_once(tmp_db, on):
    """Reuses ``clock_events.effective_intervals`` — the union, never the sum. Two windows
    covering the same hour exclude that hour once from availability, exactly as two stop clocks
    deduct it once."""
    _settings, session = tmp_db
    a = _window(session, starts_at=DRY_START, ends_at=DRY_START + timedelta(hours=3))
    b = _window(session, starts_at=DRY_START + timedelta(hours=2), ends_at=DRY_START + timedelta(hours=4))
    for window in (a, b):
        _approve(session, svc.request_window_approval(session, window, _cfg()))
    svc.schedule_window(session, a, _cfg(), actor=APPROVER)
    # b overlaps a, so the gate refuses it — which is the *other* half of this lane's answer to
    # overlap and is tested above. The status is written directly here because this test is
    # about the arithmetic being right if two overlapping windows ever are both SCHEDULED: a
    # hand-edited row, a seeded database, or the RRULE expansion a later wave will add. Belt
    # and braces: the rule prevents it, and the sum does not double-count when it happens.
    b.status = "SCHEDULED"
    session.flush()
    minutes = svc.planned_minutes(
        session, "safaricom", SITE, period_start=DRY_START, period_end=DRY_START + timedelta(hours=6)
    )
    assert minutes == 4 * 60, "3h + 2h with 1h shared is 4h of union, not 5h of sum"


# ------------------------------------------------------------------------ lifecycle and jobs


def test_cancelling_a_window_bumps_the_sequence_and_cancels_its_tasks(tmp_db, on):
    """A METHOD:CANCEL whose SEQUENCE has not advanced may be ignored by the recipient's
    calendar, leaving a live entry for work that is not happening."""
    _settings, session = tmp_db
    plan = _plan(session)
    window = _window(session)
    task, _ = svc.propose_task(session, plan, SITE, _cfg(), due_at=WINDOW_START)
    task.window_id = window.id
    session.flush()
    svc.cancel_window(session, window, actor=APPROVER, reason="customer notice not served in time")
    assert window.status == "CANCELLED" and window.sequence == 1
    assert task.status == "CANCELLED"
    assert svc.window_ics_fields(session, window, _cfg())["method"] == "CANCEL"
    with pytest.raises(svc.MaintenanceStateError):
        svc.cancel_window(session, window, actor=APPROVER, reason="again")


def test_a_completed_window_marks_unfinished_work_missed(tmp_db, on):
    """MISSED is a real status and not an absence: "the window passed and the battery check did
    not happen" must be distinguishable from "nobody looked"."""
    _settings, session = tmp_db
    plan = _plan(session)
    window = _window(session, starts_at=DRY_START, ends_at=DRY_END)
    task, _ = svc.propose_task(session, plan, SITE, _cfg(), due_at=DRY_START)
    task.window_id = window.id
    _approve(session, svc.request_window_approval(session, window, _cfg()))
    svc.schedule_window(session, window, _cfg(), actor=APPROVER)
    svc.complete_window(session, window, actor=APPROVER)
    assert window.status == "COMPLETED" and task.status == "MISSED"


def test_a_completion_needs_an_outcome_and_cannot_be_redone(tmp_db, on):
    """A tick in a box is what makes a maintenance regime look healthy while the generator has
    not started in a year — and the completion IS the next due date."""
    _settings, session = tmp_db
    plan = _plan(session)
    task, _ = svc.propose_task(session, plan, SITE, _cfg(), due_at=WINDOW_START)
    with pytest.raises(ValueError, match="outcome is required"):
        svc.complete_task(session, task, completed_by="Alice Mwangi", outcome="  ")
    # The work was done last night; a completion stamped in the future is refused, because a
    # completion that has not happened yet would move every subsequent due date at the site.
    done_at = utcnow() - timedelta(hours=12)
    with pytest.raises(ValueError, match="cannot be in the future"):
        svc.complete_task(session, task, completed_by="Alice Mwangi", outcome="x", completed_at=utcnow() + timedelta(days=2))
    svc.complete_task(session, task, completed_by="Alice Mwangi", outcome="ran 45 min at 40% load", completed_at=done_at)
    assert task.status == "DONE"
    assert svc.last_completion(session, plan.id, SITE) == done_at
    with pytest.raises(svc.MaintenanceStateError, match="already DONE"):
        svc.complete_task(session, task, completed_by="Alice Mwangi", outcome="again")
    # And the next due date now follows the measured completion, not the assumption.
    due, basis = svc.next_due(plan, last_completed_at=svc.last_completion(session, plan.id, SITE))
    assert basis == svc.BASIS_LAST_COMPLETION and due == done_at + timedelta(days=30)


def test_one_live_task_per_plan_and_site(tmp_db, on):
    """The job runs hourly; a monthly generator exercise must not accumulate one task a tick."""
    _settings, session = tmp_db
    plan = _plan(session)
    first, created_a = svc.propose_task(session, plan, SITE, _cfg(), due_at=WINDOW_START)
    second, created_b = svc.propose_task(session, plan, SITE, _cfg(), due_at=WINDOW_START + timedelta(days=1))
    assert created_a is True and created_b is False and first.id == second.id


def test_the_plan_due_job_proposes_only_work_inside_the_notice_horizon(tmp_db, on):
    _settings, session = tmp_db
    soon = _plan(session, interval_days=30)
    soon.created_at = utcnow() - timedelta(days=29)  # due tomorrow
    far = _plan(session, task_type="TOWER_STRUCTURAL", interval_days=1095)
    session.flush()

    result = svc.plan_due(session, _settings)
    assert "proposed=1" in result.summary
    assert "cards=1" in result.summary, "until schema v8 this was cards=0 on a database with no incidents"
    assert "not_due_yet=1" in result.summary
    tasks = session.scalars(svc.owned_tasks(session, "safaricom")).all()
    assert [t.plan_id for t in tasks] == [soon.id]
    assert far.id not in {t.plan_id for t in tasks}
    # And the card it raised is an APPROVE_SCHEDULE pointing at the task, not at the incident.
    card = session.get(HitlTaskRow, tasks[0].hitl_task_id)
    assert card.task_type == "APPROVE_SCHEDULE"
    assert card.entity_type == "maintenance_task" and card.entity_id == tasks[0].id
    assert (card.incident_id, card.operator_id) == (None, "safaricom")
    assert card.created_by == svc.MAINTENANCE_RAISER, "an agent raiser can never equal the approver (§6.5)"
    assert "NOT approval to take the site off air" in card.proposed_payload["warning"]


def test_the_sweep_leaves_a_lapsed_unapproved_window_visible(tmp_db, on):
    """A window nobody approved in time is a planning failure. Auto-cancelling it would erase
    the evidence of it on the next tick."""
    _settings, session = tmp_db
    window = _window(session, starts_at=utcnow() - timedelta(hours=2), ends_at=utcnow() - timedelta(hours=1))
    result = svc.window_sweep(session, _settings)
    assert "lapsed_unapproved=1" in result.summary
    assert window.status == "PROPOSED"


def test_the_sweep_completes_a_window_whose_night_is_over(tmp_db, on):
    _settings, session = tmp_db
    window = _window(session, starts_at=utcnow() - timedelta(hours=6), ends_at=utcnow() - timedelta(hours=1))
    _approve(session, svc.request_window_approval(session, window, _cfg()))
    # The rain guard has no forecast and the window is not in season here only by accident of
    # the current date, so schedule it directly through the gate and let the sweep close it.
    svc.schedule_window(session, window, _cfg(), actor=APPROVER)
    result = svc.window_sweep(session, _settings)
    assert "completed=1" in result.summary and window.status == "COMPLETED"


# ---------------------------------------------------------------------------- operator scoping


def test_maintenance_tasks_are_scoped_through_their_plan(tmp_db, on):
    """``maintenance_tasks`` has no ``operator_id``; the clause lives in the plan join, in the
    WHERE, never in a check after the read (§8)."""
    _settings, session = tmp_db
    mine = _plan(session)
    svc.propose_task(session, mine, SITE, _cfg(), due_at=WINDOW_START)
    theirs = MaintenancePlanRow(
        id=new_id(),
        operator_id="airtel",
        site_id=SITE,
        task_type="TOWER_VISUAL",
        interval_days=365,
        standard_ref="TIA-222 practice",
        created_at=utcnow(),
    )
    session.add(theirs)
    session.flush()
    session.add(MaintenanceTaskRow(id=new_id(), plan_id=theirs.id, site_id=SITE, due_at=WINDOW_START, created_at=utcnow()))
    session.flush()

    assert session.query(MaintenanceTaskRow).count() == 2
    ours = session.scalars(svc.owned_tasks(session, "safaricom")).all()
    assert [t.plan_id for t in ours] == [mine.id]


# ------------------------------------------------------------------------------ the ICS seam


def test_the_ics_seam_carries_the_calendar_identity_and_no_addresses(tmp_db, on):
    """Attendee e-mail is personal data (§7.5.6): the seam carries a config path, resolved at
    dispatch like every other audience."""
    _settings, session = tmp_db
    window = _window(session)
    fields = svc.window_ics_fields(session, window, _cfg())
    assert fields["uid"] == window.uid and fields["uid"].startswith("maint-")
    assert fields["sequence"] == 0 and fields["method"] == "REQUEST"
    assert fields["tzid"] == "Africa/Nairobi"
    assert fields["attendees_ref"] == svc.DEFAULT_ATTENDEES_REF == "maintenance.recipients.FE_ONCALL"
    assert "@" not in fields["attendees_ref"]
    assert fields["dtstart"].replace(tzinfo=None) == WINDOW_START
    assert fields["dtstart_eat"].startswith("2026-11-12 00:00"), "EAT is UTC+3, on the way out only"


# --------------------------------------------------------------------------------- the API


def test_the_api_walks_a_window_from_proposal_to_scheduled(client):
    """End to end over HTTP, including that the window gate is a 403 until the right card is
    approved on the ordinary HITL surface."""
    # Until schema v8 this test had to ingest an alarm first, purely so that there was an
    # incident to file the card against; without one the request-approval route answered 503.
    # A quiet network is exactly when maintenance gets planned, so the walk now starts empty.
    assert client.get("/api/v1/incidents").json() == []

    r = client.post(
        "/api/v1/maintenance/plans",
        json={"task_type": "BATTERY_CHECK", "site_id": SITE, "interval_days": 7, "standard_ref": "IEEE 1188 practice"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["standard_note"].startswith("weekly voltage")

    r = client.post(
        "/api/v1/maintenance/windows",
        json={"scope": "SITE", "scope_ref": SITE, "starts_at": "2026-11-11T21:00:00Z", "ends_at": "2026-11-12T02:00:00Z"},
    )
    assert r.status_code == 200, r.text
    window = r.json()
    assert window["status"] == "PROPOSED"
    assert window["rain_guard"]["verdict"] == "NO_FORECAST_RAIN_SEASON"
    assert window["rain_season_flag"] == 1

    # Unapproved: 403, and the window is untouched.
    r = client.post(f"/api/v1/maintenance/windows/{window['id']}/schedule", json={})
    assert r.status_code == 403, r.text
    assert "APPROVE_MAINTENANCE_WINDOW" in r.text

    r = client.post(f"/api/v1/maintenance/windows/{window['id']}/request-approval", json={})
    assert r.status_code == 200, r.text
    card_id = r.json()["hitl_task_id"]

    # The card is visible on the ordinary HITL inbox with no incident behind it: it is owned
    # through hitl_tasks.operator_id, not through an incident it was never about.
    pending = client.get("/api/v1/hitl/pending").json()
    assert card_id in [t["id"] for t in pending]
    card = next(t for t in pending if t["id"] == card_id)
    assert card["task_type"] == "APPROVE_MAINTENANCE_WINDOW"
    assert card["incident_id"] is None and card["incident_number"] is None
    assert "anchor_incident_number" not in card["proposed_payload"]
    assert card["proposed_payload"]["rain_season_flag"] == 1
    assert "off air" in card["proposed_payload"]["warning"]

    assert client.post(f"/api/v1/hitl/{card_id}/approve", json={"resolved_by": APPROVER}).status_code == 200
    r = client.post(f"/api/v1/maintenance/windows/{window['id']}/schedule", json={})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "SCHEDULED" and r.json()["approved_by"] == APPROVER

    # Cancelling advances the sequence for the METHOD:CANCEL.
    r = client.post(f"/api/v1/maintenance/windows/{window['id']}/cancel", json={"reason": "postponed"})
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED" and r.json()["sequence"] == 1
    assert client.get("/api/v1/incidents").json() == [], "and no incident was conjured along the way"


def test_the_stop_clock_proposal_route_is_read_only(client):
    """There is no POST beside it: accepting a proposal is the existing, role-gated
    ``POST /api/v1/incidents/{id}/clock``."""
    r = client.post(
        "/api/v1/events",
        json={"source": "NMS", "site_id": SITE, "alarm_code": "POWER_FAIL", "message": "x", "severity": "MAJOR", "users_affected": 10},
    )
    incident_id = r.json()["incident"]["id"]
    r = client.get(f"/api/v1/maintenance/incidents/{incident_id}/stop-clock-proposal")
    assert r.status_code == 200, r.text
    assert r.json() == {"proposal": None}
    assert client.post(f"/api/v1/maintenance/incidents/{incident_id}/stop-clock-proposal", json={}).status_code == 405
