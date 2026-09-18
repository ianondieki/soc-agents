"""Spec §7.6.1-§7.6.3 -- stop-clock events (SCCs): the arithmetic, the write path, the
discipline counter, and the routes (Phase 4 Lane 4A, step 1).

The arithmetic section is the commercial crux and is pure (no database): every case is a
handful of unsaved ``ClockEventRow`` objects and a window. Three rules are pinned because
each one, wrong, moves money:

* overlapping events deduct their UNION, never their sum;
* an event still open at the period end is bounded by the period end;
* a reversed event deducts nothing (but stays visible in the breakdown).

The discipline counter (``opened_at - started_at``) is pinned as operator-side: it is
computed from the server-stamped ``opened_at`` and never touches the deduction.

Roles are pinned twice on purpose: in the service (a vendor role is refused even with
``AUTH_DISABLED=true``, §7.6.6) and through the route with the demo role switcher.
"""

from __future__ import annotations

import importlib
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from noc_agents.api import auth
from noc_agents.api.deps import OPERATIONS, SUPERVISORS
from noc_agents.config import get_settings
from noc_agents.db.models import AuditRow, IncidentRow, WorkNoteRow
from noc_agents.db.models_vendors import SCC_CODES, ClockEventRow, VendorRow
from noc_agents.domain.schemas import EventIngest
from noc_agents.graph.pipeline import process_event
from noc_agents.realtime.commit_hook import pending_events
from noc_agents.realtime.hub import hub
from noc_agents.services.clock_events import (
    CLOSER_ROLES,
    LATE_OPENING_THRESHOLD_MIN,
    OPENER_ROLES,
    ClockPermissionError,
    ClockStateError,
    Interval,
    clock_event_out,
    close_clock_event,
    deducted_minutes,
    deducted_minutes_for_incident,
    deducted_timedelta,
    effective_intervals,
    incident_window,
    late_openings,
    list_clock_events,
    open_clock_event,
    opening_delay_minutes,
    reverse_clock_event,
    scc_breakdown,
)
from noc_agents.services.vendors import FLAG

T0 = datetime(2026, 9, 1, 0, 0, 0)


def _h(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def _ev(start: float, end: float | None, *, code: str = "UTILITY_POWER", reversed_: bool = False, opened_after_min: int = 0, id_: str | None = None) -> ClockEventRow:
    """An unsaved event: ``start``/``end`` in hours after T0; ``end=None`` = still open."""
    started = _h(start)
    return ClockEventRow(
        id=id_ or f"ev-{code}-{start}-{end}",
        incident_id="inc",
        scc_code=code,
        started_at=started,
        ended_at=_h(end) if end is not None else None,
        opened_by="NOC Analyst",
        opened_role="noc_analyst",
        opened_at=started + timedelta(minutes=opened_after_min),
        reason="test",
        reversed_at=_h(99) if reversed_ else None,
        reversed_by="Sup" if reversed_ else None,
        reversal_reason="withdrawn" if reversed_ else None,
        created_at=started,
    )


WINDOW = dict(window_start=_h(0), window_end=_h(10))


# --------------------------------------------------------------------------
# Arithmetic (pure)
# --------------------------------------------------------------------------


def test_roles_are_the_deps_allow_lists_and_codes_are_the_spec_list():
    assert OPENER_ROLES == OPERATIONS and "noc_analyst" in OPENER_ROLES
    assert CLOSER_ROLES == SUPERVISORS and "noc_analyst" not in CLOSER_ROLES
    assert "msp_coordinator" not in OPENER_ROLES and "field_engineer" not in OPENER_ROLES
    assert len(SCC_CODES) == 10 and "UTILITY_POWER" in SCC_CODES
    assert LATE_OPENING_THRESHOLD_MIN == 60


def test_a_single_closed_event_deducts_its_own_length():
    assert deducted_minutes([_ev(1, 2)], **WINDOW) == 60
    assert effective_intervals([_ev(1, 2)], **WINDOW) == [Interval(_h(1), _h(2))]


def test_overlapping_events_deduct_their_union_not_their_sum():
    """Two SCCs covering the same hour stop the clock ONCE. 1-3h + 2-4h = 3h, not 4h."""
    events = [_ev(1, 3, code="UTILITY_POWER"), _ev(2, 4, code="SITE_ACCESS_DENIED")]
    assert deducted_minutes(events, **WINDOW) == 180
    assert effective_intervals(events, **WINDOW) == [Interval(_h(1), _h(4))]


def test_an_event_nested_inside_another_adds_nothing():
    assert deducted_minutes([_ev(1, 5), _ev(2, 3)], **WINDOW) == 240


def test_adjacent_events_are_summed_exactly_once_at_the_join():
    assert deducted_minutes([_ev(1, 2), _ev(2, 3)], **WINDOW) == 120
    assert effective_intervals([_ev(1, 2), _ev(2, 3)], **WINDOW) == [Interval(_h(1), _h(3))]


def test_disjoint_events_are_summed():
    assert deducted_minutes([_ev(1, 2), _ev(5, 7)], **WINDOW) == 180
    assert effective_intervals([_ev(5, 7), _ev(1, 2)], **WINDOW) == [Interval(_h(1), _h(2)), Interval(_h(5), _h(7))]


def test_an_event_straddling_the_window_is_clipped_to_it():
    assert deducted_minutes([_ev(-1, 1)], **WINDOW) == 60  # started before the outage
    assert deducted_minutes([_ev(9, 12)], **WINDOW) == 60  # ran past the period end
    assert deducted_minutes([_ev(-5, 15)], **WINDOW) == 600  # covers everything: the whole window, no more


def test_an_event_entirely_outside_the_window_deducts_nothing():
    assert deducted_minutes([_ev(11, 12)], **WINDOW) == 0
    assert deducted_minutes([_ev(-2, -1)], **WINDOW) == 0
    assert deducted_minutes([_ev(-2, 0)], **WINDOW) == 0  # ends exactly at the window start: empty


def test_an_event_still_open_is_bounded_by_the_window_end():
    """A stop clock nobody closed does not deduct minutes from next month."""
    assert deducted_minutes([_ev(8, None)], **WINDOW) == 120
    assert deducted_minutes([_ev(8, None)], window_start=_h(0), window_end=_h(20)) == 720  # a later window sees more of it
    assert deducted_minutes([_ev(7, 9), _ev(8, None)], **WINDOW) == 180  # open + overlapping closed: union


def test_a_reversed_event_deducts_nothing_alone_or_in_company():
    assert deducted_minutes([_ev(1, 2, reversed_=True)], **WINDOW) == 0
    assert deducted_minutes([_ev(1, 2, reversed_=True), _ev(5, 6)], **WINDOW) == 60
    assert deducted_minutes([_ev(0, 10, reversed_=True), _ev(1, 2)], **WINDOW) == 60  # a reversed blanket event hides nothing
    assert deducted_minutes([_ev(1, None, reversed_=True)], **WINDOW) == 0  # reversed while still open


def test_an_empty_or_inverted_window_deducts_nothing():
    assert deducted_minutes([_ev(1, 2)], window_start=_h(5), window_end=_h(5)) == 0
    assert effective_intervals([_ev(1, 2)], window_start=_h(6), window_end=_h(5)) == []
    assert deducted_minutes([], **WINDOW) == 0


def test_minutes_are_truncated_and_the_exact_figure_is_available():
    ev = _ev(1, None)
    ev.ended_at = _h(1) + timedelta(seconds=90)
    assert deducted_timedelta([ev], **WINDOW) == timedelta(seconds=90)
    assert deducted_minutes([ev], **WINDOW) == 1  # a partial minute is not credited


def test_golden_three_sccs_on_a_six_hour_outage():
    """The mini golden the scorecard's twelve-incident fixture builds on: a power SCC, an
    access SCC overlapping it by 30 min, an OBSERVATION clock still open at restore, and a
    reversed FORCE_MAJEURE blanket that must change nothing. Outage 00:00-06:00."""
    events = [
        _ev(0.5, 2.0, code="UTILITY_POWER"),
        _ev(1.5, 3.0, code="SITE_ACCESS_DENIED"),
        _ev(5.0, None, code="OBSERVATION"),
        _ev(0.0, 6.0, code="FORCE_MAJEURE", reversed_=True),
    ]
    window = dict(window_start=_h(0), window_end=_h(6))
    assert effective_intervals(events, **window) == [Interval(_h(0.5), _h(3.0)), Interval(_h(5.0), _h(6.0))]
    assert deducted_minutes(events, **window) == 150 + 60 == 210
    # The naive sum would be 90 + 90 + 60 (+ 360 reversed) -- the union is what is deducted.
    assert sum(line["minutes"] for line in scc_breakdown(events, **window)) == 240


def test_breakdown_shows_each_claim_as_made_and_the_total_deducts_once():
    events = [_ev(1, 3, id_="a"), _ev(2, 4, id_="b"), _ev(5, None, id_="c"), _ev(6, 7, id_="d", reversed_=True)]
    lines = scc_breakdown(events, **WINDOW)
    assert [l["event_id"] for l in lines] == ["a", "b", "c", "d"]
    assert [l["minutes"] for l in lines] == [120, 120, 300, 0]
    assert sum(l["minutes"] for l in lines) == 540 > deducted_minutes(events, **WINDOW) == 480
    c = lines[2]
    assert c["open"] is True and c["ended_at"] is None and c["effective_end"] == _h(10)
    d = lines[3]
    assert d["reversed"] is True and d["effective_start"] is None and d["minutes"] == 0


# --------------------------------------------------------------------------
# Discipline counter: operator-side, never the vendor's
# --------------------------------------------------------------------------


def test_opening_delay_is_opened_at_minus_started_at_in_whole_minutes():
    assert opening_delay_minutes(_ev(1, 2, opened_after_min=90)) == 90
    assert opening_delay_minutes(_ev(1, 2, opened_after_min=0)) == 0
    ev = _ev(1, 2)
    ev.opened_at = ev.started_at + timedelta(seconds=119)
    assert opening_delay_minutes(ev) == 1


def test_opening_delay_never_goes_negative_on_permitted_clock_skew():
    ev = _ev(1, 2)
    ev.opened_at = ev.started_at - timedelta(seconds=30)  # started_at a little "in the future"
    assert opening_delay_minutes(ev) == 0


def test_late_openings_use_the_sixty_minute_threshold_and_count_reversed_ones_too():
    on_time = _ev(1, 2, opened_after_min=60, id_="on-time")  # exactly 60 is not late
    late = _ev(3, 4, opened_after_min=61, id_="late")
    late_reversed = _ev(5, 6, opened_after_min=200, reversed_=True, id_="late-reversed")
    assert late_openings([on_time, late, late_reversed]) == [late, late_reversed]
    assert late_openings([late], threshold_min=120) == []


def test_the_discipline_counter_never_changes_the_deduction():
    """Recorded 3 h late or on time, the vendor's minutes are the same."""
    assert deducted_minutes([_ev(1, 2, opened_after_min=180)], **WINDOW) == deducted_minutes([_ev(1, 2)], **WINDOW) == 60


# --------------------------------------------------------------------------
# Write path (service)
# --------------------------------------------------------------------------


def _incident(session, *, msp_name: str | None = "EGYPRO", number: str = "INC-CLK", operator_id: str = "safaricom", **extra) -> IncidentRow:
    inc = IncidentRow(
        operator_id=operator_id,
        incident_number=number,
        status="IN_PROGRESS",
        site_id="SFC-MTK-HUB-THK",
        region_code="MTK",
        correlation_fingerprint="fp",
        msp_name=msp_name,
        failure_time=T0,
        **extra,
    )
    session.add(inc)
    session.flush()
    return inc


def _open(session, inc, **kw) -> ClockEventRow:
    args = dict(scc_code="UTILITY_POWER", reason="KPLC outage confirmed", opened_by="J. Otieno", opened_role="noc_analyst", now=_h(2))
    args.update(kw)
    return open_clock_event(session, inc, **args)


def test_open_records_the_condition_and_stamps_opened_at_from_the_server_clock(tmp_db):
    _settings, session = tmp_db
    inc = _incident(session)
    ev = _open(session, inc, started_at=_h(1))
    assert (ev.scc_code, ev.reason, ev.opened_by, ev.opened_role) == ("UTILITY_POWER", "KPLC outage confirmed", "J. Otieno", "noc_analyst")
    assert ev.started_at == _h(1) and ev.ended_at is None and ev.is_open
    assert ev.opened_at == _h(2) == ev.created_at  # the server's now, not anything the caller typed
    assert opening_delay_minutes(ev) == 60
    assert ev.reversed_at is None and ev.evidence_note_id is None
    assert list_clock_events(session, inc.id) == [ev]


def test_open_defaults_started_at_to_now_and_accepts_an_earlier_observed_start(tmp_db):
    _settings, session = tmp_db
    inc = _incident(session)
    assert _open(session, inc).started_at == _h(2)
    assert _open(session, inc, started_at=_h(-20)).started_at == _h(-20)  # long before the outage: allowed, clipped later


def test_open_refuses_a_future_start_beyond_clock_skew(tmp_db):
    _settings, session = tmp_db
    inc = _incident(session)
    with pytest.raises(ValueError, match="future"):
        _open(session, inc, started_at=_h(2) + timedelta(minutes=2))
    ev = _open(session, inc, started_at=_h(2) + timedelta(seconds=30))  # inside the minute of skew
    assert opening_delay_minutes(ev) == 0


def test_open_converts_an_aware_timestamp_to_naive_utc(tmp_db):
    """A browser sends 03:00+03:00; the row must hold 00:00 naive UTC (§7.0.6)."""
    _settings, session = tmp_db
    inc = _incident(session)
    eat = datetime(2026, 9, 1, 3, 0, tzinfo=timezone(timedelta(hours=3)))
    assert _open(session, inc, started_at=eat).started_at == T0


def test_open_refuses_unknown_codes_empty_reasons_and_anonymous_openers(tmp_db):
    _settings, session = tmp_db
    inc = _incident(session)
    with pytest.raises(ValueError, match="scc_code"):
        _open(session, inc, scc_code="RAIN")
    with pytest.raises(ValueError, match="reason"):
        _open(session, inc, reason="   ")
    with pytest.raises(ValueError, match="opened_by"):
        _open(session, inc, opened_by="")
    assert list_clock_events(session, inc.id) == []
    assert _open(session, inc, scc_code=" utility_power ").scc_code == "UTILITY_POWER"  # tolerant of case/space


@pytest.mark.parametrize("role", ["msp_coordinator", "field_engineer", "management", "planning", "legal", "", "nobody"])
def test_only_noc_and_supervisor_roles_may_open(tmp_db, role):
    """§7.6.6: an SCC opened by a vendor role is refused -- in the service, so the demo
    role switcher cannot get around an inert require_role."""
    _settings, session = tmp_db
    inc = _incident(session)
    with pytest.raises(ClockPermissionError):
        _open(session, inc, opened_role=role)
    assert list_clock_events(session, inc.id) == []


@pytest.mark.parametrize("role", list(OPERATIONS))
def test_every_operations_role_may_open(tmp_db, role):
    _settings, session = tmp_db
    inc = _incident(session)
    assert _open(session, inc, opened_role=role).opened_role == role


def test_evidence_note_must_be_a_work_note_on_this_incident(tmp_db):
    _settings, session = tmp_db
    inc = _incident(session)
    other = _incident(session, number="INC-OTHER")
    mine = WorkNoteRow(incident_id=inc.id, author="KPLC desk", author_role="NOC", body="KPLC ref 12345")
    theirs = WorkNoteRow(incident_id=other.id, author="x", author_role="NOC", body="not this ticket")
    session.add_all([mine, theirs])
    session.flush()
    with pytest.raises(ValueError, match="evidence_note_id"):
        _open(session, inc, evidence_note_id=theirs.id)
    with pytest.raises(ValueError, match="evidence_note_id"):
        _open(session, inc, evidence_note_id="no-such-note")
    assert _open(session, inc, evidence_note_id=mine.id).evidence_note_id == mine.id


def test_opening_a_stop_clock_makes_vendor_id_mean_something(tmp_db):
    """A stop clock is vendor-attributable, so the first one on an incident whose
    ``msp_name`` resolves stamps ``vendor_id`` (seeding the vendors on the way)."""
    settings, session = tmp_db
    inc = _incident(session, msp_name="TETRANET")
    assert inc.vendor_id is None
    _open(session, inc, cfg=settings.operator)
    assert session.get(VendorRow, inc.vendor_id).code == "TETRANET"
    fe = _incident(session, msp_name=None, number="INC-FE")
    _open(session, fe, cfg=settings.operator)
    assert fe.vendor_id is None  # nothing to attribute to
    bare = _incident(session, number="INC-NOCFG")
    _open(session, bare)  # no cfg: the caller opted out of attribution
    assert bare.vendor_id is None


def test_close_is_for_supervisors_and_happens_once(tmp_db):
    _settings, session = tmp_db
    inc = _incident(session)
    ev = _open(session, inc, started_at=_h(1))
    with pytest.raises(ClockPermissionError):
        close_clock_event(session, inc, ev, closed_by="J. Otieno", closed_role="noc_analyst", now=_h(3))
    assert ev.ended_at is None
    close_clock_event(session, inc, ev, closed_by="Sup A", closed_role="shift_supervisor", now=_h(3))
    assert ev.ended_at == _h(3) and not ev.is_open
    with pytest.raises(ClockStateError, match="already closed"):
        close_clock_event(session, inc, ev, closed_by="Sup A", closed_role="shift_supervisor", now=_h(4))
    assert ev.ended_at == _h(3)


def test_close_honours_an_observed_end_time_but_not_an_impossible_one(tmp_db):
    _settings, session = tmp_db
    inc = _incident(session)
    ev = _open(session, inc, started_at=_h(1))
    with pytest.raises(ValueError, match="before started_at"):
        close_clock_event(session, inc, ev, closed_by="Sup", closed_role="duty_manager", ended_at=_h(0.5), now=_h(3))
    with pytest.raises(ValueError, match="future"):
        close_clock_event(session, inc, ev, closed_by="Sup", closed_role="duty_manager", ended_at=_h(3.5), now=_h(3))
    eat = datetime(2026, 9, 1, 5, 30, tzinfo=timezone(timedelta(hours=3)))  # 02:30 UTC
    close_clock_event(session, inc, ev, closed_by="Sup", closed_role="duty_manager", ended_at=eat, now=_h(3))
    assert ev.ended_at == _h(2.5)


def test_close_refuses_an_event_from_another_incident(tmp_db):
    _settings, session = tmp_db
    inc = _incident(session)
    other = _incident(session, number="INC-OTHER")
    ev = _open(session, other)
    with pytest.raises(ValueError, match="belong"):
        close_clock_event(session, inc, ev, closed_by="Sup", closed_role="admin", now=_h(3))


def test_reverse_needs_a_supervisor_and_a_reason_and_happens_once(tmp_db):
    _settings, session = tmp_db
    inc = _incident(session)
    ev = _open(session, inc, started_at=_h(1))
    with pytest.raises(ClockPermissionError):
        reverse_clock_event(session, inc, ev, reversed_by="J. Otieno", reversed_role="noc_analyst", reason="oops", now=_h(3))
    with pytest.raises(ValueError, match="reason"):
        reverse_clock_event(session, inc, ev, reversed_by="Sup", reversed_role="shift_supervisor", reason="  ", now=_h(3))
    assert ev.reversed_at is None
    reverse_clock_event(session, inc, ev, reversed_by="Sup A", reversed_role="shift_supervisor", reason="KPLC log shows no outage", now=_h(3))
    assert (ev.reversed_at, ev.reversed_by, ev.reversal_reason) == (_h(3), "Sup A", "KPLC log shows no outage")
    assert ev.is_reversed and not ev.is_open
    with pytest.raises(ClockStateError, match="already reversed"):
        reverse_clock_event(session, inc, ev, reversed_by="Sup A", reversed_role="shift_supervisor", reason="again", now=_h(4))
    with pytest.raises(ClockStateError, match="reversed"):
        close_clock_event(session, inc, ev, closed_by="Sup A", closed_role="shift_supervisor", now=_h(4))


def test_a_reversed_event_stays_in_the_table_but_deducts_nothing(tmp_db):
    _settings, session = tmp_db
    inc = _incident(session, restored_at=_h(6))
    live = _open(session, inc, started_at=_h(1), now=_h(1))
    close_clock_event(session, inc, live, closed_by="Sup", closed_role="admin", now=_h(2))
    gone = _open(session, inc, started_at=_h(3), now=_h(3), scc_code="FORCE_MAJEURE")
    assert deducted_minutes_for_incident(session, inc) == 60 + 180  # [1,2] + [3, restored 6]
    reverse_clock_event(session, inc, gone, reversed_by="Sup", reversed_role="admin", reason="not force majeure", now=_h(4))
    assert deducted_minutes_for_incident(session, inc) == 60
    assert len(list_clock_events(session, inc.id)) == 2  # the claim is kept, visibly withdrawn


def test_incident_window_is_failure_time_to_restored_at_or_now(tmp_db):
    _settings, session = tmp_db
    inc = _incident(session)
    assert incident_window(inc, now=_h(4)) == (T0, _h(4))  # live: deducted-so-far
    inc.restored_at = _h(6)
    assert incident_window(inc, now=_h(9)) == (T0, _h(6))
    inc.failure_time = None
    inc.outage_start_at = _h(0.5)
    assert incident_window(inc) == (_h(0.5), _h(6))
    ev = _open(session, inc, started_at=_h(-1), now=_h(1))
    close_clock_event(session, inc, ev, closed_by="Sup", closed_role="admin", ended_at=_h(7), now=_h(8))
    assert deducted_minutes_for_incident(session, inc) == 330  # clipped to [0.5, 6]
    # The caller's window wins over the incident's own: [-1, 7] clipped to [0, 10] is 7 h.
    assert deducted_minutes_for_incident(session, inc, window_start=_h(0), window_end=_h(10)) == 420


def test_every_transition_writes_an_audit_row(tmp_db):
    _settings, session = tmp_db
    inc = _incident(session)
    ev = _open(session, inc, started_at=_h(1))
    close_clock_event(session, inc, ev, closed_by="Sup", closed_role="admin", reason="power back", now=_h(3))
    ev2 = _open(session, inc, scc_code="OBSERVATION", now=_h(3))
    reverse_clock_event(session, inc, ev2, reversed_by="Sup", reversed_role="admin", reason="mistaken", now=_h(4))
    rows = session.scalars(select(AuditRow).where(AuditRow.entity_type == "clock_event").order_by(AuditRow.ts, AuditRow.action)).all()
    assert sorted((r.action, r.entity_id) for r in rows) == sorted(
        [("clock.opened", ev.id), ("clock.closed", ev.id), ("clock.opened", ev2.id), ("clock.reversed", ev2.id)]
    )
    assert all(r.operator_id == "safaricom" for r in rows)
    assert {r.rationale for r in rows if r.action == "clock.reversed"} == {"mistaken"}
    assert {r.rationale for r in rows if r.action == "clock.closed"} == {"power back"}


def test_realtime_events_are_buffered_and_leave_only_on_commit(tmp_db):
    """§7.0.4: the workspace is never told about a stop clock the database did not keep."""
    _settings, session = tmp_db
    inc = _incident(session)
    hub._history.clear()
    try:
        ev = _open(session, inc, started_at=_h(1))
        pending = pending_events(session)
        assert [(e.type, e.payload["change"], e.payload["event_id"]) for e in pending] == [("incident.updated", "clock.opened", ev.id)]
        assert pending[0].incident_id == inc.id and pending[0].operator_id == "safaricom"
        assert hub.recent(50) == []  # nothing published yet
        session.rollback()
        assert pending_events(session) == [] and hub.recent(50) == []  # a rollback announces nothing

        inc = _incident(session, number="INC-CLK-2")
        ev = _open(session, inc, started_at=_h(1))
        session.commit()
        published = [e for e in hub.recent(50) if e["type"] == "incident.updated"]
        assert [(e["payload"]["change"], e["payload"]["scc_code"]) for e in published] == [("clock.opened", "UTILITY_POWER")]
        assert published[0]["incident_id"] == inc.id
    finally:
        hub._history.clear()


def test_clock_event_out_z_stamps_every_timestamp_and_flags_lateness():
    ev = _ev(1, 2, opened_after_min=75, id_="x")
    out = clock_event_out(ev)
    assert out["id"] == "x" and out["scc_code"] == "UTILITY_POWER"
    assert out["started_at"].isoformat().endswith("Z") and out["ended_at"].isoformat().endswith("Z") and out["opened_at"].isoformat().endswith("Z")
    assert out["reversed_at"] is None and out["reversed"] is False and out["open"] is False
    assert (out["opening_delay_min"], out["late"]) == (75, True)
    assert clock_event_out(_ev(1, None, id_="y"))["open"] is True


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

HUB_EVENT = {
    "site_id": "SFC-NBIE-HUB-EMB",
    "site_name": "Embakasi East Aggregation HUB",
    "site_type": "HUB",
    "region_code": "NBI_E",
    "alarm_code": "POWER_GRID_FAIL",
    "failure_domain": "POWER",
    "users_affected": 450000,
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A live API on its own SQLite file with the lane ON (same reload pattern as the
    restore-provenance tests)."""
    db = tmp_path / "clocks.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
    monkeypatch.setenv(FLAG, "true")

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
        monkeypatch.delenv(FLAG, raising=False)
        importlib.reload(main)


def _ingest(client: TestClient, event: dict = HUB_EVENT) -> str:
    r = client.post("/api/v1/events", json=event)
    assert r.status_code == 200, r.text
    return r.json()["incident"]["id"]


def _as(client: TestClient, role: str, name: str | None = None) -> None:
    """Demo role switcher (AUTH_DISABLED=true): what the workspace's role menu does."""
    assert client.post("/api/v1/session", json={"display_name": name or role, "role": role}).status_code == 200


def test_routes_are_a_404_while_the_flag_is_off(client, monkeypatch):
    inc_id = _ingest(client)
    monkeypatch.setenv(FLAG, "false")
    assert client.get(f"/api/v1/incidents/{inc_id}/clock").status_code == 404
    assert client.post(f"/api/v1/incidents/{inc_id}/clock", json={"scc_code": "OBSERVATION", "reason": "x"}).status_code == 404
    assert client.post(f"/api/v1/incidents/{inc_id}/clock/ev/close", json={}).status_code == 404
    assert client.post(f"/api/v1/incidents/{inc_id}/clock/ev/reverse", json={"reason": "x"}).status_code == 404
    monkeypatch.setenv(FLAG, "true")
    assert client.get(f"/api/v1/incidents/{inc_id}/clock").status_code == 200


def test_open_list_close_and_reverse_through_the_workspace_control(client):
    from noc_agents.db.models import get_session, utcnow

    inc_id = _ingest(client)
    # Give the ticket a two-hour-old outage so a stop clock started 30 min ago lies inside it.
    session = get_session()
    try:
        row = session.get(IncidentRow, inc_id)
        row.failure_time = utcnow() - timedelta(hours=2)
        session.commit()
    finally:
        session.close()

    empty = client.get(f"/api/v1/incidents/{inc_id}/clock").json()
    assert (empty["events"], empty["deducted_minutes"], empty["late_openings"], empty["restored"]) == ([], 0, 0, False)
    assert empty["vendor_id"] is None

    started = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    r = client.post(
        f"/api/v1/incidents/{inc_id}/clock",
        json={"scc_code": "UTILITY_POWER", "reason": "KPLC outage confirmed on 95551", "started_at": started, "opened_by": "J. Otieno"},
    )
    assert r.status_code == 200, r.text
    ev = r.json()["event"]
    assert (ev["scc_code"], ev["opened_role"], ev["opened_by"], ev["open"]) == ("UTILITY_POWER", "noc_analyst", "J. Otieno", True)
    assert ev["opening_delay_min"] == 30 and ev["late"] is False
    clock = r.json()["clock"]
    assert 29 <= clock["deducted_minutes"] <= 30  # live "so far": [now-30min, now]
    assert clock["vendor_id"] is not None  # the incident now points at EGYPRO's row

    listed = client.get(f"/api/v1/incidents/{inc_id}/clock").json()
    assert [e["id"] for e in listed["events"]] == [ev["id"]]

    # The analyst may not close it; the supervisor may, and the deduction freezes.
    assert client.post(f"/api/v1/incidents/{inc_id}/clock/{ev['id']}/close", json={}).status_code == 403
    _as(client, "shift_supervisor", "Sup A")
    r = client.post(f"/api/v1/incidents/{inc_id}/clock/{ev['id']}/close", json={"reason": "power back"})
    assert r.status_code == 200, r.text
    assert r.json()["event"]["open"] is False and r.json()["event"]["ended_at"].endswith("Z")
    assert 29 <= r.json()["clock"]["deducted_minutes"] <= 30
    assert client.post(f"/api/v1/incidents/{inc_id}/clock/{ev['id']}/close", json={}).status_code == 409

    # Reversal needs a reason (an empty one is a 400, a missing one a 422) and happens once.
    assert client.post(f"/api/v1/incidents/{inc_id}/clock/{ev['id']}/reverse", json={"reason": "  "}).status_code == 400
    assert client.post(f"/api/v1/incidents/{inc_id}/clock/{ev['id']}/reverse", json={}).status_code == 422
    r = client.post(f"/api/v1/incidents/{inc_id}/clock/{ev['id']}/reverse", json={"reason": "KPLC log shows supply was on"})
    assert r.status_code == 200, r.text
    assert r.json()["event"]["reversed"] is True and r.json()["event"]["reversed_by"] == "Sup A"
    assert r.json()["clock"]["deducted_minutes"] == 0  # the withdrawn claim deducts nothing...
    assert len(r.json()["clock"]["events"]) == 1  # ...but stays visible
    assert client.post(f"/api/v1/incidents/{inc_id}/clock/{ev['id']}/reverse", json={"reason": "again"}).status_code == 409


def test_a_vendor_role_is_refused_even_with_auth_off(client):
    """§7.6.6 through the demo role switcher: require_role is inert, the service is not."""
    inc_id = _ingest(client)
    for role in ("msp_coordinator", "field_engineer"):
        _as(client, role)
        r = client.post(f"/api/v1/incidents/{inc_id}/clock", json={"scc_code": "OBSERVATION", "reason": "our fault, honest"})
        assert r.status_code == 403, (role, r.text)
    assert client.get(f"/api/v1/incidents/{inc_id}/clock").json()["events"] == []
    _as(client, "noc_analyst")
    assert client.post(f"/api/v1/incidents/{inc_id}/clock", json={"scc_code": "OBSERVATION", "reason": "x"}).status_code == 200


def test_bad_input_is_a_400_and_the_evidence_note_must_be_on_the_ticket(client):
    inc_id = _ingest(client)
    other_id = _ingest(client, {**HUB_EVENT, "site_id": "SFC-MTK-BTS-01", "region_code": "MTK", "alarm_code": "SITE_DOWN"})
    assert client.post(f"/api/v1/incidents/{inc_id}/clock", json={"scc_code": "RAIN", "reason": "x"}).status_code == 400
    assert client.post(f"/api/v1/incidents/{inc_id}/clock", json={"scc_code": "OBSERVATION", "reason": ""}).status_code == 400
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert client.post(f"/api/v1/incidents/{inc_id}/clock", json={"scc_code": "OBSERVATION", "reason": "x", "started_at": future}).status_code == 400

    note = client.post(f"/api/v1/incidents/{other_id}/notes", json={"author": "KPLC desk", "author_role": "NOC", "body": "KPLC ref 12345"})
    assert note.status_code == 200, note.text
    from noc_agents.db.models import get_session

    session = get_session()
    try:
        note_id = session.scalar(select(WorkNoteRow.id).where(WorkNoteRow.incident_id == other_id, WorkNoteRow.body == "KPLC ref 12345"))
    finally:
        session.close()
    assert note_id
    assert client.post(f"/api/v1/incidents/{inc_id}/clock", json={"scc_code": "UTILITY_POWER", "reason": "x", "evidence_note_id": note_id}).status_code == 400
    r = client.post(f"/api/v1/incidents/{other_id}/clock", json={"scc_code": "UTILITY_POWER", "reason": "x", "evidence_note_id": note_id})
    assert r.status_code == 200 and r.json()["event"]["evidence_note_id"] == note_id


def test_another_operators_incident_and_events_are_a_404_never_a_403(client):
    from noc_agents.db.models import get_session

    session = get_session()
    try:
        atl = process_event(
            session,
            get_settings("airtel"),
            EventIngest(site_id="ATL-NBI-HUB-001", site_name="Airtel HUB", site_type="HUB", region_code="NBI", alarm_code="POWER_GRID_FAIL", failure_domain="POWER", users_affected=450000),
        )
        atl_id = atl.id
        atl_ev = open_clock_event(session, atl, scc_code="OBSERVATION", reason="theirs", opened_by="ATL NOC", opened_role="noc_analyst")
        atl_ev_id = atl_ev.id
        session.commit()
    finally:
        session.close()

    assert client.get(f"/api/v1/incidents/{atl_id}/clock").status_code == 404
    assert client.post(f"/api/v1/incidents/{atl_id}/clock", json={"scc_code": "OBSERVATION", "reason": "x"}).status_code == 404
    _as(client, "shift_supervisor")
    assert client.post(f"/api/v1/incidents/{atl_id}/clock/{atl_ev_id}/close", json={}).status_code == 404
    assert client.post(f"/api/v1/incidents/{atl_id}/clock/{atl_ev_id}/reverse", json={"reason": "x"}).status_code == 404
    # ...and an event cannot be reached through a DIFFERENT incident of our own either.
    mine = _ingest(client)
    assert client.post(f"/api/v1/incidents/{mine}/clock/{atl_ev_id}/close", json={}).status_code == 404
    assert client.get("/api/v1/incidents/no-such/clock").status_code == 404


def test_an_event_reached_through_the_wrong_own_incident_is_a_404(client):
    a = _ingest(client)
    b = _ingest(client, {**HUB_EVENT, "site_id": "SFC-MTK-BTS-02", "region_code": "MTK", "alarm_code": "SITE_DOWN"})
    ev = client.post(f"/api/v1/incidents/{a}/clock", json={"scc_code": "OBSERVATION", "reason": "x"}).json()["event"]["id"]
    _as(client, "duty_manager")
    assert client.post(f"/api/v1/incidents/{b}/clock/{ev}/close", json={}).status_code == 404
    assert client.post(f"/api/v1/incidents/{b}/clock/{ev}/reverse", json={"reason": "x"}).status_code == 404
    assert client.post(f"/api/v1/incidents/{a}/clock/{ev}/close", json={}).status_code == 200
