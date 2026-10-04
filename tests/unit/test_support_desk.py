"""The desk end to end: every route through ``process_complaint``, the trace, dedupe, repeats,
realtime events after commit, the human actions, and the public view."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from noc_agents.db.models import utcnow
from noc_agents.db.models_support import SupportComplaintRow, SupportToolCallRow
from noc_agents.realtime.hub import hub
from noc_agents.support import desk, views
from noc_agents.support.context import default_context
from noc_agents.support.evals import IsolatedDatabases
from noc_agents.support.vocab import AGENTS

OP = "safaricom"
CTX = default_context()


@pytest.fixture()
def session():
    databases = IsolatedDatabases(OP)
    with databases.session() as s:
        yield s
    databases.close()


@pytest.fixture()
def clean_hub():
    hub._history.clear()
    yield hub
    hub._history.clear()


def _file(session, body, msisdn, **kwargs):
    kwargs.setdefault("emit_events", False)
    return desk.process_complaint(session, operator_id=OP, body=body, msisdn=msisdn, ctx=CTX, **kwargs)


def _detail(session, row, **kwargs):
    return views.detail(session, row, **kwargs)


# ----------------------------------------------------------------------------- the routes


def test_a_resolver_case_is_answered_from_the_cited_article(session):
    row = _file(session, "How do I stop these premium sms messages? Nimechoka nazo.", "0711000202").complaint
    d = _detail(session, row)["complaint"]
    assert (d["route"], d["status"], d["outcome"]) == ("resolver", "answered", "auto_resolved")
    assert [c["article_id"] for c in d["citations"]] == ["KB-BILLING-PREMIUM-SMS"]
    assert d["reply"].startswith("Hi there,") and d["ref"] in d["reply"]
    assert d["escalation"] is None and d["linked_incident"] is None


def test_an_action_case_runs_the_tool_notes_the_ticket_and_says_what_was_done(session):
    row = _file(session, "I sent KES 1,500 to the wrong number, code SJK4H7QW2L, please reverse.", "0700000412",
                name="Wanjiku Kamau").complaint
    detail = _detail(session, row)
    c = detail["complaint"]
    assert (c["route"], c["status"], c["outcome"]) == ("action", "action_taken", "action_completed")
    assert [(t["tool"], t["status"]) for t in detail["tool_calls"]] == [
        ("lookup_account", "ok"), ("reverse_mpesa", "ok"), ("update_ticket", "ok")]
    assert c["reply"].startswith("Hi Wanjiku,") and "SJK4H7QW2L" in c["reply"] and "KES 1,500" in c["reply"]
    assert c["customer"] == {"name": "Wanjiku Kamau", "msisdn_masked": "+254 7•• ••• 412", "account_ref": "ACC-100412"}


def test_a_storm_outage_links_the_live_noc_incident_and_names_the_ticket(session):
    c = _detail(session, _file(session, "No network in Nakuru since morning", "0700001023").complaint)["complaint"]
    assert c["status"] == "action_taken"
    assert c["linked_incident"]["title"].endswith("Nakuru Rift HUB") and c["linked_incident"]["status"] == "AWAITING_VENDOR"
    assert c["linked_incident"]["incident_number"] in c["reply"] and "engineers are already working" in c["reply"]


def test_an_outage_with_no_open_incident_is_answered_and_the_search_is_on_record(session):
    detail = _detail(session, _file(session, "Hakuna network huku Mombasa tangu jana", "0700001023").complaint)
    assert detail["complaint"]["status"] == "answered"
    assert detail["complaint"]["citations"][0]["article_id"] == "KB-NETWORK-OUTAGE"
    assert [(t["tool"], t["result"]["found"]) for t in detail["tool_calls"]] == [("link_incident", False)]


def test_nothing_safe_to_act_on_falls_back_to_the_resolver(session):
    # A reversal with no code and no account: the desk asks for the code rather than guessing.
    c = _detail(session, _file(session, "I sent money to the wrong number yesterday, please reverse", "0711000500").complaint)["complaint"]
    assert (c["route"], c["status"]) == ("resolver", "answered")
    assert c["citations"][0]["article_id"] == "KB-MPESA-REVERSAL"


@pytest.mark.parametrize(
    "body,msisdn,reason,status",
    [
        ("Someone did a SIM swap on my line and withdrew my M-PESA", "0700001245", "fraud_or_sim_swap", "escalated"),
        ("Nimetuma 12000 kwa namba mbaya, code ni SHR2M9PL4Q. Rudisha.", "0700000118", "over_refund_limit", "awaiting_approval"),
        ("Hello, it is not working since yesterday", "0711000235", "low_confidence", "escalated"),
        ("How do I become an M-PESA agent and how much float do I need?", "0711000237", "not_grounded", "escalated"),
        ("My daily 1GB bundle was not applied even though I paid", "0700000456", "tool_failed", "escalated"),
        ("You people are THIEVES!! My Business 50GB bundle expired early, useless", "0700000890", "angry_high_value", "escalated"),
    ],
)
def test_escalations_carry_the_reason_and_an_honest_holding_reply(session, body, msisdn, reason, status):
    c = _detail(session, _file(session, body, msisdn).complaint)["complaint"]
    assert (c["route"], c["status"], c["outcome"]) == ("human", status, "escalated")
    assert c["escalation"]["reason_code"] == reason and c["escalation"]["claimed_by"] is None
    assert "passed to a member of our team" in c["reply"] and c["ref"] in c["reply"]


def test_a_call_over_its_limit_is_parked_for_approval_and_a_call_that_would_succeed_is_held(session):
    parked = _detail(session, _file(session, "Nimetuma 12000 kwa namba mbaya, code ni SHR2M9PL4Q.", "0700000118").complaint)
    assert [(t["tool"], t["status"]) for t in parked["tool_calls"]] == [("lookup_account", "ok"), ("reverse_mpesa", "needs_approval")]
    held = _detail(session, _file(session, "You are THIEVES!! My Business 50GB bundle expired early", "0700000890").complaint)
    assert [t["tool"] for t in held["tool_calls"]] == ["lookup_account"]  # the re-credit did NOT run
    assert held["steps"][-1]["detail"]["held_tool"] == "recredit_bundle"


def test_the_third_complaint_in_a_week_on_one_category_escalates(session):
    first = _file(session, "My calls keep dropping in town", "0711000600").complaint
    second = _file(session, "Calls keep dropping again today", "0711000600").complaint
    third = _file(session, "Still my calls keep dropping, third time", "0711000600").complaint
    assert (first.status, second.status) == ("answered", "answered")
    assert third.escalation_reason_code == "repeat_unresolved"


def test_the_fixtures_complaint_history_counts_towards_repeats(session):
    row = _file(session, "My calls keep dropping everywhere", "0700000901").complaint  # two network complaints on file
    assert row.escalation_reason_code == "repeat_unresolved"


# ------------------------------------------------------------------------------- the trace


def test_every_step_has_an_agent_a_summary_a_detail_and_a_duration(session):
    row = _file(session, "bundles zimeisha mapema sana", "0700000345").complaint
    steps = _detail(session, row)["steps"]
    assert [s["seq"] for s in steps] == list(range(1, len(steps) + 1))
    assert [s["agent"] for s in steps[:2]] == ["intake", "triage"]
    assert all(s["agent"] in AGENTS and s["summary"] and isinstance(s["detail"], dict) for s in steps)
    assert all(isinstance(s["duration_ms"], int) and s["duration_ms"] >= 0 for s in steps)
    triage_step = steps[1]["detail"]
    assert triage_step["category"] == "data_bundles" and triage_step["route"] == "action"


def test_the_customer_message_and_the_reply_are_both_in_the_conversation(session):
    row = _file(session, "My calls keep dropping in town", "0711000601", name="Achieng").complaint
    messages = _detail(session, row)["messages"]
    assert [(m["author"], m["name"]) for m in messages] == [("customer", "Achieng"), ("agent", "Support desk")]
    assert messages[1]["body"] == row.reply


def test_references_are_sequential_per_operator(session):
    refs = [_file(session, f"My calls keep dropping on street {n}", f"07110007{n:02d}").complaint.ref for n in range(3)]
    assert refs == ["CMP-000001", "CMP-000002", "CMP-000003"]


def test_the_subject_is_the_first_line_cut_at_a_word(session):
    long = "My calls keep dropping " + "every single afternoon near the market " * 5
    row = _file(session, long, "0711000800").complaint
    assert len(row.subject) <= 90 and row.subject.endswith("…")
    assert _file(session, "Calls dropping", "0711000801", subject="Calls").complaint.subject == "Calls"


def test_sla_follows_urgency(session):
    now = utcnow()
    critical = _file(session, "Someone did a SIM swap on my line", "0711000802", now=now).complaint
    low = _file(session, "How do I activate roaming?", "0711000803", now=now).complaint
    assert critical.sla_due_at - now == timedelta(hours=1)
    assert low.sla_due_at - now == timedelta(hours=72)


# ------------------------------------------------------------------------- intake rules


def test_an_identical_complaint_inside_two_minutes_returns_the_one_on_file(session):
    now = utcnow()
    first = _file(session, "My calls keep dropping", "0712345678", now=now)
    again = _file(session, "my calls  keep dropping!", "+254712345678", now=now + timedelta(seconds=90))
    later = _file(session, "My calls keep dropping", "0712345678", now=now + timedelta(seconds=150))
    assert first.created and not again.created and again.complaint.id == first.complaint.id
    assert later.created and later.complaint.id != first.complaint.id
    assert session.scalar(select(SupportComplaintRow.ref).order_by(SupportComplaintRow.ref.desc()).limit(1)) == "CMP-000002"


@pytest.mark.parametrize("body,msisdn,channel", [("hi", "0700000412", "web"), ("x" * 4001, "0700000412", "web"),
                                                 ("My calls drop", "12345", "web"), ("My calls drop", "0700000412", "fax")])
def test_bad_input_is_refused_before_anything_is_written(session, body, msisdn, channel):
    with pytest.raises(desk.DeskInputError):
        _file(session, body, msisdn, channel=channel)
    assert session.scalar(select(SupportComplaintRow)) is None


# ------------------------------------------------------------------------------ realtime


def test_events_leave_only_after_the_commit_and_carry_no_personal_data(session, clean_hub):
    row = _file(session, "Someone did a SIM swap on my line", "0700001245", emit_events=True, name="Ochieng").complaint
    events = [e for e in clean_hub.recent(50) if e["type"].startswith("support.")]
    assert [e["type"] for e in events] == ["support.created", "support.escalated"]
    assert events[1]["payload"]["reason_code"] == "fraud_or_sim_swap" and events[0]["payload"]["ref"] == row.ref
    flat = json.dumps(events)
    assert "Ochieng" not in flat and "0700001245" not in flat and "SIM swap" not in flat


def test_the_eval_path_emits_nothing(session, clean_hub):
    _file(session, "My calls keep dropping", "0711000900", emit_events=False)
    assert not [e for e in clean_hub.recent(50) if e["type"].startswith("support.")]


# -------------------------------------------------------------------------- human actions


def test_claim_then_resolve(session, clean_hub):
    row = _file(session, "Someone did a SIM swap on my line", "0700001245").complaint
    desk.claim(session, row, actor="Amina (NOC)")
    assert (row.status, row.claimed_by) == ("in_progress", "Amina (NOC)")
    with pytest.raises(desk.DeskConflict, match="already claimed"):
        desk.claim(session, row, actor="Brian")
    desk.resolve_case(session, row, actor="Amina (NOC)", reply="We blocked the swap and restored your line.", note="SIM re-issued")
    d = _detail(session, row)
    assert (d["complaint"]["status"], d["complaint"]["outcome"]) == ("resolved", "human_resolved")
    assert d["complaint"]["reply"] == "We blocked the swap and restored your line."
    assert [s["action"] for s in d["steps"][-2:]] == ["claimed", "resolved"]
    assert d["messages"][-1]["author"] == "staff"
    assert [e["type"] for e in clean_hub.recent(10)] == ["support.updated", "support.updated"]


def test_only_a_case_with_a_person_can_be_claimed_or_resolved(session):
    answered = _file(session, "How do I activate roaming?", "0711000901").complaint
    with pytest.raises(desk.DeskConflict):
        desk.claim(session, answered, actor="Amina")
    with pytest.raises(desk.DeskConflict):
        desk.resolve_case(session, answered, actor="Amina", reply="Done already.")


def _parked(session):
    row = _file(session, "Nimetuma 12000 kwa namba mbaya, code ni SHR2M9PL4Q.", "0700000118", name="Otieno").complaint
    call = session.scalar(select(SupportToolCallRow).where(SupportToolCallRow.complaint_id == row.id,
                                                           SupportToolCallRow.status == "needs_approval"))
    return row, call


def test_approving_runs_the_tool_with_the_approval_and_the_status_follows(session):
    row, call = _parked(session)
    desk.approve(session, row, call.id, actor="Duty Manager", ctx=CTX)
    d = _detail(session, row)
    assert (d["complaint"]["status"], d["complaint"]["outcome"]) == ("action_taken", "action_completed")
    approved = next(t for t in d["tool_calls"] if t["id"] == call.id)
    assert approved["status"] == "approved" and approved["decided_by"] == "Duty Manager" and approved["result"]["amount_kes"] == 12000
    assert d["tool_calls"][-1]["tool"] == "update_ticket"
    assert d["complaint"]["reply"].startswith("Hi Otieno, we have reversed")
    with pytest.raises(desk.DeskConflict):
        desk.approve(session, row, call.id, actor="Duty Manager", ctx=CTX)


def test_rejecting_sends_the_case_back_to_a_person(session):
    row, call = _parked(session)
    with pytest.raises(desk.DeskInputError):
        desk.reject(session, row, call.id, actor="DM", reason="  ")
    desk.reject(session, row, call.id, actor="DM", reason="recipient disputes it")
    assert row.status == "escalated" and row.escalation_reason_code == "over_refund_limit"
    assert session.get(SupportToolCallRow, call.id).policy == "rejected by DM: recipient disputes it"


def test_an_approval_that_can_no_longer_complete_escalates_with_tool_failed(session):
    row, call = _parked(session)
    first = _file(session, "Nimetuma 12000 kwa namba mbaya SHR2M9PL4Q, rudisha tafadhali", "0700000118").complaint
    other_call = session.scalar(select(SupportToolCallRow).where(SupportToolCallRow.complaint_id == first.id,
                                                                 SupportToolCallRow.status == "needs_approval"))
    desk.approve(session, first, other_call.id, actor="DM", ctx=CTX)  # reversed on the second ticket
    desk.approve(session, row, call.id, actor="DM", ctx=CTX)  # ...so the first can no longer run
    assert row.status == "escalated" and row.escalation_reason_code == "tool_failed"
    assert session.get(SupportToolCallRow, call.id).status == "refused"


def test_a_decision_on_a_call_from_another_complaint_is_not_found(session):
    row, _ = _parked(session)
    other = _file(session, "I have been charged KES 1,500 for betting tips I never subscribed to, refund", "0700000789").complaint
    other_call = session.scalar(select(SupportToolCallRow).where(SupportToolCallRow.complaint_id == other.id,
                                                                 SupportToolCallRow.status == "needs_approval"))
    for call_id in ("no-such-call", other_call.id):
        with pytest.raises(desk.DeskNotFound):
            desk.approve(session, row, call_id, actor="DM", ctx=CTX)
        with pytest.raises(desk.DeskNotFound):
            desk.reject(session, row, call_id, actor="DM", reason="no")


def test_resolving_supersedes_a_pending_approval(session):
    row, call = _parked(session)
    desk.resolve_case(session, row, actor="DM", reply="We called you and agreed a manual reversal.")
    assert session.get(SupportToolCallRow, call.id).status == "rejected"


# ---------------------------------------------------------------------------- the views


def test_the_public_view_blanks_everything_derived_from_the_account(session):
    row = _file(session, "I sent KES 1,500 to the wrong number, code SJK4H7QW2L, please reverse.", "0700000412").complaint
    public = _detail(session, row, public=True)
    assert public["complaint"]["customer"] == {"name": None, "msisdn_masked": "+254 7•• ••• 412", "account_ref": None}
    assert all(s["detail"] == {} for s in public["steps"])
    assert all(t["args"] == {} and t["result"] is None for t in public["tool_calls"])
    assert "Wanjiku" not in json.dumps(public) and "Brian" not in json.dumps(public)
    assert public["complaint"]["reply"] == row.reply  # the customer still gets the answer
    staff = _detail(session, row)
    assert staff["complaint"]["customer"]["name"] == "Wanjiku Kamau" and staff["tool_calls"][0]["result"]["found"]


def test_the_queue_filters_searches_and_counts(session):
    _file(session, "How do I activate roaming?", "0711001001")
    _file(session, "Someone did a SIM swap on my line", "0711001002")
    _file(session, "No network in Nakuru since morning", "0711001003")
    queue = views.list_complaints(session, OP)
    assert [i["ref"] for i in queue["items"]] == ["CMP-000003", "CMP-000002", "CMP-000001"]
    assert queue["counts"]["by_route"] == {"resolver": 1, "human": 1, "action": 1}
    assert [i["category"] for i in views.list_complaints(session, OP, status="escalated")["items"]] == ["sim_and_fraud"]
    assert [i["ref"] for i in views.list_complaints(session, OP, q="nakuru")["items"]] == ["CMP-000003"]
    assert views.list_complaints(session, OP, route="action", category="roaming")["items"] == []
    assert views.list_complaints(session, "airtel")["items"] == []  # operator-scoped


def test_metrics(session):
    now = utcnow()
    _file(session, "How do I activate roaming?", "0711001101", now=now)
    _file(session, "I sent KES 1,500 to the wrong number, code SJK4H7QW2L, please reverse.", "0700000412", now=now)
    _file(session, "Someone did a SIM swap on my line", "0711001102", now=now)
    _file(session, "Hello, it is not working", "0711001103", now=now - timedelta(hours=30))
    m = views.metrics(session, OP, hours=0, now=now)
    assert (m["total"], m["auto_resolved"], m["action_completed"], m["escalated"]) == (4, 1, 1, 2)
    assert m["resolution_rate"] == 0.5 and m["escalation_rate"] == 0.5
    assert m["by_category"]["roaming"] == 1 and set(m["by_category"]) >= {"network", "other"}
    assert views.metrics(session, OP, hours=24, now=now)["total"] == 3
    assert views.metrics(session, "airtel", hours=0, now=now)["total"] == 0
