"""Capacity observations and advisories — spec §7.5.1/§7.5.3, Phase 5 Lane 5A.

Four things this file exists to protect, in order of how much damage their absence does.

**An advisory is advice and cannot act.** §7.5.3: "advisory routed to Planning; never an
upgrade order". A capacity advisory that could book a maintenance window would be a counter
export taking live customers off air with no human anywhere in the chain.
``test_a_capacity_advisory_never_schedules_maintenance_touches_an_incident_or_opens_a_stop_clock``
asserts the four tables that would have to gain a row are still empty afterwards — after the
service call *and* after a full scan job, because the job is the thing that would one day be
"made more useful".

**A confident number from noise is worse than silence.** §7.4.5 prescribes an "advisory only,
minimum count threshold" for exactly this reason: Kenyan per-cell volumes are thin.
``test_a_cell_with_two_observations_is_not_a_trend_and_the_reading_says_so`` pins the floor,
and pins that the reading *withholds* ``peak_pct`` rather than publishing the 95 % that is
sitting right there in the rows. ``test_an_advisory_is_never_opened_from_data_the_lane_has_
already_said_it_does_not_trust`` pins the consequence.

**A site that was deliberately off air did not have a capacity problem.** The exclusion is
asked of ``services.maintenance`` and only SCHEDULED windows count, because a PROPOSED window
is a plan and not a planned outage. The pair of window tests pins both halves, and
``test_a_reading_says_out_loud_when_planned_maintenance_could_not_be_excluded`` pins the case
that is actually the DEFAULT — ``MAINTENANCE_ENABLED`` off — where the honest answer is
"not excluded, and here is why" rather than an unlabelled number.

**Operator isolation is a property of the query.** Both operators are seeded at the same
``site_id`` and the same ``cell_id``, which is legal in this schema, and the totals must not
move.

No network anywhere: every row in this file is written straight to the database.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from noc_agents.api import auth
from noc_agents.config import get_settings
from noc_agents.db.models import HitlTaskRow, OutboxRow, new_id
from noc_agents.db.models_capacity import (
    ADVISORY_ACKNOWLEDGED,
    ADVISORY_CLOSED,
    ADVISORY_OPEN,
    DEFAULT_METRIC,
    CapacityAdvisoryRow,
    CapacityObservationRow,
)
from noc_agents.db.models_maintenance import (
    WINDOW_PROPOSED,
    WINDOW_SCHEDULED,
    MaintenanceTaskRow,
    MaintenanceWindowRow,
)
from noc_agents.db.models_vendors import ClockEventRow
from noc_agents.realtime.hub import hub
from noc_agents.services import capacity as svc

#: A fixed "now", so every reading in this file has a deterministic 14-day lookback window
#: (2026-09-06 06:00 -> 2026-09-20 06:00 UTC) and no test depends on the wall clock.
NOW = datetime(2026, 9, 20, 6, 0, 0)

SITE = "SFC-NBIE-ENB-KAY12"
CELL = "KAY12-L1800-1"
OTHER_OPERATOR = "airtel"  # a second controller in the same database file (§8)
REVIEWER = "Grace Wanjiru"  # a named human; never an agent: or policy: principal

#: The three UTC hours the fixtures use. 15:00/16:00/17:00 UTC is 18:00/19:00/20:00 EAT — the
#: Kenyan evening peak, and three DIFFERENT EAT hours on the same EAT day, which is what the
#: day-qualifying rule counts.
BUSY_HOURS = (15, 16, 17)

SAMPLE_CSV = Path(__file__).resolve().parents[2] / "data" / "seed" / "v2" / "capacity_sample.csv"


# ------------------------------------------------------------------------------- fixtures


@pytest.fixture()
def on(monkeypatch):
    """``CAPACITY_ENABLED=true``. Everything except the flag-off tests needs it."""
    monkeypatch.setenv("CAPACITY_ENABLED", "true")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A live API on its own SQLite file, with the lane armed."""
    db = tmp_path / "capacity.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
    monkeypatch.setenv("CAPACITY_ENABLED", "true")

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


def _session(client) -> "object":
    """A session on the same database the TestClient is using."""
    from noc_agents.db.models import get_session

    return get_session()


def _seed(
    session,
    *,
    days: int,
    site: str = SITE,
    cell: str | None = CELL,
    value: float = 80.0,
    hours: tuple[int, ...] = BUSY_HOURS,
    operator_id: str = "safaricom",
    metric: str = DEFAULT_METRIC,
    source: str = "CSV",
    first_day_offset: int = 1,
) -> list[CapacityObservationRow]:
    """``days`` consecutive days of ``hours`` busy-hour samples, ending the day before NOW."""
    rows: list[CapacityObservationRow] = []
    for day in range(first_day_offset, first_day_offset + days):
        base = (NOW - timedelta(days=day)).replace(hour=0, minute=0, second=0, microsecond=0)
        for hour in hours:
            row = CapacityObservationRow(
                id=new_id(),
                operator_id=operator_id,
                site_id=site,
                cell_id=cell,
                metric=metric,
                value=value,
                busy_hour_at=base + timedelta(hours=hour),
                source=source,
            )
            session.add(row)
            rows.append(row)
    session.flush()
    return rows


def _window(session, *, status: str, starts_at: datetime, ends_at: datetime, site: str = SITE) -> MaintenanceWindowRow:
    """One maintenance window straight into the table ``maintenance.planned_minutes`` reads."""
    row = MaintenanceWindowRow(
        id=new_id(),
        operator_id="safaricom",
        scope="SITE",
        scope_ref=site,
        starts_at=starts_at,
        ends_at=ends_at,
        uid=f"{new_id()}@noc.local",
        sequence=0,
        organizer="noc@example.test",
        attendees_ref="audiences.FE_ONCALL",
        status=status,
    )
    session.add(row)
    session.flush()
    return row


def _read(session, **kwargs):
    return svc.read_capacity(session, _cfg(), site_id=SITE, cell_id=CELL, now=NOW, **kwargs)


# ------------------------------------------------------------- the flag is off by default


def test_the_whole_lane_is_inert_with_the_flag_off(tmp_db, monkeypatch):
    """§8.9.4: with ``CAPACITY_ENABLED`` unset the system behaves exactly as it does today."""
    monkeypatch.delenv("CAPACITY_ENABLED", raising=False)
    settings, session = tmp_db

    assert svc.capacity_enabled() is False
    # The job reports itself off and writes nothing, rather than silently doing nothing.
    result = svc.capacity_scan(session, settings, now=NOW)
    assert "off" in result.summary
    assert session.query(CapacityAdvisoryRow).count() == 0


def test_the_routes_are_404_and_never_403_while_the_flag_is_off(client, monkeypatch):
    """A 403 would announce a feature that is supposed to be invisible (``routers/capacity.py``)."""
    monkeypatch.setenv("CAPACITY_ENABLED", "false")
    for path in ("/api/v1/capacity/advisories", f"/api/v1/capacity/sites/{SITE}", "/api/v1/capacity/observations"):
        response = client.get(path)
        assert response.status_code == 404, path
        assert "CAPACITY_ENABLED" in response.json()["detail"]


# ------------------------------------------------------------------- say when you do not know


def test_a_cell_with_two_observations_is_not_a_trend_and_the_reading_says_so(tmp_db, on):
    """The failure mode §7.4.5 warns about: a confident figure drawn from noise.

    Two samples, both at 95 %, which is as alarming as this metric gets. The reading must
    still be INSUFFICIENT_DATA, must name the shortfall, and must publish **no** utilisation
    number — the 95 % is sitting in the rows and the lane declines to quote it.
    """
    _settings, session = tmp_db
    _seed(session, days=1, value=95.0, hours=(16, 17))

    reading = _read(session)

    assert reading.verdict == svc.VERDICT_INSUFFICIENT_DATA
    assert reading.observations == 2
    assert reading.distinct_days == 1
    assert reading.peak_pct is None and reading.mean_busy_hour_pct is None
    body = reading.as_json()
    assert body["numbers_withheld"] is True
    assert body["peak_pct"] is None
    assert "Not enough data to advise" in body["reason"]
    # The floor is stated on the reading itself, so a reader can see WHAT would be enough.
    assert body["policy"]["min_observations"] == 21
    assert body["policy"]["min_days"] == 7


def test_the_data_floor_is_explicit_and_configurable_rather_than_a_number_in_the_code(tmp_db, on, monkeypatch):
    """An operator with a denser feed may lower the floor; the answer then changes honestly."""
    _settings, session = tmp_db
    _seed(session, days=2)

    assert _read(session).verdict == svc.VERDICT_INSUFFICIENT_DATA

    monkeypatch.setattr(svc, "DEFAULT_CAPACITY", {**svc.DEFAULT_CAPACITY, "min_days": 2, "min_observations": 6, "sustained_days": 2})
    reading = _read(session)
    assert reading.verdict == svc.VERDICT_SUSTAINED
    assert reading.peak_pct == 80.0


def test_an_advisory_is_never_opened_from_data_the_lane_has_already_said_it_does_not_trust(tmp_db, on):
    """INSUFFICIENT_DATA is not a near miss. Advice drawn from it would be worse than silence."""
    _settings, session = tmp_db
    _seed(session, days=2, value=99.0)

    reading = _read(session)
    assert reading.verdict == svc.VERDICT_INSUFFICIENT_DATA
    assert reading.advisable is False
    assert svc.open_advisory(session, reading) is None
    assert session.query(CapacityAdvisoryRow).count() == 0


# ------------------------------------------------------------------------ the trend rule


def test_a_sustained_cell_above_the_trigger_is_advised_and_the_advisory_carries_its_working(tmp_db, on):
    """§7.5.3: >= 70 % for >= 3 busy hours a day on >= 7 days -> an advisory routed to Planning."""
    _settings, session = tmp_db
    _seed(session, days=7, value=82.0)

    reading = _read(session)
    assert reading.verdict == svc.VERDICT_SUSTAINED
    assert reading.qualifying_days == 7
    assert reading.peak_pct == 82.0

    advisory = svc.open_advisory(session, reading)
    assert advisory is not None
    assert advisory.status == ADVISORY_OPEN
    assert advisory.routed_to == "PLANNING"
    # The policy as it was, copied onto the row: an advisory re-read after somebody edits the
    # trigger must still say what it was actually judged against.
    assert advisory.trigger_pct == 70.0
    assert advisory.sustained_days == 7

    body = svc.advisory_out(advisory)
    assert body["advice_only"] is True
    assert "not an upgrade order" in body["advice_note"]
    assert len(body["evidence"]["days"]) == 7
    assert body["evidence"]["days"][0]["hours_at_or_above_trigger"] == 3


def test_a_cell_below_the_trigger_on_plenty_of_data_is_answered_with_a_number_and_no_advisory(tmp_db, on):
    """The other sufficient answer. A quiet cell is quiet, and the lane says so with figures."""
    _settings, session = tmp_db
    _seed(session, days=8, value=55.0)

    reading = _read(session)
    assert reading.verdict == svc.VERDICT_BELOW_TRIGGER
    assert reading.qualifying_days == 0
    assert reading.peak_pct == 55.0  # published: this one IS known
    assert svc.open_advisory(session, reading) is None


def test_three_non_adjacent_busy_hours_qualify_a_day_and_the_adjacency_figure_is_still_reported(tmp_db, on):
    """The one place §7.5.1 and §7.5.3 disagree, resolved in the open.

    §7.5.1 calls the knob ``consecutive_busy_hours``; §7.5.3 states the rule as ">= 3 busy
    hours/day". This lane counts hours rather than requiring a run — a feed reporting 15:00,
    17:00 and 19:00 describes a cell that is busy all afternoon — and it puts the adjacency
    figure on every day's evidence so a reviewer who reads the spec the other way can see it.
    """
    _settings, session = tmp_db
    _seed(session, days=7, value=76.0, hours=(15, 17, 19))

    reading = _read(session)
    assert reading.verdict == svc.VERDICT_SUSTAINED
    day = reading.days[0]
    assert day.hours_at_or_above == 3
    assert day.longest_run == 1  # no two of 15:00/17:00/19:00 are adjacent
    assert day.as_json()["longest_adjacent_run"] == 1


def test_a_re_sent_export_cannot_inflate_a_days_busy_hour_count(tmp_db, on):
    """Two belts (module docstring, idea 4): the ingest skips duplicates AND the day count is
    over distinct hour slots, so even a duplicate that lands cannot manufacture a trend."""
    _settings, session = tmp_db
    drafts = [
        svc.ObservationDraft(SITE, CELL, DEFAULT_METRIC, 95.0, NOW - timedelta(days=1, hours=6), "CSV")
        for _ in range(9)
    ]
    report = svc.ingest_observations(session, drafts, actor="Peter Kamau")
    assert report.accepted == 1 and report.duplicates == 8

    # And if nine identical rows somehow existed, the day would still be one busy hour.
    _seed(session, days=1, value=95.0, hours=(16,))
    _seed(session, days=1, value=95.0, hours=(16,))
    day_slots = {
        svc._hour_slot(r.busy_hour_at)
        for r in session.query(CapacityObservationRow).all()
        if r.value >= 70
    }
    assert len(day_slots) <= 2


# ------------------------------------------------------- an advisory is advice, not an action


def test_a_capacity_advisory_never_schedules_maintenance_touches_an_incident_or_opens_a_stop_clock(tmp_db, on):
    """§7.5.3's whole sentence, asserted as the absence of four kinds of row.

    Checked after the service call *and* after a full scan job, because the job is the thing a
    later "make this more useful" change would reach for. The maintenance lane keeps the same
    line by giving its stop-clock proposal no acceptance helper.
    """
    settings, session = tmp_db
    _seed(session, days=7, value=85.0)

    advisory = svc.open_advisory(session, _read(session))
    assert advisory is not None
    svc.capacity_scan(session, settings, now=NOW)

    assert session.query(CapacityAdvisoryRow).count() == 1  # the advisory, and only the advisory
    assert session.query(MaintenanceWindowRow).count() == 0
    assert session.query(MaintenanceTaskRow).count() == 0
    assert session.query(HitlTaskRow).count() == 0
    assert session.query(ClockEventRow).count() == 0
    assert session.query(OutboxRow).count() == 0


def test_the_lane_offers_no_route_or_helper_that_turns_an_advisory_into_work(tmp_db, on):
    """The structural half of the rule: there is nothing to call and nothing to POST.

    A convenience wrapper is one refactor from being called by a job, so the absence is pinned
    rather than left to review. If a future change adds one deliberately, this test is the
    conversation about it.
    """
    import noc_agents.api.routers.capacity as routes

    for forbidden in ("accept_advisory", "schedule_from_advisory", "raise_window_for", "propose_task"):
        assert not hasattr(svc, forbidden)
    paths = {route.path for route in routes.router.routes}
    assert not any(p.endswith(("/schedule", "/raise-window", "/approve")) for p in paths)


def test_an_hourly_scan_does_not_reopen_an_advisory_it_has_already_raised(tmp_db, on):
    """One congested cell is one piece of advice, not one per tick."""
    settings, session = tmp_db
    _seed(session, days=7, value=85.0)

    svc.capacity_scan(session, settings, now=NOW)
    svc.capacity_scan(session, settings, now=NOW)
    svc.capacity_scan(session, settings, now=NOW)

    assert session.query(CapacityAdvisoryRow).count() == 1


# ------------------------------------------------------------ planned maintenance is excluded


def test_samples_taken_inside_an_approved_window_are_excluded_from_the_reading(tmp_db, on, monkeypatch):
    """A site off air for a battery swap did not have a capacity problem.

    And the consequence is followed through honestly: removing a day's samples drops this cell
    under the data floor, so the answer becomes "not enough data" rather than a trend computed
    from the six days that are left.
    """
    monkeypatch.setenv("MAINTENANCE_ENABLED", "true")
    _settings, session = tmp_db
    _seed(session, days=7, value=85.0)

    covered = (NOW - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    _window(session, status=WINDOW_SCHEDULED, starts_at=covered + timedelta(hours=14), ends_at=covered + timedelta(hours=18))

    reading = _read(session)
    assert reading.planned_exclusion == svc.PLANNED_APPLIED
    assert reading.planned_minutes == 240
    assert reading.excluded_planned == 3
    assert reading.observations == 18
    assert reading.verdict == svc.VERDICT_INSUFFICIENT_DATA
    # The day is still on the evidence, saying why it is empty rather than simply missing.
    empty_day = [d for d in reading.days if d.samples == 0]
    assert len(empty_day) == 1 and empty_day[0].excluded_planned == 3


def test_a_proposed_window_is_a_plan_and_excludes_nothing(tmp_db, on, monkeypatch):
    """``maintenance.scheduled_windows_for_site`` counts SCHEDULED only, and so does this lane.

    A PROPOSED window is somebody's intention. Excluding its minutes would let an unapproved
    plan quietly erase real traffic from a capacity figure.
    """
    monkeypatch.setenv("MAINTENANCE_ENABLED", "true")
    _settings, session = tmp_db
    _seed(session, days=7, value=85.0)

    covered = (NOW - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    _window(session, status=WINDOW_PROPOSED, starts_at=covered + timedelta(hours=14), ends_at=covered + timedelta(hours=18))

    reading = _read(session)
    assert reading.excluded_planned == 0
    assert reading.planned_minutes == 0
    assert reading.verdict == svc.VERDICT_SUSTAINED


def test_a_reading_says_out_loud_when_planned_maintenance_could_not_be_excluded(tmp_db, on, monkeypatch):
    """The DEFAULT case, and the one an unlabelled number would misrepresent.

    With ``MAINTENANCE_ENABLED`` off, ``maintenance.is_planned`` returns ``None`` by design:
    this deployment does not track planned work. The reading must say the exclusion was not
    applied rather than present unexcluded numbers as excluded ones — the rain guard's rule,
    applied here.
    """
    monkeypatch.delenv("MAINTENANCE_ENABLED", raising=False)
    _settings, session = tmp_db
    _seed(session, days=7, value=85.0)

    covered = (NOW - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    _window(session, status=WINDOW_SCHEDULED, starts_at=covered + timedelta(hours=14), ends_at=covered + timedelta(hours=18))

    reading = _read(session)
    assert reading.planned_exclusion == svc.PLANNED_NOT_TRACKED
    assert reading.planned_minutes is None
    assert reading.excluded_planned == 0
    assert "MAINTENANCE_ENABLED is off" in reading.reason


# ---------------------------------------------------------------------- operator isolation


def test_the_other_operators_observations_never_reach_this_operators_totals(tmp_db, on):
    """Both operators at the same site and the same cell — legal in this schema, and the
    scoping is the WHERE clause, never a check after the fetch (§8)."""
    _settings, session = tmp_db
    _seed(session, days=7, value=80.0)
    _seed(session, days=7, value=99.0, operator_id=OTHER_OPERATOR)

    reading = _read(session)
    assert reading.observations == 21  # not 42
    assert reading.peak_pct == 80.0  # never 99
    assert svc.cells_with_data(session, _cfg(), now=NOW) == [(SITE, CELL)]

    advisory = svc.open_advisory(session, reading)
    assert advisory is not None and advisory.operator_id == "safaricom"


def test_another_operators_advisory_is_a_404_and_never_a_403(client):
    """404, because a 403 confirms the id exists in the other operator's data (``deps.py``)."""
    session = _session(client)
    try:
        theirs = CapacityAdvisoryRow(
            id=new_id(), operator_id=OTHER_OPERATOR, site_id=SITE, cell_id=CELL, opened_at=NOW, status=ADVISORY_OPEN
        )
        session.add(theirs)
        session.commit()
        theirs_id = theirs.id
    finally:
        session.close()

    assert client.get(f"/api/v1/capacity/advisories/{theirs_id}").status_code == 404
    assert client.get("/api/v1/capacity/advisories").json() == []
    assert client.post(f"/api/v1/capacity/advisories/{theirs_id}/review", json={"status": "CLOSED"}).status_code == 404


# ----------------------------------------------------------------------------- ingest rules


def test_a_row_with_impossible_units_is_refused_and_the_whole_file_is_refused_with_it(tmp_db, on):
    """``DL_TOTAL_PRB_USAGE`` is a percentage. A 700 is raw PRBs or per-mille, and a units
    error that reaches the table is an advisory somebody spends capital on.

    All-or-nothing, and every reason at once: a 3 000-row export returned one error at a time
    is a morning gone.
    """
    _settings, session = tmp_db
    csv = (
        "site_id,cell_id,metric,value,busy_hour_at,source\n"
        f"{SITE},{CELL},DL_TOTAL_PRB_USAGE,74.0,2026-09-19 15:00:00,CSV\n"
        f"{SITE},{CELL},DL_TOTAL_PRB_USAGE,700,2026-09-19 16:00:00,CSV\n"
        f"{SITE},{CELL},NOT_A_METRIC,74.0,not-a-timestamp,CSV\n"
    )
    with pytest.raises(svc.CapacityRejected) as caught:
        svc.parse_csv(csv, cfg=_cfg(), now=NOW)

    reasons = " ".join(caught.value.errors)
    assert "row 3" in reasons and "outside 0-100" in reasons
    assert "row 4" in reasons and "NOT_A_METRIC" in reasons
    assert "not a timestamp" in reasons
    assert session.query(CapacityObservationRow).count() == 0


def test_a_csv_missing_a_required_column_is_a_different_file_and_is_refused(tmp_db, on):
    _settings, session = tmp_db
    with pytest.raises(svc.CapacityRejected) as caught:
        svc.parse_csv("site_id,value\nX,70\n", cfg=_cfg(), now=NOW)
    assert "missing column(s)" in caught.value.errors[0]
    assert "busy_hour_at" in caught.value.errors[0]


def test_a_naive_busy_hour_is_never_guessed_and_the_answer_says_which_zone_was_applied(tmp_db, on):
    """Three hours of silent error moves every sample out of the busy hour it describes — and
    out of the maintenance window that should have excluded it. So the zone is a parameter."""
    _settings, session = tmp_db
    row = {"site_id": SITE, "cell_id": CELL, "metric": DEFAULT_METRIC, "value": 74.0, "busy_hour_at": "2026-09-19 17:00:00"}

    as_utc, errors = svc.validate_observation(row, cfg=_cfg(), naive_tz=svc.NAIVE_TZ_UTC, now=NOW)
    as_eat, _ = svc.validate_observation(row, cfg=_cfg(), naive_tz=svc.NAIVE_TZ_EAT, now=NOW)
    assert not errors
    assert as_utc.busy_hour_at == datetime(2026, 9, 19, 17, 0)
    assert as_eat.busy_hour_at == datetime(2026, 9, 19, 14, 0)  # EAT is UTC+3

    # An offset-carrying timestamp is believed, whatever naive_tz says.
    aware, _ = svc.validate_observation(
        {**row, "busy_hour_at": "2026-09-19T17:00:00+03:00"}, cfg=_cfg(), naive_tz=svc.NAIVE_TZ_UTC, now=NOW
    )
    assert aware.busy_hour_at == datetime(2026, 9, 19, 14, 0)

    report = svc.ingest_observations(session, [as_eat], actor="Peter Kamau", naive_tz=svc.NAIVE_TZ_EAT)
    assert report.as_json()["naive_tz_applied"] == "EAT"


def test_a_busy_hour_stamped_in_the_future_is_refused_rather_than_stored(tmp_db, on):
    """A mis-set exporting clock parks samples outside every lookback window, then delivers
    them all at once months later as a sudden week of congestion."""
    _settings, session = tmp_db
    _draft, errors = svc.validate_observation(
        {"site_id": SITE, "value": 74.0, "busy_hour_at": (NOW + timedelta(days=30)).isoformat()},
        cfg=_cfg(),
        now=NOW,
    )
    assert any("future" in e for e in errors)


def test_the_shipped_sample_csv_parses_and_reads_as_the_demo_promises(tmp_db, on):
    """§7.5.4's ``data/seed/v2/capacity_sample.csv``, end to end and offline.

    The shipped file is three cells — one clearly over the 7-day threshold, one under it and one
    quiet control — and ``tests/unit/test_seed_v2.py`` pins its row count and that exact shape,
    so it is read here rather than extended. The thin-feed case that produces no number at all
    is covered by this file's own fixtures instead.
    """
    _settings, session = tmp_db
    drafts = svc.parse_csv(SAMPLE_CSV.read_text(encoding="utf-8"), cfg=_cfg(), now=datetime(2026, 9, 9))
    report = svc.ingest_observations(session, drafts, actor="Peter Kamau", source="CSV")
    assert report.accepted == len(drafts)
    assert report.duplicates == 0

    at = datetime(2026, 9, 9)
    verdicts = {
        (r.site_id, r.cell_id): r.verdict
        for r in [
            svc.read_capacity(session, _cfg(), site_id=site, cell_id=cell, now=at)
            for site, cell in svc.cells_with_data(session, _cfg(), now=at)
        ]
    }
    assert verdicts[("SFC-NBIE-ENB-KAY12", "KAY12-L1800-1")] == svc.VERDICT_SUSTAINED
    # Four qualifying days out of eight is a cell to watch, not a cell to advise on.
    assert verdicts[("SFC-CST-ENB-NYL12", "NYL12-L2600-3")] == svc.VERDICT_BELOW_TRIGGER
    assert verdicts[("SFC-RFT-ENB-NKR-A1", "NKRA1-L0800-2")] == svc.VERDICT_BELOW_TRIGGER

    # Re-ingesting the same file is a no-op, which is what makes a retried upload safe.
    again = svc.ingest_observations(session, drafts, actor="Peter Kamau", source="CSV")
    assert again.accepted == 0 and again.duplicates == len(drafts)


# ---------------------------------------------------------------------------------- review


def test_only_a_named_human_may_review_an_advisory_and_a_closed_one_stays_closed(tmp_db, on):
    """A job may say "look at this"; only a person may say "seen" or "no action". Re-opening a
    CLOSED advisory would rewrite the record of what Planning decided."""
    _settings, session = tmp_db
    _seed(session, days=7, value=85.0)
    advisory = svc.open_advisory(session, _read(session))

    with pytest.raises(svc.CapacityRejected):
        svc.review_advisory(session, advisory, status=ADVISORY_ACKNOWLEDGED, actor=svc.CAPACITY_AGENT)
    with pytest.raises(svc.CapacityRejected):
        svc.review_advisory(session, advisory, status="ACTIONED", actor=REVIEWER)

    svc.review_advisory(session, advisory, status=ADVISORY_ACKNOWLEDGED, actor=REVIEWER, note="with Planning")
    assert advisory.status == ADVISORY_ACKNOWLEDGED and advisory.reviewed_by == REVIEWER

    svc.review_advisory(session, advisory, status=ADVISORY_CLOSED, actor=REVIEWER, note="new site in Q1")
    assert advisory.status == ADVISORY_CLOSED
    with pytest.raises(svc.CapacityRejected):
        svc.review_advisory(session, advisory, status=ADVISORY_ACKNOWLEDGED, actor=REVIEWER)


# ------------------------------------------------------------------------------------- API


def test_the_api_ingests_json_and_serves_the_reading_with_its_verdict(client):
    """§7.5.2's ingest and the site read, over HTTP."""
    payload = {
        "observations": [
            {
                "site_id": SITE,
                "cell_id": CELL,
                "metric": DEFAULT_METRIC,
                "value": 84.0,
                "busy_hour_at": (NOW - timedelta(days=day, hours=24 - hour)).isoformat(),
                "source": "MANUAL",
            }
            for day in range(1, 8)
            for hour in BUSY_HOURS
        ]
    }
    posted = client.post("/api/v1/capacity/observations", json=payload)
    assert posted.status_code == 200, posted.text
    assert posted.json()["accepted"] == 21
    assert posted.json()["naive_tz_applied"] == "UTC"

    site = client.get(f"/api/v1/capacity/sites/{SITE}").json()
    assert site["has_data"] is True
    assert len(site["cells"]) == 1
    assert site["cells"][0]["verdict"] in (svc.VERDICT_SUSTAINED, svc.VERDICT_BELOW_TRIGGER)

    # A site nobody has ever sent a sample for is not a site with no capacity problem.
    empty = client.get("/api/v1/capacity/sites/SFC-RFT-ENB-NKR-A1").json()
    assert empty["has_data"] is False and empty["cells"] == []


def test_the_api_refuses_a_bad_batch_whole_and_lists_every_reason(client):
    response = client.post(
        "/api/v1/capacity/observations",
        json={
            "observations": [
                {"site_id": SITE, "value": 74.0, "busy_hour_at": "2026-09-19T15:00:00Z"},
                {"site_id": "", "value": 700.0, "busy_hour_at": "2026-09-19T16:00:00Z"},
            ]
        },
    )
    assert response.status_code == 422
    errors = response.json()["detail"]["errors"]
    assert any("site_id is required" in e for e in errors)
    assert any("outside 0-100" in e for e in errors)

    session = _session(client)
    try:
        assert session.query(CapacityObservationRow).count() == 0
    finally:
        session.close()


def test_the_csv_route_is_absent_until_the_upload_lane_is_enabled_too(client, monkeypatch):
    """Turning on capacity must not silently turn on a file-upload path §7.9.5 ships off."""
    monkeypatch.setenv("UPLOADS_ENABLED", "false")
    response = client.post("/api/v1/capacity/observations/csv", content=b"site_id\n", headers={"content-type": "text/csv"})
    assert response.status_code == 404
    assert "UPLOADS_ENABLED" in response.json()["detail"]


def test_the_csv_route_streams_the_body_through_the_shared_upload_validator(client, monkeypatch, tmp_path):
    """One upload path for the whole system: the bytes are sniffed, the cap is enforced while
    reading, and a rejected file is never written to disk."""
    monkeypatch.setenv("UPLOADS_ENABLED", "true")
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    csv = (
        "site_id,cell_id,metric,value,busy_hour_at,source\n"
        + "".join(
            f"{SITE},{CELL},DL_TOTAL_PRB_USAGE,84.0,{(NOW - timedelta(days=day, hours=24 - hour)):%Y-%m-%d %H:%M:%S},CSV\n"
            for day in range(1, 8)
            for hour in BUSY_HOURS
        )
    ).encode("utf-8")

    ok = client.post(
        "/api/v1/capacity/observations/csv",
        content=csv,
        headers={"content-type": "text/csv"},
        params={"filename": "prb_export.csv"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["accepted"] == 21
    assert len(ok.json()["sha256"]) == 64

    # A PDF is refused on its bytes, whatever it claims, and 415 rather than 400.
    bad = client.post(
        "/api/v1/capacity/observations/csv",
        content=b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n",
        headers={"content-type": "text/csv"},
    )
    assert bad.status_code == 415
    assert not any((tmp_path / "uploads").glob("*.bin")) if (tmp_path / "uploads").exists() else True


def test_the_api_records_a_review_and_still_books_no_work(client):
    """The only write a person makes against an advisory, and it reaches nothing else."""
    session = _session(client)
    try:
        _seed(session, days=7, value=85.0)
        advisory = svc.open_advisory(session, _read(session))
        session.commit()
        advisory_id = advisory.id
    finally:
        session.close()

    listed = client.get("/api/v1/capacity/advisories", params={"status": "OPEN"}).json()
    assert len(listed) == 1 and listed[0]["advice_only"] is True

    reviewed = client.post(
        f"/api/v1/capacity/advisories/{advisory_id}/review",
        json={"status": "CLOSED", "note": "new site in Q1", "actor": REVIEWER},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == ADVISORY_CLOSED
    assert reviewed.json()["reviewed_by"] == REVIEWER

    session = _session(client)
    try:
        assert session.query(MaintenanceWindowRow).count() == 0
        assert session.query(HitlTaskRow).count() == 0
        assert session.query(ClockEventRow).count() == 0
    finally:
        session.close()


# ----------------------------------------------------------------------------- the job card


def test_the_scan_job_is_registered_in_the_scheduler_and_ships_off(tmp_db):
    """The card is wired in, and wiring it in is safe because it ships off.

    The original version of this test asserted the OPPOSITE -- that the card was absent from
    ``SCHEDULED_JOBS`` -- which was an accurate statement about its author's own scope (a lane
    may not edit the shared scheduler module; that file is integrated once, deliberately). It
    is the wrong thing to pin permanently, though: as an invariant it forbids the lane from
    ever being scheduled, so the person who eventually wires it up correctly is met by a red
    test telling them not to. The complaints lane shipped the same anti-invariant and it was
    replaced for the same reason.

    What is worth protecting is not "unregistered" but "registered and inert": a card in
    ``SCHEDULED_JOBS`` carrying ``default_enabled=False``, whose job re-checks its own flag,
    cannot run on a deployment that did not ask for it.
    """
    from noc_agents.scheduler.loop import SCHEDULED_JOBS, job_enabled

    card = svc.CAPACITY_SCAN_JOB
    assert card.enabled_env == "CAPACITY_ENABLED"
    assert card.default_enabled is False
    registered = next((j for j in SCHEDULED_JOBS if j.name == card.name), None)
    assert registered is not None, (
        f"{card.name!r} is not in SCHEDULED_JOBS; capacity advisories would never be opened"
    )
    assert registered is card  # the registered card is this lane's own, not a copy
    assert job_enabled(registered) is False  # CAPACITY_ENABLED is unset in the suite (conftest)
