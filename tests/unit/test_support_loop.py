"""Close the loop, the service level (docs/CLOSE_THE_LOOP.md): who is told when an incident is
restored, when the message waits for a person, the card's two outcomes, the still-down rules, the
surge rules and the loop numbers -- on a real file-backed database, without HTTP. The routes, the
NOC hooks and the races are in ``tests/system/test_support_loop_api.py``.

Each test is written to fail when the rule it names is broken: an inferred restore that tells
someone, a P2 at L2 that sends, a batch over 20 that sends, a second SMS to one number, a second
send on a second restore, a reject that tells, another operator's customer touched.
"""

from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from noc_agents.config import get_settings
from noc_agents.db.models import Base, HitlTaskRow, IncidentRow, OutboxRow, WorkNoteRow
from noc_agents.db.models_support import (
    SupportComplaintRow,
    SupportMessageRow,
    SupportNoticeRow,
    SupportStepRow,
    SupportSurgeMemberRow,
    SupportSurgeRow,
)
from noc_agents.orchestrator import outbox
from noc_agents.services.gsm7 import segments_for
from noc_agents.services.hitl import suppress_held_outbox
from noc_agents.support import loop, surge
from noc_agents.support.context import default_context
from noc_agents.support.policy import load_policy
from noc_agents.support.text import mask_msisdn

OP = "safaricom"
CTX = default_context()
T0 = datetime(2026, 10, 5, 13, 40)  # naive UTC: 16:40 in Nairobi
_seq = itertools.count(1)


@pytest.fixture()
def session(tmp_path):
    from noc_agents.db import models_all  # noqa: F401  (every table on Base)

    engine = create_engine(f"sqlite:///{(tmp_path / 'loop.db').as_posix()}",
                           connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, future=True)()
    yield s
    s.close()
    engine.dispose()


def settings_at(level: str):
    settings = get_settings().model_copy(deep=True)
    settings.operator.autonomy_level = level
    return settings


def incident(session, *, priority="P3", region="NBI_E", site_name="Embakasi East Aggregation HUB",
             status="IN_PROGRESS", operator=OP, county="Nairobi") -> IncidentRow:
    n = next(_seq)
    inc = IncidentRow(operator_id=operator, incident_number=f"INC{n:06d}", status=status, priority=priority,
                      site_id=f"SITE-{n}", site_name=site_name, region_code=region, county=county,
                      correlation_fingerprint=f"fp-{n}", created_at=T0 - timedelta(hours=2))
    session.add(inc)
    session.flush()
    return inc


def msisdn(n: int) -> str:
    return f"+2547000{n:05d}"


def complaint(session, inc: IncidentRow | None, number: str, *, language="en", place: str | None = "kayole",
              status="action_taken", created_at: datetime | None = None, operator=OP, category="network",
              body="No network in Kayole since morning.") -> SupportComplaintRow:
    n = next(_seq)
    row = SupportComplaintRow(
        operator_id=operator, ref=f"CMP-{n:06d}", msisdn=number, msisdn_masked=mask_msisdn(number), language=language,
        body=body, body_hash=f"h{n}", category=category, status=status, place=place,
        linked_incident_id=inc.id if inc is not None else None,
        created_at=created_at or T0 - timedelta(minutes=90 - n % 60), updated_at=T0 - timedelta(minutes=80),
        sla_due_at=T0 + timedelta(hours=4),
    )
    session.add(row)
    session.flush()
    return row


def restore(session, inc: IncidentRow, *, source="SUPERVISOR", level="L2_GUARDED", at=T0, policy=None):
    inc.status, inc.restored_source, inc.restored_at = "RESTORED", source, at
    session.flush()
    notice = loop.on_incident_restored(session, inc, trigger="restore", settings=settings_at(level), policy=policy,
                                       now=at + timedelta(minutes=1))
    session.commit()
    return notice


def sms(session) -> list[OutboxRow]:
    return list(session.scalars(select(OutboxRow).where(OutboxRow.kind == "SMS").order_by(OutboxRow.created_at)).all())


def cards(session, task_type=loop.CUSTOMER_UPDATE_TASK_TYPE) -> list[HitlTaskRow]:
    return list(session.scalars(select(HitlTaskRow).where(HitlTaskRow.task_type == task_type)).all())


def steps(session, row: SupportComplaintRow, action: str | None = None) -> list[SupportStepRow]:
    stmt = select(SupportStepRow).where(SupportStepRow.complaint_id == row.id)
    if action:
        stmt = stmt.where(SupportStepRow.action == action)
    return list(session.scalars(stmt.order_by(SupportStepRow.seq)).all())


# ======================================================================== 1. who is told


def test_an_inferred_restore_tells_nobody(session):
    inc = incident(session)
    row = complaint(session, inc, msisdn(1))
    assert restore(session, inc, source="VENDOR_NOTE_INFERRED", level="L3_CONDITIONAL") is None
    session.refresh(row)
    assert sms(session) == [] and cards(session) == []
    assert row.status == "action_taken" and row.told_restored_at is None and steps(session, row) == []


@pytest.mark.parametrize("source", ["SUPERVISOR", "MARK_RESTORED", "ALARM_CLEAR"])
def test_a_confirmed_restore_tells_each_customer_once_with_the_exact_message(session, source):
    inc = incident(session, priority="P3")
    row = complaint(session, inc, msisdn(2), place="kayole")
    notice = restore(session, inc, source=source)
    session.refresh(row)
    text = f"Service is back in Kayole. Your complaint {row.ref} is now closed. Still down? Tell us at http://127.0.0.1:8000/track?ref={row.ref}"
    assert notice.state == "sent" and notice.restore_source == source and notice.recipients == 1
    [out] = sms(session)
    assert out.status == outbox.PENDING and out.requires_hitl == 0 and out.incident_id is None
    # One key per number per notice ATTEMPT (7.1), the number as a keyed HMAC, never in clear.
    assert out.idempotency_key == f"support-restore:{notice.id}:{loop.msisdn_hash(row.msisdn)}"
    payload = json.loads(out.payload_json)
    assert payload["body"] == text and payload["complaint_ref"] == row.ref and payload["segments"] == 1
    assert row.msisdn not in out.payload_json and row.msisdn not in out.idempotency_key  # the number never enters the outbox
    assert (row.status, row.closure_reason, row.told_incident_id) == ("closed", "service_restored", inc.id)
    assert row.told_restored_at == T0 + timedelta(minutes=1)
    [message] = session.scalars(select(SupportMessageRow).where(SupportMessageRow.complaint_id == row.id)).all()
    assert (message.author, message.channel, message.body) == ("agent", "sms", text)
    [step] = steps(session, row)
    assert (step.agent, step.action) == ("followup", "told_restored")
    assert step.summary == f"Told the customer service is back in Kayole ({inc.incident_number} restored 16:40)"


def test_one_sms_per_number_however_often_it_complained_and_resolved_cases_too(session):
    inc = incident(session)
    a1 = complaint(session, inc, msisdn(3), created_at=T0 - timedelta(minutes=50))
    a2 = complaint(session, inc, msisdn(3), created_at=T0 - timedelta(minutes=30), status="resolved")
    a3 = complaint(session, inc, msisdn(3), created_at=T0 - timedelta(minutes=10), place="umoja")
    b = complaint(session, inc, msisdn(4), status="resolved")
    restore(session, inc)
    rows = sms(session)
    assert len(rows) == 2
    by_ref = {json.loads(r.payload_json)["complaint_ref"]: r for r in rows}
    assert set(by_ref) == {a3.ref, b.ref}  # the newest complaint of each number is the one the SMS names
    assert json.loads(by_ref[a3.ref].payload_json)["complaint_ids"] == [a3.id, a2.id, a1.id]
    for row in (a1, a2, a3, b):
        session.refresh(row)
        assert row.status == "closed" and row.told_incident_id == inc.id, row.ref
    assert "Service is back in Umoja" in json.loads(by_ref[a3.ref].payload_json)["body"]


def test_only_this_operators_complaints_about_this_incident_are_told(session):
    inc = incident(session)
    other = incident(session, site_name="Thika Mt Kenya HUB", region="MTK")
    mine = complaint(session, inc, msisdn(5))
    theirs = complaint(session, inc, msisdn(6), operator="airtel")  # another operator's row naming the same id
    elsewhere = complaint(session, other, msisdn(7))
    unlinked = complaint(session, None, msisdn(8))
    restore(session, inc)
    assert len(sms(session)) == 1
    for row in (theirs, elsewhere, unlinked):
        session.refresh(row)
        assert row.told_restored_at is None and row.status == "action_taken", row.ref
    session.refresh(mine)
    assert mine.status == "closed"


def test_language_and_place_follow_the_customer_and_fit_one_segment(session):
    inc = incident(session, region="NBI_E")
    sw = complaint(session, inc, msisdn(9), language="sw", place="kayole")
    mixed = complaint(session, inc, msisdn(10), language="mixed", place=None)
    restore(session, inc)
    bodies = {json.loads(r.payload_json)["complaint_ref"]: json.loads(r.payload_json)["body"] for r in sms(session)}
    assert bodies[sw.ref].startswith(f"Huduma imerejea Kayole. Lalamiko lako {sw.ref} limefungwa. Bado haifanyi kazi? "
                                     "Tuambie hapa http://127.0.0.1:8000/track?ref=")
    # No place named: the incident's area, in the floor's words (the region's label).
    assert bodies[mixed.ref].startswith("Service is back in Nairobi East. ")
    assert all(segments_for(body) == 1 for body in bodies.values())


def test_the_track_url_follows_the_public_base_url(session, monkeypatch):
    monkeypatch.setenv(loop.BASE_URL_ENV, "https://care.example.co.ke/")
    inc = incident(session)
    row = complaint(session, inc, msisdn(11))
    restore(session, inc)
    assert json.loads(sms(session)[0].payload_json)["body"].endswith(f"Tell us at https://care.example.co.ke/track?ref={row.ref}")


# ================================================================= 2. wait or send now


@pytest.mark.parametrize("level,priority,waits", [
    ("L1_COPILOT", "P1", True), ("L1_COPILOT", "P3", True), ("L1_COPILOT", "P4", True),
    ("L2_GUARDED", "P1", True), ("L2_GUARDED", "P2", True), ("L2_GUARDED", "P3", False), ("L2_GUARDED", "P4", False),
    ("L3_CONDITIONAL", "P1", True), ("L3_CONDITIONAL", "P2", False), ("L3_CONDITIONAL", "P4", False),
    ("L9_UNKNOWN", "P4", True),  # a level the policy does not name waits for everything
    ("L2_GUARDED", "P9", True),  # so does a priority it does not know
])
def test_the_autonomy_ladder_decides_who_sends(session, level, priority, waits):
    inc = incident(session, priority=priority)
    row = complaint(session, inc, msisdn(12))
    notice = restore(session, inc, level=level)
    session.refresh(row)
    [out] = sms(session)
    if waits:
        [card] = cards(session)
        assert notice.state == "awaiting_approval" and notice.card_id == card.id
        assert (out.status, out.requires_hitl, out.hitl_task_id) == (outbox.HELD, 1, card.id)
        assert (card.incident_id, card.operator_id, card.entity_type, card.entity_id) == (inc.id, OP, "support_notice", notice.id)
        assert card.created_by == loop.RAISED_BY and row.told_restored_at is None and row.status == "action_taken"
        session.refresh(inc)
        assert inc.requires_hitl  # the card is an open task on the incident
    else:
        assert cards(session) == [] and notice.state == "sent"
        assert (out.status, out.requires_hitl) == (outbox.PENDING, 0) and row.status == "closed"


@pytest.mark.parametrize("numbers,waits", [(20, False), (21, True)])
def test_more_than_twenty_numbers_wait_whatever_the_priority(session, numbers, waits):
    inc = incident(session, priority="P4")
    for n in range(numbers):
        complaint(session, inc, msisdn(100 + n))
    notice = restore(session, inc, level="L3_CONDITIONAL")
    assert notice.recipients == numbers and (notice.state == "awaiting_approval") is waits
    assert len(sms(session)) == numbers and len(cards(session)) == (1 if waits else 0)


def test_the_card_payload_is_the_contracts(session):
    inc = incident(session, priority="P2")
    rows = [complaint(session, inc, msisdn(200 + n), language="sw" if n % 3 == 0 else "en",
                      place=("kayole", "umoja", "pipeline", "donholm", "kayole")[n % 5]) for n in range(12)]
    restore(session, inc)
    [card] = cards(session)
    payload = card.proposed_payload
    assert set(payload) == {"kind", "incident_number", "place_summary", "recipients", "languages", "text_en", "text_sw",
                            "segments_en", "segments_sw", "sample", "restore_source",
                            "restored_at", "restored_by", "restore_note", "incident_status"}  # 7.2: the evidence
    assert (payload["restored_at"], payload["incident_status"]) == ("2026-10-05T13:40:00Z", "RESTORED")
    assert payload["kind"] == "restore_notice" and payload["incident_number"] == inc.incident_number
    assert payload["recipients"] == 12 and payload["languages"] == {"en": 8, "sw": 4}
    assert payload["place_summary"] == "Kayole, Umoja, Donholm and 1 more"
    assert payload["text_en"].startswith("Service is back in ") and payload["text_sw"].startswith("Huduma imerejea ")
    assert (payload["segments_en"], payload["segments_sw"], payload["restore_source"]) == (1, 1, "SUPERVISOR")
    assert len(payload["sample"]) == 10 and set(payload["sample"][0]) == {"ref", "msisdn_masked", "language"}
    dumped = json.dumps(payload)
    assert not any(row.msisdn in dumped or row.msisdn[4:] in dumped for row in rows)  # masked numbers only


# ======================================================================= 3. never twice


def test_a_second_restore_and_the_close_never_send_again(session):
    inc = incident(session, priority="P3")
    complaint(session, inc, msisdn(13))
    restore(session, inc)
    assert restore(session, inc, source="MARK_RESTORED") is None
    inc.status = "CLOSED"
    inc.closed_at = T0 + timedelta(hours=1)
    assert loop.on_incident_restored(session, inc, trigger="close", settings=settings_at("L2_GUARDED")) is None
    session.commit()
    assert len(sms(session)) == 1 and session.scalar(select(SupportNoticeRow.id).where(SupportNoticeRow.state != "sent")) is None


def test_a_second_restore_while_the_card_waits_raises_no_second_card(session):
    inc = incident(session, priority="P2")
    complaint(session, inc, msisdn(14))
    restore(session, inc)
    assert restore(session, inc) is None
    inc.status = "CLOSED"
    assert loop.on_incident_restored(session, inc, trigger="close", settings=settings_at("L2_GUARDED")) is None
    assert len(cards(session)) == 1 and len(sms(session)) == 1


def test_the_close_tells_whoever_an_inferred_restore_did_not(session):
    inc = incident(session, priority="P3")
    row = complaint(session, inc, msisdn(15))
    restore(session, inc, source="VENDOR_NOTE_INFERRED")
    inc.status, inc.closed_at = "CLOSED", T0 + timedelta(minutes=30)
    notice = loop.on_incident_restored(session, inc, trigger="close", settings=settings_at("L2_GUARDED"),
                                       now=T0 + timedelta(minutes=31))
    session.commit()
    session.refresh(row)
    assert notice.state == "sent" and notice.restore_source == loop.CLOSE_SOURCE and row.status == "closed"
    assert steps(session, row)[0].summary.endswith(f"({inc.incident_number} closed 17:10)")


def test_a_trigger_that_does_not_match_the_incident_does_nothing(session):
    inc = incident(session, status="IN_PROGRESS")
    complaint(session, inc, msisdn(16))
    assert loop.on_incident_restored(session, inc, trigger="restore") is None  # not restored
    assert loop.on_incident_restored(session, inc, trigger="close") is None  # not closed
    with pytest.raises(ValueError):
        loop.on_incident_restored(session, inc, trigger="reopen")
    assert sms(session) == []


# ============================================================== 4. the card's outcomes


def _waiting(session, n=2):
    inc = incident(session, priority="P2")
    rows = [complaint(session, inc, msisdn(300 + i)) for i in range(n)]
    restore(session, inc)
    return inc, rows, cards(session)[0]


def test_approve_releases_the_held_rows_and_tells(session):
    inc, rows, card = _waiting(session)
    at = T0 + timedelta(minutes=12)
    assert loop.approve_customer_update(session, card, approved_by="Shift Supervisor", approved_at=at) == 2
    session.commit()
    assert {(r.status, r.approved_by) for r in sms(session)} == {(outbox.PENDING, "Shift Supervisor")}
    for row in rows:
        session.refresh(row)
        assert row.status == "closed" and row.told_restored_at == at
        assert json.loads(steps(session, row, "told_restored")[0].detail_json)["approved_by"] == "Shift Supervisor"
    notice = session.scalar(select(SupportNoticeRow))
    assert (notice.state, notice.sent_at, notice.decided_by) == ("sent", at, "Shift Supervisor")
    # Approving again changes nothing: no row is HELD (so none goes back to PENDING to be sent
    # again, nor changes approver), and nobody is told twice.
    before = [(r.id, r.status, r.approved_by, r.approved_at) for r in sms(session)]
    assert loop.approve_customer_update(session, card, approved_by="Someone Else", approved_at=at + timedelta(minutes=1)) == 0
    session.commit()
    assert [(r.id, r.status, r.approved_by, r.approved_at) for r in sms(session)] == before
    assert all(len(steps(session, row, "told_restored")) == 1 for row in rows)


def test_reject_holds_the_update_back_and_the_close_raises_it_again(session):
    """7.1: reject is "Not now". Nobody is told, the customers stay waiting, the notice is held_back
    with the reason; a re-restore does not raise it again, the close does (a fresh attempt, fresh keys)."""
    inc, rows, card = _waiting(session)
    assert loop.reject_customer_update(session, card, rejected_by="Duty Manager", reason="Embakasi still flapping",
                                       at=T0 + timedelta(minutes=5)) == 2
    session.commit()
    assert {r.status for r in sms(session)} == {outbox.SUPPRESSED}
    assert all("Embakasi still flapping" in r.last_error for r in sms(session))
    notice = session.scalar(select(SupportNoticeRow))
    assert (notice.state, notice.reason, notice.decided_by) == ("held_back", "Embakasi still flapping", "Duty Manager")
    for row in rows:
        session.refresh(row)
        assert row.told_restored_at is None and row.status == "action_taken"  # still waiting to hear
        assert json.loads(steps(session, row, "restore_notice_held_back")[0].detail_json)["reason"] == "Embakasi still flapping"
    outage = next(o for o in loop.outages(session, OP) if o["incident_id"] == inc.id)
    assert outage["waiting"] == 2 and outage["notice"]["state"] == "held_back"
    assert (outage["notice"]["reason"], outage["notice"]["held_by"]) == ("Embakasi still flapping", "Duty Manager")
    assert restore(session, inc) is None  # a re-restore leaves a held-back update alone
    inc.status, inc.closed_at = "CLOSED", T0 + timedelta(hours=1)
    again = loop.on_incident_restored(session, inc, trigger="close", settings=settings_at("L2_GUARDED"),
                                      now=T0 + timedelta(hours=1))
    session.commit()
    assert again is not None and again.id != notice.id and again.state == "awaiting_approval"
    assert len(cards(session)) == 2 and len(sms(session)) == 4  # a fresh attempt: fresh keys beside the suppressed ones
    new_card = next(c for c in cards(session) if c.id == again.card_id)
    assert loop.approve_customer_update(session, new_card, approved_by="Duty Manager", approved_at=T0 + timedelta(hours=1)) == 2


def test_a_broadcast_decision_on_the_same_incident_cannot_sweep_the_customer_rows(session):
    """The SMS rows carry the card's id and not the incident's, because the broadcast gate's reject
    (suppress_held_outbox) and approve (release_held) act on every HELD row OF THE INCIDENT."""
    inc, _rows, _card = _waiting(session)
    assert suppress_held_outbox(session, inc.id, reason="broadcast rejected") == 0
    assert outbox.release_held(session, incident_id=inc.id, alert_id="a1", approved_by="x", approved_at=T0) == 0
    assert {r.status for r in sms(session)} == {outbox.HELD}


# ======================================================================== 5. still down


def _told(session, *, hours_ago=1.0, still_down_hours_ago=None):
    inc = incident(session)
    row = complaint(session, inc, msisdn(400 + next(_seq)))
    row.status, row.closure_reason, row.told_incident_id = "closed", "service_restored", inc.id
    row.told_restored_at = T0 - timedelta(hours=hours_ago)
    if still_down_hours_ago is not None:
        row.still_down_at = T0 - timedelta(hours=still_down_hours_ago)
    session.commit()
    return inc, row


@pytest.mark.parametrize("hours_ago,still_down_hours_ago,allowed", [
    (None, None, False),  # never told
    (71.5, None, True), (72.5, None, False),  # the 72-hour window
    (30, 23, False), (30, 25, True),  # once a day
])
def test_the_still_down_windows(session, hours_ago, still_down_hours_ago, allowed):
    policy = load_policy()
    if hours_ago is None:
        row = complaint(session, incident(session), msisdn(450))
    else:
        _inc, row = _told(session, hours_ago=hours_ago, still_down_hours_ago=still_down_hours_ago)
    refusal = loop.still_down_refusal(session, row, policy, T0)
    assert (refusal is None) is allowed, refusal
    if not allowed:
        assert refusal.endswith(".") and "_" not in refusal  # a plain sentence, no code


def test_a_still_down_report_reopens_notes_the_incident_and_needs_two_numbers_for_a_card(session):
    inc, first = _told(session)
    second = complaint(session, inc, msisdn(460))
    second.status, second.told_incident_id, second.told_restored_at = "closed", inc.id, T0 - timedelta(hours=1)
    first.claimed_by = "Someone"
    session.commit()
    loop.report_still_down(session, first, note="Bado hakuna kitu", ctx=CTX, now=T0)
    session.refresh(first)
    assert first.status == "escalated" and first.escalation_reason_code == "still_down_after_restore"
    assert first.escalation_reason == "you told us service is still down, so a person will check it"
    assert first.claimed_by is None and first.closure_reason is None and first.still_down_at == T0
    assert first.sla_due_at > T0
    messages = session.scalars(select(SupportMessageRow).where(SupportMessageRow.complaint_id == first.id)
                               .order_by(SupportMessageRow.at, SupportMessageRow.id)).all()
    assert ("customer", "Bado hakuna kitu", "web") in {(m.author, m.body, m.channel) for m in messages}
    [step] = steps(session, first, "still_down_reported")
    assert step.agent == "followup" and json.loads(step.detail_json)["place"] == "kayole"
    [note] = session.scalars(select(WorkNoteRow).where(WorkNoteRow.incident_id == inc.id)).all()
    assert note.source == "support"
    assert note.body == f"Customer {first.ref} reports service is still down in Kayole after the restore (1 of 2 told)"
    assert session.scalar(select(SupportSurgeRow)) is None  # one number is not a surge
    loop.report_still_down(session, second, ctx=CTX, now=T0 + timedelta(minutes=5))
    [s] = session.scalars(select(SupportSurgeRow)).all()
    assert (s.origin, s.parent_incident_id, s.place, s.status, s.numbers) == ("still_down", inc.id, "kayole", "open", 2)
    [card] = cards(session, surge.SURGE_TASK_TYPE)
    assert card.proposed_payload["parent_incident_number"] == inc.incident_number
    assert card.proposed_payload["origin"] == "still_down"


def test_a_refused_still_down_report_changes_nothing(session):
    _inc, row = _told(session, hours_ago=80)
    with pytest.raises(loop.TrackConflict, match="more than 72 hours"):
        loop.report_still_down(session, row, ctx=CTX, now=T0)
    session.refresh(row)
    assert row.status == "closed" and row.still_down_at is None and steps(session, row) == []


# ============================================================================ 6. surges


def _network(session, n, *, minutes_ago, place="rongai", operator=OP, category="network", linked=None):
    row = complaint(session, linked, msisdn(n), place=place, operator=operator, category=category, status="answered",
                    created_at=T0 - timedelta(minutes=minutes_ago), body=f"No network in {place.title()} since lunch.")
    session.commit()
    return row


def test_three_distinct_numbers_inside_the_window_open_one_surge_and_one_card(session):
    a = _network(session, 500, minutes_ago=29)
    assert surge.observe(session, a, ctx=CTX) is None
    again = _network(session, 500, minutes_ago=20)  # the same number twice is still one number
    assert surge.observe(session, again, ctx=CTX) is None
    b = _network(session, 501, minutes_ago=10)
    assert surge.observe(session, b, ctx=CTX) is None
    c = _network(session, 502, minutes_ago=0)
    s = surge.observe(session, c, ctx=CTX)
    assert (s.status, s.place, s.origin, s.complaints, s.numbers) == ("open", "rongai", "complaints", 4, 3)
    assert s.region_code == CTX.gazetteer.regions_of("rongai")[0] and s.open_place == "rongai"
    [card] = cards(session, surge.SURGE_TASK_TYPE)
    assert (card.incident_id, card.entity_type, card.entity_id, card.operator_id) == (None, "support_surge", s.id, OP)
    payload = card.proposed_payload
    assert set(payload) == {"kind", "place", "region_code", "complaints", "numbers", "first_at", "last_at", "origin",
                            "parent_incident_number", "excerpts", "covering_incident_number",  # 7.2 and MINOR 11
                            "text_en", "text_sw", "segments_en", "segments_sw", "languages", "recipients", "sample"}
    assert (payload["kind"], payload["place"], payload["complaints"], payload["numbers"]) == ("surge", "Rongai", 4, 3)
    assert payload["first_at"] == "2026-10-05T13:11:00Z" and payload["last_at"] == "2026-10-05T13:40:00Z"
    assert len(payload["excerpts"]) == 4 and set(payload["excerpts"][0]) == {"ref", "text", "at"}
    assert not any(row.msisdn[4:] in json.dumps(payload) for row in (a, b, c))


def test_what_does_not_count_towards_a_surge(session):
    inc = incident(session, site_name="Rongai BTS", region="NBI_W")
    for n, kwargs in enumerate([
        dict(minutes_ago=31),  # outside the window
        dict(minutes_ago=5, linked=inc),  # linked to an open incident
        dict(minutes_ago=5, category="billing"),  # not a network complaint
        dict(minutes_ago=5, place="kayole"),  # another place
        dict(minutes_ago=5, operator="airtel"),  # another operator
    ]):
        _network(session, 600 + n, **kwargs)
    _network(session, 610, minutes_ago=3)
    last = _network(session, 611, minutes_ago=0)
    assert surge.observe(session, last, ctx=CTX) is None  # only two numbers really count
    no_place = complaint(session, None, msisdn(612), place=None, status="answered", created_at=T0)
    session.commit()
    assert surge.observe(session, no_place, ctx=CTX) is None
    assert session.scalar(select(SupportSurgeRow)) is None


def test_later_complaints_join_the_open_surge_and_refresh_the_card(session):
    rows = [_network(session, 700 + n, minutes_ago=20 - n) for n in range(3)]
    s = surge.observe(session, rows[-1], ctx=CTX)
    for n in range(3):  # three MORE numbers: a second threshold's worth, still one surge
        late = _network(session, 710 + n, minutes_ago=-5 - n)
        assert surge.observe(session, late, ctx=CTX).id == s.id
    session.refresh(s)
    assert (s.complaints, s.numbers) == (6, 6)
    assert len(session.scalars(select(SupportSurgeRow)).all()) == 1 and len(cards(session, surge.SURGE_TASK_TYPE)) == 1
    [card] = cards(session, surge.SURGE_TASK_TYPE)
    assert card.proposed_payload["complaints"] == 6 and card.proposed_payload["last_at"] == "2026-10-05T13:47:00Z"
    assert len(card.proposed_payload["excerpts"]) == surge.EXCERPTS_MAX


def test_a_dismissed_surge_does_not_reopen_on_the_next_complaint(session):
    rows = [_network(session, 800 + n, minutes_ago=10 - n) for n in range(3)]
    s = surge.observe(session, rows[-1], ctx=CTX)
    [card] = cards(session, surge.SURGE_TASK_TYPE)
    assert surge.dismiss(session, card, actor="Duty Manager", reason="known fibre cut", at=T0).id == s.id
    session.commit()
    assert (s.status, s.open_place, s.reason) == ("dismissed", None, "known fibre cut")
    fourth = _network(session, 803, minutes_ago=-1)
    assert surge.observe(session, fourth, ctx=CTX) is None  # the dismissed three are not counted again
    for n in (804, 805):
        assert surge.observe(session, _network(session, n, minutes_ago=-2), ctx=CTX) is not None or n == 804
    assert len(session.scalars(select(SupportSurgeRow).where(SupportSurgeRow.status == "open")).all()) == 1


def test_an_excerpt_is_at_most_140_characters():
    assert surge._excerpt("x" * 500) == "x" * 139 + "…" and len(surge._excerpt("y" * 141)) == 140
    assert surge._excerpt("  short   text ") == "short text"


def test_the_card_names_no_open_surge_when_decided_on_the_wrong_entity(session):
    card = HitlTaskRow(incident_id=None, operator_id=OP, task_type=surge.SURGE_TASK_TYPE, entity_type="rbac_matrix",
                       entity_id="x", status="PENDING")
    card.proposed_payload = {}
    session.add(card)
    session.flush()
    assert surge.mark_confirmed(session, card, actor="a", at=T0) is None
    assert surge.dismiss(session, card, actor="a", reason="r", at=T0) is None


def test_two_openers_racing_on_one_place_make_one_surge(session):
    """The unique (operator_id, open_place) key is the backstop behind the write lock."""
    first = SupportSurgeRow(operator_id=OP, place="rongai", open_place="rongai", first_at=T0, last_at=T0)
    session.add(first)
    session.commit()
    session.add(SupportSurgeRow(operator_id=OP, place="rongai", open_place="rongai", first_at=T0, last_at=T0))
    with pytest.raises(Exception):  # IntegrityError
        session.commit()
    session.rollback()
    # Settled surges (open_place NULL) never clash with each other or with the open one.
    session.add_all([SupportSurgeRow(operator_id=OP, place="rongai", status="dismissed", first_at=T0, last_at=T0)
                     for _ in range(2)])
    session.commit()


def test_the_surge_description_reads_like_the_contract():
    s = SupportSurgeRow(place="rongai", numbers=4, first_at=datetime(2026, 10, 5, 11, 5), last_at=datetime(2026, 10, 5, 11, 31))
    assert surge.description_for(s, "Africa/Nairobi") == \
        "4 customers reported no service in Rongai between 14:05 and 14:31; no network alarm"
    assert surge.site_id_for("NBI_W", "ongata rongai") == "CUST-NBI_W-ONGATA-RONGAI"


# ===================================================================== 7. loop numbers


def test_the_loop_numbers(session):
    policy = load_policy()
    inc1 = incident(session, priority="P3", status="RESTORED")
    inc1.restored_at = T0
    inc2 = incident(session, priority="P3", status="CLOSED")
    inc2.restored_at, inc2.closed_at = T0, T0 + timedelta(minutes=1)
    open_inc = incident(session, priority="P2")
    told_after = [1, 2, 3, 4, 100]  # minutes after the restore
    for n, minutes in enumerate(told_after):
        row = complaint(session, inc1 if n < 3 else inc2, msisdn(900 + n))
        row.told_incident_id, row.told_restored_at, row.status = row.linked_incident_id, T0 + timedelta(minutes=minutes), "closed"
    # Repeat contacts: 900 twice more on inc1 (+2), 950 twice on the open incident (+1).
    for _ in range(2):
        extra = complaint(session, inc1, msisdn(900))
        extra.told_incident_id, extra.told_restored_at = inc1.id, T0 + timedelta(minutes=1)
    complaint(session, open_inc, msisdn(950))
    complaint(session, open_inc, msisdn(950))
    complaint(session, open_inc, msisdn(951))
    session.commit()
    numbers = loop.loop_metrics(session, OP, now=T0 + timedelta(hours=3))
    assert numbers["told"] == 5  # five people, not seven complaints
    assert numbers["told_median_minutes"] == 3.0 and numbers["told_p90_minutes"] == 100.0
    assert numbers["repeat_contacts"] == 3 and numbers["outages_with_complaints"] == 3
    assert numbers["repeat_contacts_per_outage"] == 1.0
    assert numbers["waiting_to_hear"] == 2  # 950 and 951 on the open incident
    assert set(numbers) == {"waiting_to_hear", "told", "told_median_minutes", "told_p90_minutes", "notices_waiting",
                            "recipients_waiting", "still_down_reports", "repeat_contacts", "outages_with_complaints",
                            "repeat_contacts_per_outage", "spotted_by_customers", "surges"}
    assert numbers["surges"] == {"open": 0, "confirmed": 0, "dismissed": 0}
    # A one-hour window ending three hours later sees no SMS at all: timings are None, not 0.
    windowed = loop.loop_metrics(session, OP, hours=1, now=T0 + timedelta(hours=3))
    assert windowed["told"] == 0 and windowed["told_median_minutes"] is None and windowed["told_p90_minutes"] is None
    assert windowed["waiting_to_hear"] == 2  # state now, whatever the window
    assert policy.track.still_down_within_hours == 72


def test_the_p90_is_nearest_rank():
    assert loop._p90([5.0]) == 5.0
    assert loop._p90([float(n) for n in range(1, 11)]) == 9.0
    assert loop._p90([1.0, 2.0, 3.0, 40.0]) == 40.0


def test_waiting_cards_and_still_down_reports_are_counted(session):
    _inc, _rows, _card = _waiting(session, n=3)
    inc, row = _told(session)
    loop.report_still_down(session, row, ctx=CTX, now=T0)
    numbers = loop.loop_metrics(session, OP, now=T0 + timedelta(minutes=5))
    assert (numbers["notices_waiting"], numbers["recipients_waiting"], numbers["still_down_reports"]) == (1, 3, 1)


# ========================================================== 8. outages and the incident panel


def test_the_notice_state_on_outages_and_the_incident_panel(session):
    waiting = incident(session, priority="P3")
    complaint(session, waiting, msisdn(1001))
    inferred = incident(session, priority="P3")
    complaint(session, inferred, msisdn(1002))
    restore(session, inferred, source="VENDOR_NOTE_INFERRED")
    sent = incident(session, priority="P3")
    complaint(session, sent, msisdn(1003))
    complaint(session, sent, msisdn(1003))
    restore(session, sent)
    held = incident(session, priority="P2")
    complaint(session, held, msisdn(1004))
    restore(session, held)
    quiet = incident(session, priority="P3", status="RESTORED")
    quiet.restored_source = "SUPERVISOR"
    complaint(session, quiet, msisdn(1005))  # restored, but no notice was ever written (say the desk was off)
    session.commit()
    rows = {o["incident_number"]: o for o in loop.outages(session, OP)}
    assert rows[waiting.incident_number]["notice"]["state"] == "waiting_for_restore"
    assert rows[inferred.incident_number]["notice"]["state"] == "waiting_for_restore"
    assert rows[sent.incident_number]["notice"]["state"] == "sent"
    assert rows[held.incident_number]["notice"]["state"] == "awaiting_approval"
    assert rows[held.incident_number]["notice"]["card_id"] == cards(session)[0].id
    assert rows[quiet.incident_number]["notice"]["state"] == "none"
    s = rows[sent.incident_number]
    assert (s["customers"], s["told"], s["waiting"], s["repeat_contacts"], s["places"]) == (1, 1, 0, 1, ["Kayole"])
    assert set(s) == {"incident_id", "incident_number", "title", "priority", "status", "places", "restored_at",
                      "restore_source", "customers", "told", "waiting", "still_down", "repeat_contacts", "notice",
                      "from_customer_reports"}
    assert set(s["notice"]) == {"state", "card_id", "recipients", "sent_at", "reason", "held_by", "held_at"}
    panel = loop.incident_customers(session, sent)
    assert set(panel) == {"customers", "told", "waiting", "still_down", "notice", "complaints", "follow_up"}
    assert set(panel["complaints"][0]) == {"id", "ref", "msisdn_masked", "status", "told_restored_at", "still_down_at",
                                           "closure_reason"}
    assert panel["complaints"][0]["msisdn_masked"].startswith("+254 7•• •• ") and panel["follow_up"] is None
    assert (panel["customers"], panel["told"], len(panel["complaints"])) == (1, 1, 2)
    assert "+2547" not in json.dumps(panel)  # masked numbers only
    assert loop.outages(session, "airtel") == []


def test_a_surge_member_counts_once_per_kind(session):
    rows = [_network(session, 1100 + n, minutes_ago=5 - n) for n in range(3)]
    s = surge.observe(session, rows[-1], ctx=CTX)
    assert surge._add_member(session, s, rows[0], "complaint", T0) is False
    assert surge._add_member(session, s, rows[0], "still_down", T0) is True
    assert len(session.scalars(select(SupportSurgeMemberRow).where(SupportSurgeMemberRow.surge_id == s.id)).all()) == 4


# ===================================================================== 9. never in the evals


def test_the_eval_runner_never_counts_surges(monkeypatch):
    """Surges are observed by the API and the seeder only: eval complaints live in throwaway
    databases and must never open a card or a ticket (section 3)."""
    import inspect

    from noc_agents.support import evals

    assert "surge" not in inspect.getsource(evals)
    called = []
    monkeypatch.setattr(surge, "observe", lambda *args, **kwargs: called.append(args))
    evals.run_eval(operator_id=OP, ctx=CTX, split="dev")
    assert called == []


# ============================================================================== 10. races


def test_two_complaints_crossing_the_threshold_at_once_open_one_surge(tmp_path, monkeypatch):
    """Both observers would open the surge; the desk's write lock makes the second wait and join.
    The barrier sits where the race window was (between "is a surge open?" and "open one"), with a
    timeout, so without the lock both threads are inside it together."""
    import threading

    from noc_agents.db import models_all  # noqa: F401

    engine = create_engine(f"sqlite:///{(tmp_path / 'race.db').as_posix()}",
                           connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, future=True)
    seed = factory()
    rows = [_network(seed, 1200 + n, minutes_ago=10 - n) for n in range(4)]
    for row in rows[:2]:
        assert surge.observe(seed, row, ctx=CTX) is None
    ids = [rows[2].id, rows[3].id]
    seed.close()
    barrier = threading.Barrier(2)
    real = surge._open_surge

    def window(*args, **kwargs):
        found = real(*args, **kwargs)
        try:
            barrier.wait(timeout=1.5)
        except threading.BrokenBarrierError:
            pass  # alone: the other observer is (correctly) waiting for the lock
        return found

    monkeypatch.setattr(surge, "_open_surge", window)
    errors: list[BaseException] = []

    def observe(complaint_id: str) -> None:
        s = factory()
        try:
            surge.observe(s, s.get(SupportComplaintRow, complaint_id), ctx=CTX)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            s.close()

    threads = [threading.Thread(target=observe, args=(i,), daemon=True) for i in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == [] and not any(t.is_alive() for t in threads)
    check = factory()
    try:
        [opened] = check.scalars(select(SupportSurgeRow)).all()
        members = {m.complaint_id for m in check.scalars(select(SupportSurgeMemberRow)).all()}
        assert set(ids) <= members and opened.numbers == 4
        assert len(check.scalars(select(HitlTaskRow).where(HitlTaskRow.task_type == surge.SURGE_TASK_TYPE)).all()) == 1
    finally:
        check.close()
        engine.dispose()
