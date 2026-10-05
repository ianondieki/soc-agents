"""Close the loop, revision 2 and the security review (docs/CLOSE_THE_LOOP.md section 7 and the
Decisions), at the service level: link strength, late linking, "Not now", the card evidence, the
Track page's honesty and its echo-only rule, the daily SMS cap, keyed hashes, stale and covered
surges, the tell history -- and operator scoping across all of it. Each test is written to fail
when the rule it names is broken (the mutation run in the report kills each one)."""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import logging
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
from noc_agents.support import desk, loop, surge, tools
from noc_agents.support.context import default_context
from noc_agents.support.text import mask_msisdn, mask_msisdn_staff, redact_untyped

OP = "safaricom"
CTX = default_context()
T0 = datetime(2026, 10, 5, 13, 40)
_seq = itertools.count(1)


@pytest.fixture()
def session(tmp_path):
    from noc_agents.db import models_all  # noqa: F401

    engine = create_engine(f"sqlite:///{(tmp_path / 'r2.db').as_posix()}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, future=True)()
    yield s
    s.close()
    engine.dispose()


def settings_at(level: str = "L2_GUARDED"):
    settings = get_settings().model_copy(deep=True)
    settings.operator.autonomy_level = level
    return settings


def msisdn(n: int) -> str:
    return f"+2547001{n:05d}"


def incident(session, *, priority="P3", region="NBI_E", site_name="Embakasi East Aggregation HUB", site_type="HUB",
             status="IN_PROGRESS", operator=OP, county="Nairobi", children=0, created_at=None) -> IncidentRow:
    n = next(_seq)
    inc = IncidentRow(operator_id=operator, incident_number=f"INC{n:06d}", status=status, priority=priority,
                      site_id=f"SITE-{n}", site_name=site_name, site_type=site_type, region_code=region, county=county,
                      child_sites_down=children, correlation_fingerprint=f"fp-{n}",
                      created_at=created_at or T0 - timedelta(hours=2), users_affected=1000)
    session.add(inc)
    session.flush()
    return inc


def complaint(session, inc, number, *, place="kayole", status="action_taken", created_at=None, operator=OP,
              category="network", body="No network in Kayole since morning.", channel="web", language="en"):
    n = next(_seq)
    row = SupportComplaintRow(
        operator_id=operator, ref=f"CMP-{n:06d}", msisdn=number, msisdn_masked=mask_msisdn(number), body=body,
        body_hash=f"h{n}", category=category, status=status, place=place, channel=channel, language=language,
        linked_incident_id=inc.id if inc is not None else None, link_strength="site" if inc is not None else None,
        created_at=created_at or T0 - timedelta(minutes=30), updated_at=T0 - timedelta(minutes=30),
        sla_due_at=T0 + timedelta(hours=4),
    )
    session.add(row)
    session.flush()
    return row


def restore(session, inc, *, source="SUPERVISOR", level="L2_GUARDED", now=None, note=None):
    inc.status, inc.restored_source, inc.restored_at, inc.restored_by = "RESTORED", source, T0, "Shift Supervisor"
    session.flush()
    notice = loop.on_incident_restored(session, inc, trigger="restore", settings=settings_at(level), note=note,
                                       now=now or T0 + timedelta(minutes=1))
    session.commit()
    return notice


def sms(session) -> list[OutboxRow]:
    return list(session.scalars(select(OutboxRow).where(OutboxRow.kind == "SMS").order_by(OutboxRow.created_at)).all())


def steps(session, row, action=None):
    stmt = select(SupportStepRow).where(SupportStepRow.complaint_id == row.id)
    if action:
        stmt = stmt.where(SupportStepRow.action == action)
    return list(session.scalars(stmt.order_by(SupportStepRow.seq)).all())


# ===================================================================== B1. link strength


@pytest.mark.parametrize("site_name,site_type,county,children,place,regions,expected", [
    ("Kayole Town BTS", "BTS", "Nairobi", 0, "kayole", ("NBI_E",), "site"),
    ("Some BTS", "BTS", "Nairobi", 0, "nairobi", ("NBI_E", "NBI_W"), "county"),
    ("Embakasi East Aggregation HUB", "HUB", "Nairobi", 0, "kayole", ("NBI_E",), "wide_area"),
    ("Core Switch", "CORE", "Nairobi", 0, "kayole", ("NBI_E",), "wide_area"),
    ("Umoja BTS", "BTS", "Nairobi", 2, "kayole", ("NBI_E",), "wide_area"),  # a site with children down
    ("Umoja BTS", "BTS", "Nairobi", 0, "kayole", ("NBI_E",), "region"),  # one site's outage: weak
    ("Umoja BTS", "BTS", "Nairobi", 0, "kayole", ("NBI_W",), None),
    # A title never counts as the site: titles carry the region's label ("(Nairobi East)").
    ("Ring Node 4", "TX", "Machakos", 0, "nairobi east", ("NBI_E",), "region"),
])
def test_link_strength_grades(session, site_name, site_type, county, children, place, regions, expected):
    inc = incident(session, site_name=site_name, site_type=site_type, county=county, children=children)
    inc.title = f"[TX] TRANSMISSION - {site_name} (Nairobi East)"
    assert tools.link_strength(inc, place, regions) == expected


def test_a_weak_region_match_is_not_linked_and_is_named_as_nearby(session):
    weak = incident(session, site_name="Umoja BTS", site_type="BTS")
    env = tools.ToolEnv(session, OP, msisdn(1), None, CTX.policy, T0)
    out = tools.run_tool("link_incident", env, {"place": "kayole", "regions": ["NBI_E"]})
    assert out.fallback and out.result["found"] is False
    assert out.result["nearby_incident"]["incident_number"] == weak.incident_number
    hub = incident(session, site_name="Embakasi East Aggregation HUB", site_type="HUB")
    out = tools.run_tool("link_incident", env, {"place": "kayole", "regions": ["NBI_E"]})
    assert out.result["found"] and out.result["incident_id"] == hub.id and out.result["link_strength"] == "wide_area"


def test_the_desk_says_honestly_that_an_outage_is_nearby_and_counts_the_complaint_towards_a_surge(session):
    weak = incident(session, site_name="Umoja BTS", site_type="BTS")
    session.commit()
    rows = []
    for n in range(3):
        result = desk.process_complaint(session, operator_id=OP, body="Hakuna network huku Kayole tangu asubuhi.",
                                        msisdn=f"07455{n:05d}", ctx=CTX, now=T0 + timedelta(minutes=n))
        row = result.complaint
        assert row.linked_incident_id is None and row.link_strength is None
        assert f"There is a known outage nearby (ticket {weak.incident_number}); your report is with our network team" in row.reply
        called = [s for s in steps(session, row, "called_tool") if json.loads(s.detail_json)["tool"] == "link_incident"]
        assert json.loads(called[0].detail_json)["result"]["nearby_incident"]["incident_number"] == weak.incident_number
        rows.append(row)
        found = surge.observe(session, row, ctx=CTX)
    assert found is not None and found.numbers == 3  # weak complaints feed the early-warning signal


def test_a_strong_link_is_recorded_on_the_complaint(session):
    incident(session, site_name="Embakasi East Aggregation HUB", site_type="HUB")
    session.commit()
    row = desk.process_complaint(session, operator_id=OP, body="Manze hakuna network huku Kayole tangu saa nne.",
                                 msisdn="0745600001", ctx=CTX, now=T0).complaint
    assert row.linked_incident_id is not None and row.link_strength == "wide_area"


# ======================================================================= 7.3 late linking


def test_late_linking_adopts_recent_unlinked_complaints_the_new_incident_covers(session):
    recent = complaint(session, None, msisdn(10), place="kitale", status="answered", created_at=T0 - timedelta(hours=5))
    old = complaint(session, None, msisdn(11), place="kitale", status="answered", created_at=T0 - timedelta(hours=7))
    elsewhere = complaint(session, None, msisdn(12), place="kisumu", status="answered")
    theirs = complaint(session, None, msisdn(13), place="kitale", status="answered", operator="airtel")
    billing = complaint(session, None, msisdn(14), place="kitale", status="answered", category="billing")
    session.commit()
    inc = incident(session, site_name="Kitale Town BTS", site_type="BTS", region="RFT", county="Trans Nzoia")
    session.commit()
    assert loop.late_link(session, inc, ctx=CTX, now=T0) == 1
    session.refresh(recent)
    assert (recent.linked_incident_id, recent.link_strength) == (inc.id, "site")
    [step] = steps(session, recent, "linked_late")
    assert step.agent == "followup" and inc.incident_number in step.summary
    for row in (old, elsewhere, theirs, billing):
        session.refresh(row)
        assert row.linked_incident_id is None, row.ref
    assert sms(session) == [] and session.scalars(select(SupportMessageRow)).all() == []  # no SMS on a late link
    child = incident(session, site_name="Kitale North BTS", site_type="BTS", region="RFT")
    child.parent_incident_id = inc.id
    assert loop.late_link(session, child, ctx=CTX, now=T0) == 0  # a cascade child adopts nobody


def test_late_linking_never_links_on_a_weak_match(session):
    row = complaint(session, None, msisdn(15), place="kayole", status="answered")
    session.commit()
    weak = incident(session, site_name="Umoja BTS", site_type="BTS")  # same region, one site: weak
    session.commit()
    assert loop.late_link(session, weak, ctx=CTX, now=T0) == 0
    session.refresh(row)
    assert row.linked_incident_id is None


# ================================================================ 7.1 not now, and raising again


def test_a_person_raises_a_held_back_update_again_and_a_told_number_is_never_told_twice(session):
    inc = incident(session, priority="P3")
    told = complaint(session, inc, msisdn(20))
    restore(session, inc)  # P3 at L2: told at once
    later = complaint(session, inc, msisdn(20))  # the same number again, linked afterwards
    other = complaint(session, inc, msisdn(21))
    session.commit()
    notice = loop.raise_customer_update(session, inc, actor="Duty Manager", settings=settings_at())
    assert notice.state == "sent" and notice.recipients == 1  # only 21: 20 was told already
    payloads = [json.loads(r.payload_json) for r in sms(session)]
    assert [p["complaint_ref"] for p in payloads].count(told.ref) == 1 and other.ref in [p["complaint_ref"] for p in payloads]
    assert later.ref not in [p["complaint_ref"] for p in payloads]
    with pytest.raises(loop.UpdateConflict, match="told"):
        loop.raise_customer_update(session, inc, actor="Duty Manager", settings=settings_at())


def test_raising_again_is_refused_while_open_or_while_a_card_waits(session):
    open_inc = incident(session, priority="P2")
    complaint(session, open_inc, msisdn(22))
    session.commit()
    with pytest.raises(loop.UpdateConflict, match="restored or closed"):
        loop.raise_customer_update(session, open_inc, actor="x", settings=settings_at())
    restore(session, open_inc)  # P2 at L2: a card waits
    with pytest.raises(loop.UpdateConflict, match="waiting for approval"):
        loop.raise_customer_update(session, open_inc, actor="x", settings=settings_at())


def test_the_update_card_carries_the_evidence_that_service_is_back(session):
    inc = incident(session, priority="P2")
    complaint(session, inc, msisdn(23))
    restore(session, inc, note="Generator refuelled; all sectors up since 16:35. " + "x" * 300)
    card = session.scalar(select(HitlTaskRow).where(HitlTaskRow.task_type == loop.CUSTOMER_UPDATE_TASK_TYPE))
    payload = card.proposed_payload
    assert payload["restored_at"] == "2026-10-05T13:40:00Z" and payload["restored_by"] == "Shift Supervisor"
    assert payload["restore_note"].startswith("Generator refuelled") and len(payload["restore_note"]) == 200
    assert payload["incident_status"] == "RESTORED"
    assert payload["sample"][0]["msisdn_masked"] == mask_msisdn_staff(msisdn(23))  # four digits for staff


def test_approve_and_reject_touch_only_the_cards_own_held_rows(session):
    """MAJOR 4: a HELD broadcast row of the same incident, a HELD handover row and a HELD regulatory
    row beside the card's own rows are never released or suppressed by its decision."""
    inc = incident(session, priority="P2")
    complaint(session, inc, msisdn(24))
    complaint(session, inc, msisdn(25))
    restore(session, inc)
    card = session.scalar(select(HitlTaskRow).where(HitlTaskRow.task_type == loop.CUSTOMER_UPDATE_TASK_TYPE))
    others = [
        outbox.enqueue(session, kind="SMS", idempotency_key="broadcast-1", incident_id=inc.id, alert_id="a1", held=True,
                       requires_hitl=True, payload={"audience": "ops"}, operator_id=OP),
        outbox.enqueue(session, kind="EMAIL", idempotency_key="handover-1", hitl_task_id="handover-card", held=True,
                       requires_hitl=True, payload={"audience": "shift"}, operator_id=OP),
        outbox.enqueue(session, kind="EMAIL", idempotency_key="regulatory-1", hitl_task_id="regulatory-card", held=True,
                       incident_id=inc.id, requires_hitl=True, payload={"regulatory_notification_id": "r1"}, operator_id=OP),
    ]
    session.commit()
    ids = {r.id for r in others}
    assert loop.approve_customer_update(session, card, approved_by="DM", approved_at=T0 + timedelta(minutes=5)) == 2
    session.commit()
    assert {r.status for r in session.scalars(select(OutboxRow).where(OutboxRow.id.in_(ids))).all()} == {outbox.HELD}
    inc2 = incident(session, priority="P2", site_name="Thika Mt Kenya HUB", region="MTK")
    complaint(session, inc2, msisdn(26), place="thika")
    restore(session, inc2)
    card2 = session.scalar(select(HitlTaskRow).where(HitlTaskRow.task_type == loop.CUSTOMER_UPDATE_TASK_TYPE,
                                                     HitlTaskRow.incident_id == inc2.id))
    assert loop.reject_customer_update(session, card2, rejected_by="DM", reason="wrong place", at=T0) == 1
    session.commit()
    assert {r.status for r in session.scalars(select(OutboxRow).where(OutboxRow.id.in_(ids))).all()} == {outbox.HELD}


# ======================================================================== B4. the daily cap


def _sent_today(session, number: str, n: int) -> None:
    for i in range(n):
        row = outbox.enqueue(session, kind="SMS", idempotency_key=f"support-earlier:{number}:{i}", operator_id=OP,
                             payload={"msisdn_hash": loop.msisdn_hash(number), "audience": "customer"})
        row.status, row.updated_at = outbox.SENT, T0 - timedelta(hours=2)
    session.commit()


#: The daily cap from the shipped policy; the tests fill a number up to it rather than to a constant.
CAP = CTX.policy.customer_updates.max_sms_per_number_per_day


def test_a_number_already_at_the_daily_cap_is_not_sent_another(session):
    inc = incident(session, priority="P3")
    capped = complaint(session, inc, msisdn(30))
    fresh = complaint(session, inc, msisdn(31))
    _sent_today(session, msisdn(30), CAP)
    notice = restore(session, inc)
    assert notice.recipients == 1
    keys = [json.loads(r.payload_json).get("complaint_ref") for r in sms(session) if r.idempotency_key.startswith("support-restore:")]
    assert keys == [fresh.ref]
    session.refresh(capped)
    assert capped.told_restored_at is None and steps(session, capped, "sms_capped")  # still waiting, and why
    # A day later the cap has rolled over: a person can raise it again for the one still waiting.
    again = loop.raise_customer_update(session, inc, actor="DM", settings=settings_at(), now=T0 + timedelta(hours=25))
    assert again.recipients == 1


def test_the_cap_applies_at_approval_too(session):
    inc = incident(session, priority="P2")
    row = complaint(session, inc, msisdn(32))
    restore(session, inc)
    _sent_today(session, msisdn(32), CAP)  # the number reached the cap while the card waited
    card = session.scalar(select(HitlTaskRow).where(HitlTaskRow.task_type == loop.CUSTOMER_UPDATE_TASK_TYPE))
    assert loop.approve_customer_update(session, card, approved_by="DM", approved_at=T0 + timedelta(minutes=3)) == 0
    held = [r for r in sms(session) if r.hitl_task_id == card.id]
    assert held[0].status == outbox.SUPPRESSED and "cap" in held[0].last_error
    session.refresh(row)
    assert row.told_restored_at is None


def test_startup_warns_about_the_hash_key_and_an_unverified_real_sms_adapter(monkeypatch):
    monkeypatch.delenv("SUPPORT_HASH_KEY", raising=False)
    monkeypatch.setenv("SMS_ENABLED", "true")
    monkeypatch.setenv("SMS_PROVIDER", "africastalking")
    warnings = loop.startup_warnings()
    assert any("SUPPORT_HASH_KEY" in w for w in warnings) and any("not verified" in w for w in warnings)
    monkeypatch.setenv("SUPPORT_HASH_KEY", "a-real-secret")
    monkeypatch.setenv("SMS_ENABLED", "false")
    assert loop.startup_warnings() == []
    monkeypatch.setenv("SMS_ENABLED", "true")
    monkeypatch.setenv("SMS_PROVIDER", "mock")
    assert loop.startup_warnings() == []


def test_the_lifespan_logs_the_startup_warnings(monkeypatch, caplog):
    import asyncio

    import noc_agents.main as main

    monkeypatch.delenv("SUPPORT_HASH_KEY", raising=False)
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")

    async def boot() -> None:
        async with main.lifespan(main.app):
            pass

    with caplog.at_level(logging.WARNING, logger="noc_agents.main"):
        asyncio.run(boot())
    assert any("SUPPORT_HASH_KEY" in r.getMessage() for r in caplog.records)


# =========================================================================== B9. keyed hashes


def test_number_hashes_are_hmacs_keyed_by_the_support_hash_key(monkeypatch):
    monkeypatch.setenv("SUPPORT_HASH_KEY", "key-one")
    one = loop.msisdn_hash("+254700000412")
    assert one == hmac.new(b"key-one", b"+254700000412", hashlib.sha256).hexdigest()[:24]
    assert one != hashlib.sha256(b"+254700000412").hexdigest()[:24]  # not a plain hash anyone can rebuild
    monkeypatch.setenv("SUPPORT_HASH_KEY", "key-two")
    assert loop.msisdn_hash("+254700000412") != one
    monkeypatch.delenv("SUPPORT_HASH_KEY")
    assert loop.msisdn_hash("+254700000412") == hmac.new(loop.DEV_HASH_KEY.encode(), b"+254700000412", hashlib.sha256).hexdigest()[:24]


# ===================================================================== 7.3 / B2 / B6 the Track page


def test_track_never_shows_a_code_or_amount_the_caller_did_not_type(session):
    """The reviewer's case: a call-centre complaint (staff typed it) about SJK4H7QW2L of KES 1,500."""
    text = "I sent SJK4H7QW2L of KES 1,500 to the wrong number this morning, please reverse it."
    staff_typed = desk.process_complaint(session, operator_id=OP, body=text, msisdn="0700000412", channel="call_centre",
                                         ctx=CTX, now=T0).complaint
    assert "SJK4H7QW2L" in staff_typed.reply  # the desk's reply did echo it ...
    page = json.dumps(loop.tracked(session, staff_typed, policy=CTX.policy, now=T0))
    assert "SJK4H7QW2L" not in page and "1,500" not in page and "1500" not in page  # ... Track never does
    assert "[code]" in page and "[amount]" in page
    self_typed = desk.process_complaint(session, operator_id=OP, body=text, msisdn="0700000412", channel="web",
                                        ctx=CTX, now=T0 + timedelta(minutes=5)).complaint
    page = json.dumps(loop.tracked(session, self_typed, policy=CTX.policy, now=T0 + timedelta(minutes=5)))
    assert "SJK4H7QW2L" in page and "1,500" in page  # the caller's own words come back to them


def test_the_redaction_rule():
    assert redact_untyped("Reversed SJK4H7QW2L of KES 1,500.", "") == "Reversed [code] of KES [amount]."
    assert redact_untyped("Reversed SJK4H7QW2L of KES 1,500.", "code SJK4H7QW2L amount 1500") == "Reversed SJK4H7QW2L of KES 1,500."
    kept = "Dial *544# or call 100. Ticket INC000004, ref CMP-000123 at 16:40, see http://127.0.0.1:8000/track?ref=CMP-000123"
    assert redact_untyped(kept, "") == kept  # short codes, references, times and links are not amounts
    assert redact_untyped("nimetuma 12000", "", bare=True) == "nimetuma [amount]"


def test_track_states_never_say_anything_untrue(session):
    policy = CTX.policy
    unlinked = complaint(session, None, msisdn(40), status="answered")
    page = loop.tracked(session, unlinked, policy=policy, now=T0)
    assert page["headline"] == "We have passed your report to our network team"
    assert page["detail"] == "If we find an outage in your area, we will link your complaint to it and tell you when it is fixed."
    inc = incident(session, priority="P3")
    told = complaint(session, inc, msisdn(41))
    restore(session, inc)
    session.refresh(told)
    assert loop.tracked(session, told, policy=policy, now=T0 + timedelta(minutes=5))["outage"]["state"] == "restored"
    loop.report_still_down(session, told, ctx=CTX, now=T0 + timedelta(minutes=10))
    page = loop.tracked(session, told, policy=policy, now=T0 + timedelta(minutes=11))
    assert page["outage"]["state"] == "still_down"  # never a green "Restored" after they said it is not
    for p in (page, loop.tracked(session, unlinked, policy=policy, now=T0)):
        assert "SMS" not in json.dumps({k: v for k, v in p.items() if k != "messages"})


@pytest.mark.parametrize("status", ["CLOSED", "CANCELLED"])
def test_a_ticket_that_closed_without_telling_them_lets_them_say_it_is_still_down(session, status):
    """MINOR 3: no SMS will come, so the page says so honestly and offers "still down"."""
    inc = incident(session, priority="P2", status=status)
    inc.closed_at = T0
    row = complaint(session, inc, msisdn(42))
    session.commit()
    page = loop.tracked(session, row, policy=CTX.policy, now=T0 + timedelta(hours=1))
    assert page["stage"] == "closed" and page["can_report_still_down"] is True
    assert "If service is still down for you, tell us below." == page["detail"]
    loop.report_still_down(session, row, ctx=CTX, now=T0 + timedelta(hours=1))
    session.refresh(row)
    assert row.status == "escalated" and row.escalation_reason_code == "still_down_after_restore"
    note = session.scalar(select(WorkNoteRow).where(WorkNoteRow.incident_id == inc.id))
    assert "after the ticket closed" in note.body


def test_no_refusal_promises_a_message_that_will_not_come(session):
    policy = CTX.policy
    for row in (complaint(session, None, msisdn(43), status="answered"),
                complaint(session, incident(session, status="IN_PROGRESS"), msisdn(44))):
        refusal = loop.still_down_refusal(session, row, policy, T0)
        assert refusal and "SMS" not in refusal


# ======================================================================= 7.2 / MINOR 11 surges


def _burst(session, place="rongai", start=600):
    rows = []
    for n in range(3):
        row = complaint(session, None, msisdn(start + n), place=place, status="answered",
                        created_at=T0 - timedelta(minutes=10 - n), body=f"No network in {place.title()}.",
                        language="sw" if n == 0 else "en")
        session.commit()
        found = surge.observe(session, row, ctx=CTX)
        rows.append(row)
    return found, rows


def test_the_surge_card_shows_the_confirmation_sms(session):
    found, rows = _burst(session)
    card = session.get(HitlTaskRow, found.card_id)
    p = card.proposed_payload
    assert p["text_en"] == "We have confirmed an outage in Rongai (ticket {ticket}). Engineers are on it; we will tell you when service is back."
    assert p["text_sw"].startswith("Tumethibitisha hitilafu ya mtandao Rongai (tiketi {ticket})")
    assert (p["segments_en"], p["segments_sw"], p["recipients"], p["languages"]) == (1, 1, 3, {"en": 2, "sw": 1})
    assert {s["msisdn_masked"] for s in p["sample"]} == {mask_msisdn_staff(r.msisdn) for r in rows}
    assert p["covering_incident_number"] is None


def test_the_surge_card_names_an_incident_that_now_covers_the_place(session):
    found, _ = _burst(session)
    covering = incident(session, site_name="Ongata Rongai eNodeB", site_type="ENODEB", region="NBI_W", county="Kajiado")
    session.commit()
    late = complaint(session, None, msisdn(610), place="rongai", status="answered")  # (say it named no link)
    surge._join(session, found, late, "complaint", T0, CTX)
    p = session.get(HitlTaskRow, found.card_id).proposed_payload
    assert p["covering_incident_number"] == covering.incident_number
    assert covering.incident_number in p["text_en"]  # the exact SMS, now that the ticket is known


def test_mark_confirmed_only_confirms_an_open_surge(session):
    found, _ = _burst(session)
    card = session.get(HitlTaskRow, found.card_id)
    for status in ("confirmed", "ingesting", "dismissed", "stale"):
        found.status = status
        session.flush()
        assert surge.mark_confirmed(session, card, actor="x", at=T0) is None, status
    found.status = "open"
    assert surge.mark_confirmed(session, card, actor="x", at=T0) == found.id


def test_a_surge_whose_card_was_decided_without_it_goes_stale_and_the_next_complaint_raises_a_fresh_card(session):
    """MINOR 4: the card was approved while the desk was off, so nothing reached the surge."""
    found, rows = _burst(session)
    session.get(HitlTaskRow, found.card_id).status = "APPROVED"
    session.commit()
    nxt = complaint(session, None, msisdn(620), place="rongai", status="answered", created_at=T0 - timedelta(minutes=5))
    session.commit()
    fresh = surge.observe(session, nxt, ctx=CTX)
    session.refresh(found)
    assert (found.status, found.open_place) == ("stale", None)
    assert fresh is not None and fresh.id != found.id and fresh.card_id != found.card_id
    assert fresh.numbers == 4  # the stale surge's complaints count again


def test_a_failing_surge_never_loses_a_still_down_report(session, monkeypatch, caplog):
    """MINOR 5: the surge step runs in a savepoint; its failure is logged and the report stands."""
    inc = incident(session, priority="P3")
    row = complaint(session, inc, msisdn(630))
    restore(session, inc)
    session.refresh(row)

    def broken(*_args, **_kwargs):
        raise RuntimeError("surges are down")

    monkeypatch.setattr(surge, "observe_still_down", broken)
    from noc_agents.realtime.hub import hub

    hub._history.clear()
    with caplog.at_level(logging.ERROR, logger="noc_agents.support.surge"):
        loop.report_still_down(session, row, ctx=CTX, now=T0 + timedelta(minutes=5))
    session.refresh(row)
    assert row.status == "escalated" and steps(session, row, "still_down_reported")
    assert any("could not be counted towards a surge" in r.getMessage() for r in caplog.records)
    assert any(e["type"] == "support.still_down" for e in hub._history)  # the report's own event survived


# ======================================================================= 7.4 / MINOR 10 history


def test_the_incident_panel_names_the_follow_up_ticket(session):
    parent = incident(session, priority="P3", status="RESTORED")
    follow = incident(session, priority="P4", site_name="Kayole (customer reports)")
    session.add(SupportSurgeRow(operator_id=OP, place="kayole", status="confirmed", parent_incident_id=parent.id,
                                incident_id=follow.id, first_at=T0, last_at=T0, decided_at=T0))
    complaint(session, parent, msisdn(700))
    session.commit()
    panel = loop.incident_customers(session, parent)
    assert panel["follow_up"] == {"incident_id": follow.id, "incident_number": follow.incident_number, "status": follow.status}
    listed = surge.list_surges(session, OP)
    assert listed[0]["parent_incident_number"] == parent.incident_number


def test_an_earlier_tell_survives_a_relink_and_a_second_tell(session):
    """MINOR 10: told about A, still down, relinked to B and told about B: A still counts its tell."""
    a = incident(session, priority="P3")
    row = complaint(session, a, msisdn(710))
    restore(session, a)
    session.refresh(row)
    b = incident(session, priority="P3", site_name="Kayole (customer reports)", site_type="BTS")
    row.linked_incident_id, row.status = b.id, "escalated"
    session.commit()
    b.status, b.restored_source, b.restored_at = "RESTORED", "SUPERVISOR", T0 + timedelta(hours=1)
    loop.on_incident_restored(session, b, trigger="restore", settings=settings_at(), now=T0 + timedelta(hours=1, minutes=1))
    session.commit()
    session.refresh(row)
    assert row.told_incident_id == b.id  # the complaint now names only B ...
    outages = {o["incident_id"]: o for o in loop.outages(session, OP)}
    assert outages[a.id]["told"] == 1 and outages[b.id]["told"] == 1  # ... but A keeps its tell
    assert loop.loop_metrics(session, OP, now=T0 + timedelta(hours=2))["told"] == 2
    assert loop.incident_customers(session, a)["complaints"][0]["told_restored_at"] == "2026-10-05T13:41:00Z"


# ================================================================ MINOR 9: operator scoping


def test_another_operators_rows_never_reach_this_operators_loop(session):
    """Seed the other operator's surge, outage, still-down report, update card and complaints, and a
    card of ours whose payload names their complaint: none of it may show or be told."""
    theirs_inc = incident(session, operator="airtel", priority="P2")
    theirs = complaint(session, theirs_inc, msisdn(800), operator="airtel")
    told_theirs = complaint(session, theirs_inc, msisdn(803), operator="airtel")
    told_theirs.told_incident_id, told_theirs.told_restored_at = theirs_inc.id, T0
    session.add(SupportSurgeRow(operator_id="airtel", place="rongai", region_code="NBI_W", status="open", open_place="rongai",
                                first_at=T0, last_at=T0, complaints=3, numbers=3))
    session.add(SupportStepRow(complaint_id=theirs.id, seq=1, agent="followup", action="still_down_reported", summary="x",
                               detail_json=json.dumps({"incident_id": theirs_inc.id}), at=T0))
    session.add(SupportStepRow(complaint_id=theirs.id, seq=2, agent="followup", action="told_restored", summary="x",
                               detail_json=json.dumps({"incident_id": theirs_inc.id}), at=T0))
    their_card = HitlTaskRow(incident_id=theirs_inc.id, operator_id="airtel", task_type=loop.CUSTOMER_UPDATE_TASK_TYPE, status="PENDING")
    their_card.proposed_payload = {}
    session.add(their_card)
    session.flush()
    session.add(SupportNoticeRow(operator_id="airtel", incident_id=theirs_inc.id, state="awaiting_approval",
                                 card_id=their_card.id, recipients=7))
    mine_inc = incident(session, priority="P2")
    mine = complaint(session, mine_inc, msisdn(801))
    restore(session, mine_inc)
    # Their complaint naming OUR incident's id (a bug or a forgery): it must not count as ours.
    complaint(session, mine_inc, msisdn(804), operator="airtel")
    session.commit()
    card = session.scalar(select(HitlTaskRow).where(HitlTaskRow.operator_id == OP, HitlTaskRow.task_type == loop.CUSTOMER_UPDATE_TASK_TYPE))
    row = session.scalar(select(OutboxRow).where(OutboxRow.hitl_task_id == card.id))
    payload = json.loads(row.payload_json)
    payload["complaint_ids"].append(theirs.id)  # a forged or buggy payload naming their complaint
    row.payload_json = json.dumps(payload)
    session.commit()

    assert surge.list_surges(session, OP) == [] and surge.region_surges(session, OP) == {}
    assert [o["incident_id"] for o in loop.outages(session, OP)] == [mine_inc.id]
    numbers = loop.loop_metrics(session, OP, now=T0 + timedelta(hours=1))
    assert numbers["surges"]["open"] == 0 and numbers["still_down_reports"] == 0
    assert (numbers["notices_waiting"], numbers["recipients_waiting"], numbers["told"]) == (1, 1, 0)
    assert (numbers["outages_with_complaints"], numbers["repeat_contacts"], numbers["waiting_to_hear"]) == (1, 0, 0)
    assert loop.incident_customers(session, mine_inc)["customers"] == 1
    assert [o["customers"] for o in loop.outages(session, OP)] == [1]
    assert loop.approve_customer_update(session, card, approved_by="DM", approved_at=T0 + timedelta(minutes=5)) == 1
    session.commit()
    session.refresh(theirs)
    session.refresh(mine)
    assert theirs.told_incident_id is None and mine.told_incident_id == mine_inc.id
    assert loop.incident_customers(session, mine_inc)["customers"] == 1
    their_unlinked = complaint(session, None, msisdn(802), place="kitale", status="answered", operator="airtel")
    session.commit()
    kitale = incident(session, site_name="Kitale Town BTS", site_type="BTS", region="RFT")
    session.commit()
    assert loop.late_link(session, kitale, ctx=CTX, now=T0) == 0
    session.refresh(their_unlinked)
    assert their_unlinked.linked_incident_id is None
