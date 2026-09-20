"""Regulatory notification clocks — spec §5.3.20, §7.6, metric M10, and the DPA s.43 rule.

Three things this file exists to protect, in order of how much damage their absence does:

**M10: "100 % drafted; 0 auto-sent."** A written notification reaching the Communications
Authority or the ODPC without a named human approving it would be a serious incident in its
own right. ``test_a_regulatory_draft_cannot_reach_the_outbox_dispatcher_without_a_named_human_approval``
is the test that proves it cannot, and it enumerates the bypasses rather than testing the
happy path twice: no card at all, an unresolved card, a card of the wrong type, a card for a
different notice, an agent or an autonomy policy as the "approver", and the raiser approving
their own notice. Every one is refused and leaves the notice untouched, and — belt and
braces — the outbox's own dispatcher refuses the row independently if the approval on it is
ever cleared. A seventh route, another operator's approval, has its own test below.

**The clock starts at ``failure_time``.** Not at detection, not at row creation. A notice
opened six hours late is born with six hours already gone; that is the whole reason
``significance_json.reason_for_delay`` exists (§9.2, DPA 2019 s.43).

**Three hours.** The CA reads Nairobi time (EAT, UTC+3, no DST); the database stores naive
UTC. Getting that backwards moves a 24-hour statutory deadline by three hours in the wrong
direction, which is the kind of bug that is invisible until it is a finding.
"""

from __future__ import annotations

import importlib
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from noc_agents.api import auth
from noc_agents.config import get_settings
from noc_agents.db.models import HitlTaskRow, IncidentRow, OutboxRow, utcnow
from noc_agents.db.models_regulatory import (
    DRAFT,
    NOT_REQUIRED,
    PENDING_APPROVAL,
    REGULATORY_KINDS,
    SENT,
    RegulatoryNotificationRow,
)
from noc_agents.domain.enums import HitlTaskType
from noc_agents.orchestrator import outbox
from noc_agents.realtime.commit_hook import pending_events
from noc_agents.realtime.hub import hub
from noc_agents.services.clock import fmt_eat
from noc_agents.services.regulatory import (
    COUNTDOWN_THRESHOLDS_H,
    DEADLINE_EVENT,
    NOTICE_ENTITY_TYPE,
    REGULATORY_RAISER,
    REGULATORY_TASK_TYPE,
    ClockStartUnknown,
    QUEUED,
    NoticeNotApproved,
    NoticeStateError,
    approval_of,
    countdown,
    due_at_for,
    evaluate_and_open,
    evaluate_significance,
    open_notification,
    regulatory_enabled,
    release_notice,
    request_approval,
    sweep_deadlines,
)

# 12:00 EAT on the reference storm day, as stored (naive UTC). The three-hour offset is the
# whole point of using this instant rather than a round UTC number.
FAILURE = datetime(2026, 9, 16, 9, 0, 0)
APPROVER = "Grace Wanjiru"  # a named human; never an agent: or policy: principal
#: An instant comfortably inside the 24-hour window. Passed explicitly wherever a send is
#: meant to be on time: the reference failure instant is a fixed date, so the default
#: ``utcnow()`` would make every send in this file "late" and drag the s.43 reason rule
#: into tests that are not about it.
ON_TIME = FAILURE + timedelta(hours=23)

HUB_EVENT = {
    "source": "NMS",
    "site_id": "SFC-MTK-HUB-THK",
    "alarm_code": "POWER_FAIL",
    "message": "Thika hub on battery, mains down",
    "severity": "CRITICAL",
    "users_affected": 450000,
}


@pytest.fixture()
def on(monkeypatch):
    """``REGULATORY_ENABLED=true``. Everything except the flag-off tests needs it."""
    monkeypatch.setenv("REGULATORY_ENABLED", "true")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A live API on its own SQLite file, with the lane armed."""
    db = tmp_path / "regulatory.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
    monkeypatch.setenv("REGULATORY_ENABLED", "true")

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


def _open_incident(client: TestClient) -> str:
    r = client.post("/api/v1/events", json=HUB_EVENT)
    assert r.status_code == 200, r.text
    return r.json()["incident"]["id"]


def _incident(session, *, number: str = "INC000701", priority: str = "P1", **overrides) -> IncidentRow:
    values = dict(
        operator_id="safaricom",
        incident_number=number,
        status="IN_PROGRESS",
        priority=priority,
        users_affected=450000,
        site_id="SFC-MTK-HUB-THK",
        site_name="Thika Hub",
        site_type="HUB",
        region_code="MTK",
        county="Kiambu",
        correlation_fingerprint="fp-regulatory",
        failure_time=FAILURE,
    )
    values.update(overrides)
    inc = IncidentRow(**values)
    session.add(inc)
    session.flush()
    return inc


def _cfg():
    return get_settings().operator


def _approve(session, task: HitlTaskRow, *, by: str = APPROVER) -> HitlTaskRow:
    """What ``POST /api/v1/hitl/{id}/approve`` does for a task type main.py does not special-case:
    it records the decision and releases nothing."""
    task.status = "APPROVED"
    task.resolved_by = by
    task.resolved_at = utcnow()
    session.flush()
    return task


def _notice_with_open_card(session):
    inc = _incident(session)
    notice = open_notification(session, inc, _cfg())
    task = request_approval(session, notice, inc, _cfg())
    return inc, notice, task


# =======================================================================================
# M10 — 0 auto-sent. The most important test in this lane.
# =======================================================================================


def test_a_regulatory_draft_cannot_reach_the_outbox_dispatcher_without_a_named_human_approval(tmp_db, on):
    """Metric M10: "100 % drafted; **0 auto-sent**".

    Every route by which a drafted notice could otherwise escape is enumerated and refused.
    The assertion that matters most is the first: after drafting AND after raising the
    approval card, the outbox contains **nothing at all** for this notice, so there is no row
    for any generic release path, any dispatcher pass, or any mistake to promote.
    """
    _settings, session = tmp_db
    inc, notice, task = _notice_with_open_card(session)

    # 1. Drafted, card raised, deadline running — and not one outbox row exists.
    assert notice.status == PENDING_APPROVAL
    assert notice.draft_alert["body"], "the notice must be drafted (M10's numerator)"
    assert session.query(OutboxRow).count() == 0, "a regulator notice has ZERO outbox rows before approval"

    # 2. A full dispatcher pass transmits nothing, because there is nothing to claim.
    report = outbox.drain_once(session)
    assert (report.claimed, report.sent) == (0, 0)

    # 3. Releasing with the card still PENDING is refused.
    with pytest.raises(NoticeNotApproved, match="PENDING"):
        release_notice(session, notice, inc, actor=APPROVER)

    # 4. Releasing with no card at all is refused.
    orphan_inc = _incident(session, number="INC000702")
    orphan = open_notification(session, orphan_inc, _cfg())
    assert orphan.hitl_task_id is None
    with pytest.raises(NoticeNotApproved, match="no APPROVE_REGULATORY_NOTICE task"):
        release_notice(session, orphan, orphan_inc, actor=APPROVER)

    # 5. An APPROVED card of a DIFFERENT type on the same incident is not an approval of this.
    other = HitlTaskRow(
        incident_id=inc.id,
        task_type="APPROVE_BROADCAST",
        status="APPROVED",
        resolved_by=APPROVER,
        resolved_at=utcnow(),
        entity_type=NOTICE_ENTITY_TYPE,
        entity_id=notice.id,
    )
    session.add(other)
    session.flush()
    notice.hitl_task_id = other.id
    with pytest.raises(NoticeNotApproved, match="not an approval of this notice"):
        release_notice(session, notice, inc, actor=APPROVER)

    # 6. An APPROVED card of the right type pointed at a DIFFERENT notice is not an approval either.
    notice.hitl_task_id = task.id
    _approve(session, task)
    task.entity_id = orphan.id
    session.flush()
    with pytest.raises(NoticeNotApproved, match="not this notice"):
        release_notice(session, notice, inc, actor=APPROVER)
    task.entity_id = notice.id
    session.flush()

    # 7. An agent or the autonomy policy is not a named human.
    for machine in ("agent:RegulatoryNotificationAgent", "policy:L2_GUARDED"):
        task.resolved_by = machine
        session.flush()
        with pytest.raises(NoticeNotApproved, match="named human"):
            release_notice(session, notice, inc, actor=APPROVER)

    # 8. The person who raised the notice may not approve it (§6.5 raiser != approver).
    task.created_by = APPROVER
    task.resolved_by = APPROVER
    session.flush()
    with pytest.raises(NoticeNotApproved, match="may not approve"):
        release_notice(session, notice, inc, actor=APPROVER)

    # Through all eight refusals: still no outbox row, and the notice never moved off
    # PENDING_APPROVAL. A refused send changes nothing.
    assert session.query(OutboxRow).count() == 0
    assert notice.status == PENDING_APPROVAL
    assert notice.sent_at is None

    # 9. With a genuine approval by a named human who did not raise it, exactly one row is
    #    written — carrying that human's name and timestamp, and flagged as approval-gated so
    #    the dispatcher can check it a second time.
    task.created_by = REGULATORY_RAISER
    task.resolved_by = APPROVER
    session.flush()
    row = release_notice(session, notice, inc, actor="Duty Manager", now=ON_TIME)
    assert session.query(OutboxRow).count() == 1
    assert row.requires_hitl == 1
    assert row.approved_by == APPROVER
    assert row.approved_at is not None
    # QUEUED, not SENT: the row exists, nothing has been transmitted, and sent_at is NULL
    # until the dispatcher reports a real transmission (record_dispatch_outcome). The old
    # assertion here read ``notice.status == SENT`` at this point, which was the bug: it
    # pinned an enqueue as a send.
    assert notice.status == QUEUED and notice.approved_by == APPROVER
    assert notice.sent_at is None

    # 10. And the outbox's own gate is independent: strip the approval off the row and the
    #     dispatcher refuses it outright rather than transmitting.
    row.approved_at = None
    session.flush()
    assert outbox.dispatch(row).status == outbox.REJECTED_UNAPPROVED


def test_requesting_approval_queues_nothing_unlike_the_handover_gate(tmp_db, on):
    """``services/handover`` parks its mail in the outbox as HELD. A HELD row is one generic
    ``release_held`` away from PENDING, and that helper releases by *incident* — so approving
    an unrelated broadcast on the same incident would promote the regulator's mail. For a
    regulator channel the right number of rows before approval is zero."""
    _settings, session = tmp_db
    inc, notice, _task = _notice_with_open_card(session)

    assert session.query(OutboxRow).count() == 0
    released = outbox.release_held(
        session, incident_id=inc.id, alert_id="whatever", approved_by="someone else", approved_at=utcnow()
    )
    assert released == 0
    assert session.query(OutboxRow).count() == 0


def test_a_second_send_of_the_same_notice_is_refused_rather_than_queued_twice(tmp_db, on):
    """Two written notifications for one incident is its own kind of regulatory mess."""
    _settings, session = tmp_db
    inc, notice, task = _notice_with_open_card(session)
    _approve(session, task)
    release_notice(session, notice, inc, actor="Duty Manager", now=ON_TIME)

    # The guard is unchanged in force, only in wording: it now refuses the QUEUED notice
    # (nothing transmitted yet) rather than a notice that had prematurely been called SENT.
    with pytest.raises(NoticeStateError, match="already been released to the outbox"):
        release_notice(session, notice, inc, actor="Duty Manager", now=ON_TIME)
    assert session.query(OutboxRow).count() == 1


def test_a_not_required_notice_can_never_be_sent(tmp_db, on):
    _settings, session = tmp_db
    inc = _incident(session, number="INC000703", priority="P4", users_affected=10, site_type="BTS")
    notice = open_notification(session, inc, _cfg())

    assert notice.status == NOT_REQUIRED
    with pytest.raises(NoticeStateError, match="NOT_REQUIRED"):
        release_notice(session, notice, inc, actor=APPROVER)


def test_approval_of_refuses_a_task_belonging_to_another_operator(tmp_db, on):
    """Every read goes through ``_owned``; another operator's approval is not visible, so it
    cannot authorise anything here."""
    _settings, session = tmp_db
    inc, notice, _task = _notice_with_open_card(session)
    theirs = _incident(session, number="ATL-000001", operator_id="airtel")
    their_task = HitlTaskRow(
        incident_id=theirs.id,
        task_type=REGULATORY_TASK_TYPE,
        status="APPROVED",
        resolved_by="Their Manager",
        resolved_at=utcnow(),
        entity_type=NOTICE_ENTITY_TYPE,
        entity_id=notice.id,
    )
    session.add(their_task)
    session.flush()
    notice.hitl_task_id = their_task.id

    with pytest.raises(NoticeNotApproved, match="not visible to this operator"):
        approval_of(session, notice)


# =======================================================================================
# The clock starts at failure_time
# =======================================================================================


def test_the_ca_clock_is_failure_time_plus_24_hours(tmp_db, on):
    """§5.3.20 acceptance: "P1 -> row with ``due_at = failure_time + 24h``"."""
    _settings, session = tmp_db
    inc = _incident(session)

    notice = open_notification(session, inc, _cfg())

    assert notice.kind == "CA_OUTAGE_24H"
    assert notice.clock_started_at == FAILURE
    assert notice.due_at == FAILURE + timedelta(hours=24)
    assert notice.status == DRAFT


def test_a_notification_created_late_keeps_the_original_deadline(tmp_db, on):
    """The whole reason ``reason_for_delay`` exists. A row opened 20 hours after the failure
    has 4 hours left, not 24 — the clock is not restarted by noticing."""
    _settings, session = tmp_db
    late_now = FAILURE + timedelta(hours=20)
    inc = _incident(session, created_at=late_now)

    notice = open_notification(session, inc, _cfg())
    block = countdown(notice, now=late_now)

    assert notice.clock_started_at == FAILURE  # not created_at
    assert notice.due_at == FAILURE + timedelta(hours=24)
    assert block["minutes_remaining"] == 4 * 60
    assert notice.created_at != notice.clock_started_at


def test_a_required_notice_refuses_to_start_its_clock_from_row_creation(tmp_db, on):
    """Falling back to ``created_at`` would invent a deadline later than the real one and
    quietly turn a missed statutory obligation into an apparently met one."""
    _settings, session = tmp_db
    inc = _incident(session, failure_time=None, outage_start_at=None)

    with pytest.raises(ClockStartUnknown, match="never at row creation"):
        open_notification(session, inc, _cfg())
    assert session.query(RegulatoryNotificationRow).count() == 0


def test_outage_start_at_is_accepted_as_the_documented_synonym_of_failure_time(tmp_db, on):
    _settings, session = tmp_db
    inc = _incident(session, failure_time=None, outage_start_at=FAILURE)

    notice = open_notification(session, inc, _cfg())

    assert notice.clock_started_at == FAILURE
    assert notice.significance["clock_start_source"] == "outage_start_at"


def test_a_kind_with_no_configured_deadline_is_refused_rather_than_given_an_invented_one(tmp_db, on):
    """``CBK_FACTSHEET`` is a kind in §7.6.1 with no entry in ``regulatory.deadlines_hours``.
    Making one up would be this system inventing a statutory deadline."""
    _settings, session = tmp_db
    inc = _incident(session)

    with pytest.raises(ValueError, match="no deadline"):
        open_notification(session, inc, _cfg(), kind="CBK_FACTSHEET")


def test_an_unknown_kind_is_refused(tmp_db, on):
    _settings, session = tmp_db
    inc = _incident(session)
    with pytest.raises(ValueError, match="unknown regulatory kind"):
        open_notification(session, inc, _cfg(), kind="CA_OUTAGE_12H")


def test_the_odpc_breach_clock_is_seventy_two_hours(tmp_db, on):
    """DPA 2019 s.43: "without delay, within seventy-two hours of becoming aware"."""
    _settings, session = tmp_db
    inc = _incident(session)

    notice = open_notification(session, inc, _cfg(), kind="ODPC_BREACH_72H")

    assert notice.due_at == FAILURE + timedelta(hours=72)


def test_opening_the_same_clock_twice_returns_the_first_row_and_never_restarts_it(tmp_db, on):
    _settings, session = tmp_db
    inc = _incident(session)

    first = open_notification(session, inc, _cfg())
    first.due_at = FAILURE + timedelta(hours=24)
    second = open_notification(session, inc, _cfg())

    assert second.id == first.id
    assert session.query(RegulatoryNotificationRow).count() == 1


# =======================================================================================
# Three hours: EAT is a display concern, never a storage one
# =======================================================================================


def test_the_deadline_is_stored_naive_utc_and_only_rendered_in_eat(tmp_db, on):
    """The bug this exists to prevent: converting ``failure_time`` to EAT and storing the
    result, which moves a 24-hour statutory deadline three hours in the wrong direction."""
    _settings, session = tmp_db
    inc = _incident(session)

    notice = open_notification(session, inc, _cfg())

    assert notice.due_at.tzinfo is None  # the storage contract: naive UTC
    assert notice.due_at == datetime(2026, 9, 17, 9, 0, 0)  # exactly +24 h on the stored value
    # and what a Nairobi operator reads is the same instant, three hours on the clock face
    assert fmt_eat(notice.clock_started_at, "%Y-%m-%d %H:%M") == "2026-09-16 12:00 EAT"
    assert fmt_eat(notice.due_at, "%Y-%m-%d %H:%M") == "2026-09-17 12:00 EAT"


def test_due_at_for_is_pure_timedelta_arithmetic(tmp_db):
    """Kenya has no DST, so EAT is UTC+3 always and "24 hours later" is the same instant
    computed in either zone. The helper must therefore never touch a timezone."""
    assert due_at_for(FAILURE, 24) == FAILURE + timedelta(hours=24)
    assert due_at_for(FAILURE, 72) == FAILURE + timedelta(hours=72)
    # a failure at 22:00 UTC is already tomorrow in Nairobi; the deadline is still +24 h
    late = datetime(2026, 9, 16, 22, 0)
    assert due_at_for(late, 24) == datetime(2026, 9, 17, 22, 0)


def test_the_countdown_carries_both_spellings_of_the_deadline(tmp_db, on):
    """A naive ISO string is read by a browser as local time; in Nairobi that understates the
    remaining hours by three (defect #41)."""
    _settings, session = tmp_db
    inc = _incident(session)
    notice = open_notification(session, inc, _cfg())

    block = countdown(notice, now=FAILURE + timedelta(hours=1))

    assert block["due_at"].isoformat().endswith("Z")
    assert block["due_at_eat"] == "2026-09-17 12:00 EAT"
    assert block["minutes_remaining"] == 23 * 60
    assert block["overdue"] is False


def test_the_countdown_goes_negative_rather_than_clamping_at_zero(tmp_db, on):
    """"Three hours late" and "on the deadline" are different operational facts."""
    _settings, session = tmp_db
    inc = _incident(session)
    notice = open_notification(session, inc, _cfg())

    block = countdown(notice, now=notice.due_at + timedelta(hours=3))

    assert block["minutes_remaining"] == -180
    assert block["overdue"] is True


# =======================================================================================
# Significance (D15) — the rule proposes, a human decides
# =======================================================================================


def test_a_p1_is_significant_and_the_card_records_which_rule_decided(tmp_db, on):
    _settings, session = tmp_db
    inc = _incident(session)

    verdict = evaluate_significance(inc, _cfg(), session=session)

    assert verdict.significant is True
    assert verdict.rule_matched == "priority=P1"
    assert verdict.yaml_path == "regulatory.significance"


def test_a_p4_on_a_small_site_produces_a_not_required_row_rather_than_no_row_at_all(tmp_db, on):
    """§7.6.8: "P4 -> NOT_REQUIRED". "We considered it and it is not notifiable" has to be
    distinguishable from "nobody looked"."""
    _settings, session = tmp_db
    inc = _incident(session, number="INC000704", priority="P4", users_affected=500, site_type="BTS")

    notice = open_notification(session, inc, _cfg())

    assert notice.status == NOT_REQUIRED
    assert notice.significance["significant"] is False
    assert notice.significance["rule_matched"] is None
    assert notice.draft_alert is None  # nothing is drafted for a notice that is not required


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"priority": "P2", "site_type": "CORE", "users_affected": 10}, "site_type=CORE"),
        ({"priority": "P2", "site_type": "BTS", "users_affected": 250000}, "users_affected>=100000"),
    ],
)
def test_each_d15_rule_can_make_an_incident_significant_on_its_own(tmp_db, on, overrides, expected):
    _settings, session = tmp_db
    inc = _incident(session, number=f"INC0007{abs(hash(expected)) % 90 + 10}", **overrides)

    verdict = evaluate_significance(inc, _cfg(), session=session)

    assert verdict.significant is True
    assert verdict.rule_matched == expected


def test_a_rule_that_could_not_be_checked_is_recorded_as_unevaluated_not_as_failed(tmp_db, on):
    """``None`` in ``checks`` means "not evaluated". Recording it as False would claim the
    multi-region test was run and came back negative."""
    _settings, session = tmp_db
    inc = _incident(session, priority="P4", site_type="BTS", users_affected=5)

    without = evaluate_significance(inc, _cfg())  # no session -> multi-region not evaluable
    with_session = evaluate_significance(inc, _cfg(), session=session)

    assert without.checks["multi_region"] is None
    assert with_session.checks["multi_region"] is False


def test_the_significance_verdict_is_frozen_because_it_is_evidence(tmp_db, on):
    """§5.3.20 makes "significant" an A2 decision: the rule proposes, a human decides."""
    _settings, session = tmp_db
    inc = _incident(session)
    verdict = evaluate_significance(inc, _cfg(), session=session)

    with pytest.raises(FrozenInstanceError):
        verdict.significant = False  # type: ignore[misc]


# =======================================================================================
# The countdown fires once per threshold, not once per tick
# =======================================================================================


def test_the_countdown_fires_at_twelve_and_two_hours_remaining(tmp_db, on):
    """§5.3.20: WS ``regulatory.deadline`` at 12 h / 2 h remaining."""
    _settings, session = tmp_db
    inc = _incident(session)
    notice = open_notification(session, inc, _cfg())

    # nothing yet at 20 hours remaining
    assert sweep_deadlines(session, _cfg(), now=notice.due_at - timedelta(hours=20)).fired == []
    assert pending_events(session) == []

    twelve = sweep_deadlines(session, _cfg(), now=notice.due_at - timedelta(hours=11, minutes=59))
    assert [f["threshold_hours"] for f in twelve.fired] == [12]
    events = pending_events(session)
    assert len(events) == 1 and events[0].type == DEADLINE_EVENT
    assert events[0].payload["threshold_hours"] == 12
    assert events[0].payload["kind"] == "CA_OUTAGE_24H"
    assert events[0].payload["due_at"].endswith("Z")
    assert events[0].payload["due_at_eat"].endswith("EAT")

    two = sweep_deadlines(session, _cfg(), now=notice.due_at - timedelta(hours=1, minutes=59))
    assert [f["threshold_hours"] for f in two.fired] == [2]
    assert len(pending_events(session)) == 2


def test_the_countdown_does_not_fire_again_on_the_next_scheduler_tick(tmp_db, on):
    """The sweep runs every five minutes. Without the fired-threshold mark the wallboard and
    every subscriber would get the same "12 hours remaining" alert 144 times."""
    _settings, session = tmp_db
    inc = _incident(session)
    notice = open_notification(session, inc, _cfg())
    now = notice.due_at - timedelta(hours=11)

    first = sweep_deadlines(session, _cfg(), now=now)
    second = sweep_deadlines(session, _cfg(), now=now + timedelta(minutes=5))
    third = sweep_deadlines(session, _cfg(), now=now + timedelta(minutes=10))

    assert len(first.fired) == 1
    assert second.fired == [] and third.fired == []
    assert len(pending_events(session)) == 1
    assert notice.significance["countdown_fired"] == [12]


def test_a_first_sweep_that_finds_one_hour_left_announces_two_hours_not_twelve(tmp_db, on):
    """Both thresholds are crossed, so both are marked; announcing "12 hours remaining" to a
    wallboard that has one is worse than announcing nothing."""
    _settings, session = tmp_db
    inc = _incident(session)
    notice = open_notification(session, inc, _cfg())

    report = sweep_deadlines(session, _cfg(), now=notice.due_at - timedelta(hours=1))

    assert [f["threshold_hours"] for f in report.fired] == [2]
    assert len(pending_events(session)) == 1
    assert sorted(notice.significance["countdown_fired"]) == sorted(COUNTDOWN_THRESHOLDS_H)


def test_a_released_notice_stops_counting_down(tmp_db, on):
    """Renamed from ``..._a_sent_notice_...``: after release the notice is QUEUED, not SENT.
    The behaviour it pins is unchanged — a notice that has left the approval queue is off the
    countdown, because the countdown exists to chase the human, and the human has acted."""
    _settings, session = tmp_db
    inc, notice, task = _notice_with_open_card(session)
    _approve(session, task)
    release_notice(session, notice, inc, actor="Duty Manager", now=ON_TIME)

    report = sweep_deadlines(session, _cfg(), now=notice.due_at - timedelta(hours=1))

    assert report.checked == 0 and report.fired == []


def test_an_overdue_notice_is_counted_so_it_can_be_shown_as_overdue(tmp_db, on):
    _settings, session = tmp_db
    inc = _incident(session)
    notice = open_notification(session, inc, _cfg())

    report = sweep_deadlines(session, _cfg(), now=notice.due_at + timedelta(hours=2))

    assert report.overdue == 1
    assert pending_events(session)[0].payload["overdue"] is True


# =======================================================================================
# DPA 2019 s.43: a late notification carries its reasons
# =======================================================================================


def test_a_late_send_without_a_reason_for_delay_is_refused(tmp_db, on):
    """§9.2, the s.43 row: "a notification made after 72 h must carry the reasons for the
    delay". The reason is the disclosure the statute asks for, not metadata."""
    _settings, session = tmp_db
    inc, notice, task = _notice_with_open_card(session)
    _approve(session, task)
    late = notice.due_at + timedelta(hours=3)

    with pytest.raises(NoticeStateError, match="reason_for_delay"):
        release_notice(session, notice, inc, actor="Duty Manager", now=late)
    assert session.query(OutboxRow).count() == 0
    assert notice.status == PENDING_APPROVAL


def test_a_late_send_with_a_reason_records_it_where_the_spec_says(tmp_db, on):
    """§7.6.1 / §9.2: ``regulatory_notifications.significance_json`` records
    ``reason_for_delay`` when ``sent_at > due_at``."""
    _settings, session = tmp_db
    inc, notice, task = _notice_with_open_card(session)
    _approve(session, task)
    late = notice.due_at + timedelta(hours=3)

    release_notice(
        session,
        notice,
        inc,
        actor="Duty Manager",
        reason_for_delay="site inaccessible during curfew; failure time confirmed only on restoration",
        now=late,
    )

    # The reason is recorded at release, which is where the refusal that demands it lives.
    # ``sent_at`` is NOT stamped here any more — this release transmitted nothing — so the
    # assertion that used to read ``notice.sent_at == late`` now states the new rule: the
    # lateness of the ENQUEUE is on the release record, and sent_at stays NULL until a
    # transmission happens. ``test_regulatory_dispatch_outcome`` carries it the rest of the way.
    assert notice.status == QUEUED
    assert notice.sent_at is None
    assert "curfew" in notice.significance["reason_for_delay"]
    assert notice.significance["release"]["late"] is True


def test_an_on_time_send_needs_no_reason_and_records_none(tmp_db, on):
    _settings, session = tmp_db
    inc, notice, task = _notice_with_open_card(session)
    _approve(session, task)

    release_notice(session, notice, inc, actor="Duty Manager", now=notice.due_at - timedelta(hours=1))

    assert "reason_for_delay" not in notice.significance
    assert notice.significance["release"]["late"] is False


# =======================================================================================
# The flag: off means off
# =======================================================================================


def test_the_lane_is_off_by_default(monkeypatch):
    """§7.6 / Appendix B: ``REGULATORY_ENABLED`` defaults to false."""
    monkeypatch.delenv("REGULATORY_ENABLED", raising=False)
    assert regulatory_enabled() is False
    monkeypatch.setenv("REGULATORY_ENABLED", "false")
    assert regulatory_enabled() is False
    monkeypatch.setenv("REGULATORY_ENABLED", "true")
    assert regulatory_enabled() is True


def test_with_the_flag_off_the_agent_entry_point_writes_nothing_at_all(tmp_db, monkeypatch):
    """Not even a NOT_REQUIRED row: §7.6's exit criteria require the flag-off system to behave
    exactly as it does today, and a new row on every ticket is not that."""
    _settings, session = tmp_db
    monkeypatch.delenv("REGULATORY_ENABLED", raising=False)
    inc = _incident(session)

    assert evaluate_and_open(session, inc, _cfg()) is None
    assert session.query(RegulatoryNotificationRow).count() == 0
    assert pending_events(session) == []


def test_with_the_flag_off_the_sweep_publishes_nothing(tmp_db, monkeypatch):
    _settings, session = tmp_db
    monkeypatch.setenv("REGULATORY_ENABLED", "true")
    inc = _incident(session)
    notice = open_notification(session, inc, _cfg())
    monkeypatch.delenv("REGULATORY_ENABLED", raising=False)

    report = sweep_deadlines(session, _cfg(), now=notice.due_at - timedelta(minutes=30))

    assert report.enabled is False
    assert report.checked == 0 and report.fired == []
    assert pending_events(session) == []


# =======================================================================================
# The draft, the card and the envelope
# =======================================================================================


def test_the_regulator_envelope_can_never_be_built_in_an_auto_send_shape(tmp_db, on):
    """``scope="RESTRICTED"`` and a ``REGULATOR`` audience each independently force
    ``governance.requires_hitl=True`` in ``services/alerts.build_alert``. Going through the
    envelope rather than around it buys that guarantee for free."""
    _settings, session = tmp_db
    inc = _incident(session)
    notice = open_notification(session, inc, _cfg())

    envelope = notice.draft_alert["envelope"]
    assert envelope is not None, "the safaricom profile's incident numbers fit §6.1"
    assert envelope["scope"] == "RESTRICTED"
    assert [a["audience"] for a in envelope["audiences"]] == ["REGULATOR"]
    assert envelope["governance"]["requires_hitl"] is True
    assert envelope["governance"]["approved_by"] is None


def test_the_draft_names_the_deadline_in_eat_and_says_it_is_not_sent(tmp_db, on):
    _settings, session = tmp_db
    inc = _incident(session)
    notice = open_notification(session, inc, _cfg())

    body = notice.draft_alert["body"]

    assert "2026-09-16 12:00 EAT" in body  # the interruption
    assert "2026-09-17 12:00 EAT" in body  # the deadline
    assert "DRAFT — not sent" in body
    assert "UNVERIFIED" in body  # the Condition 9 caveat travels with the text
    assert notice.draft_alert["ai_assisted"] is False


def test_recipients_are_a_config_path_never_an_address(tmp_db, on):
    """§6.1: the envelope carries a recipient ref; the mailbox is resolved at dispatch."""
    _settings, session = tmp_db
    inc = _incident(session)
    notice = open_notification(session, inc, _cfg())

    assert notice.draft_alert["recipients_ref"] == "regulatory.recipients.CA"
    assert "@" not in notice.draft_alert["recipients_ref"]


def test_the_card_shows_the_evidence_pack_hash_the_notice_was_built_from(tmp_db, on):
    """So the approver can quote it later and prove they were shown the same bytes."""
    _settings, session = tmp_db
    inc, notice, task = _notice_with_open_card(session)

    payload = task.proposed_payload

    assert notice.evidence_pack_id is not None
    assert payload["evidence_pack_id"] == notice.evidence_pack_id
    assert len(payload["evidence_pack_sha256"]) == 64
    assert payload["countdown"]["due_at_eat"].endswith("EAT")
    assert "regulator" in payload["warning"].lower()


def test_the_card_is_raised_by_an_agent_principal_so_any_human_may_approve_it(tmp_db, on):
    """§6.5 raiser != approver must never lock a statutory notice out of approval."""
    _settings, session = tmp_db
    _inc, notice, task = _notice_with_open_card(session)

    assert task.created_by == REGULATORY_RAISER
    assert task.created_by.startswith("agent:")
    assert task.task_type == REGULATORY_TASK_TYPE
    assert task.entity_type == NOTICE_ENTITY_TYPE and task.entity_id == notice.id
    assert notice.status == PENDING_APPROVAL


def test_asking_for_approval_twice_reuses_the_open_card(tmp_db, on):
    """Two open cards for one notice is two approvals for one send."""
    _settings, session = tmp_db
    inc, notice, first = _notice_with_open_card(session)

    second = request_approval(session, notice, inc, _cfg())

    assert second.id == first.id
    assert session.query(HitlTaskRow).filter(HitlTaskRow.entity_id == notice.id).count() == 1


# =======================================================================================
# The routes
# =======================================================================================


def test_the_workspace_panel_is_inert_and_opens_no_clock(client):
    """Opening a clock is an A2 decision; it must not be a side effect of rendering a page."""
    inc_id = _open_incident(client)

    r = client.get(f"/api/v1/incidents/{inc_id}/regulatory")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is True
    assert body["notifications"] == []
    assert body["significance"]["rule_matched"]  # the verdict is shown without acting on it
    assert body["kinds"] == list(REGULATORY_KINDS)


def test_the_send_route_is_403_without_an_approval(client):
    """The caller is being told they may not send, which is exactly what has happened."""
    inc_id = _open_incident(client)
    opened = client.post(f"/api/v1/incidents/{inc_id}/regulatory", json={"requested_by": "Alice Supervisor"})
    assert opened.status_code == 200, opened.text
    notice_id = opened.json()["id"]
    raised = client.post(f"/api/v1/regulatory/{notice_id}/request-approval", json={"requested_by": "Alice Supervisor"})
    assert raised.status_code == 200, raised.text

    r = client.post(f"/api/v1/regulatory/{notice_id}/send", json={"sent_by": "Bob Duty Manager"})

    assert r.status_code == 403, r.text
    assert "APPROVED" in r.json()["detail"]


def test_the_full_route_path_drafts_approves_and_only_then_sends(client):
    """The two acts stay separate: ``POST /hitl/{id}/approve`` records a named human's
    decision and releases nothing; ``POST /regulatory/{id}/send`` is the separate,
    separately-authorised act that puts the row in the outbox."""
    inc_id = _open_incident(client)
    notice_id = client.post(
        f"/api/v1/incidents/{inc_id}/regulatory", json={"requested_by": "Alice Supervisor"}
    ).json()["id"]
    task_id = client.post(
        f"/api/v1/regulatory/{notice_id}/request-approval", json={"requested_by": "Alice Supervisor"}
    ).json()["task_id"]

    approved = client.post(f"/api/v1/hitl/{task_id}/approve", json={"resolved_by": "Grace Duty Manager"})
    assert approved.status_code == 200, approved.text

    sent = client.post(f"/api/v1/regulatory/{notice_id}/send", json={"sent_by": "Grace Duty Manager"})

    assert sent.status_code == 200, sent.text
    # QUEUED, and the response says so: the route put a row on the outbox and transmitted
    # nothing, so neither the status nor sent_at may claim the Authority has been notified.
    assert sent.json()["notification"]["status"] == QUEUED
    assert sent.json()["notification"]["sent_at"] is None
    assert sent.json()["notification"]["approved_by"] == "Grace Duty Manager"
    assert sent.json()["outbox_status"] == "PENDING"  # queued, not transmitted inside the request


def test_the_supervisor_who_raised_the_notice_cannot_approve_it(client):
    """§6.5, through the real approve route."""
    inc_id = _open_incident(client)
    notice_id = client.post(
        f"/api/v1/incidents/{inc_id}/regulatory", json={"requested_by": "Alice Supervisor"}
    ).json()["id"]
    task_id = client.post(
        f"/api/v1/regulatory/{notice_id}/request-approval", json={"requested_by": "Alice Supervisor"}
    ).json()["task_id"]

    r = client.post(f"/api/v1/hitl/{task_id}/approve", json={"resolved_by": "Alice Supervisor"})

    assert r.status_code == 403, r.text


def test_a_released_notice_cannot_be_redrafted(client):
    """A released notice keeps the wording that went to the outbox; rewriting it would destroy
    the only record of what the regulator was (or was about to be) told. ``is_open`` treats
    QUEUED exactly as it treated SENT, so this 409 is unchanged by the status split."""
    inc_id = _open_incident(client)
    notice_id = client.post(
        f"/api/v1/incidents/{inc_id}/regulatory", json={"requested_by": "Alice Supervisor"}
    ).json()["id"]
    task_id = client.post(
        f"/api/v1/regulatory/{notice_id}/request-approval", json={"requested_by": "Alice Supervisor"}
    ).json()["task_id"]
    client.post(f"/api/v1/hitl/{task_id}/approve", json={"resolved_by": "Grace Duty Manager"})
    client.post(f"/api/v1/regulatory/{notice_id}/send", json={"sent_by": "Grace Duty Manager"})

    r = client.post(f"/api/v1/regulatory/{notice_id}/draft")

    assert r.status_code == 409, r.text


def test_write_routes_are_503_while_the_lane_is_off(client, monkeypatch):
    """Not 404 (the route exists) and not 403 (the caller is fine): the feature is not
    available on this deployment."""
    inc_id = _open_incident(client)
    monkeypatch.delenv("REGULATORY_ENABLED", raising=False)

    r = client.post(f"/api/v1/incidents/{inc_id}/regulatory", json={})

    assert r.status_code == 503, r.text
    assert "REGULATORY_ENABLED" in r.json()["detail"]


def test_the_read_route_with_the_lane_off_says_so_instead_of_looking_clean(client, monkeypatch):
    """An empty list because the flag is off must not read as "this outage has no regulatory
    obligation" — the same honesty rule as the wallboard's STALE badges (§7.10)."""
    inc_id = _open_incident(client)
    monkeypatch.delenv("REGULATORY_ENABLED", raising=False)

    body = client.get(f"/api/v1/incidents/{inc_id}/regulatory").json()

    assert body["enabled"] is False
    assert body["degraded"] is True
    assert body["notifications"] == []
    assert body["significance"] is None


def test_another_operators_notification_is_404_not_403(client):
    """404 is indistinguishable from "no such id"; a 403 would confirm the id exists in
    another operator's data, which is the fact the scoping protects."""
    r = client.post("/api/v1/regulatory/not-a-real-id/send", json={})
    assert r.status_code == 404


# =======================================================================================
# The one thing this lane cannot do for itself
# =======================================================================================


@pytest.mark.skipif(
    not hasattr(HitlTaskType, "APPROVE_REGULATORY_NOTICE"),
    reason=(
        "domain/enums.py belongs to no lane and must be edited once, by hand: add "
        "APPROVE_REGULATORY_NOTICE = \"APPROVE_REGULATORY_NOTICE\" to HitlTaskType. "
        "The gate itself does not depend on it — hitl_tasks.task_type is a plain string "
        "column and services/regulatory falls back to the same literal — so this test is "
        "skipped rather than failed until the member lands."
    ),
)
def test_the_hitl_task_type_enum_carries_the_regulatory_card():
    """Once the member exists, the enum and the service must agree on one spelling."""
    assert HitlTaskType.APPROVE_REGULATORY_NOTICE.value == REGULATORY_TASK_TYPE
    assert REGULATORY_TASK_TYPE == "APPROVE_REGULATORY_NOTICE"
