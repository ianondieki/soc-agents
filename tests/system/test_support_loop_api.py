"""Close the loop over HTTP (docs/CLOSE_THE_LOOP.md): the NOC's restore, note and close routes telling
customers, the two cards on Approvals, the public Track page and its still-down button, surges into
tickets, the loop's read routes -- and the failure and race properties: a support failure never
blocks the restore or the close, and two simultaneous approvals send once.

One module-scoped app and database, so every test uses its own place (a site named after it beats
every region-only match in ``link_incident``) and its own numbers. Places are spread so the region
of a test that leaves an incident open is never one a surge test needs quiet: CST, MTK and RFT for
the NOC hooks, WNY for the surges.
"""

from __future__ import annotations

import importlib
import itertools
import json
import re
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from noc_agents.api import auth
from noc_agents.db.models import HitlTaskRow, IncidentRow, OutboxRow, WorkNoteRow, get_session
from noc_agents.db.models_support import SupportComplaintRow, SupportNoticeRow, SupportStepRow, SupportSurgeRow
from noc_agents.orchestrator import outbox
from noc_agents.realtime.hub import hub
from noc_agents.support import loop
from noc_agents.support.ratelimit import complaint_limiter

BASE = "/api/v1/support"
TRACKED_KEYS = {"ref", "stage", "headline", "detail", "received_at", "reply_due_at", "outage", "timeline", "messages",
                "can_report_still_down"}
_numbers = itertools.count(1)
JOIN_TIMEOUT_S = 60


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    db = tmp_path_factory.mktemp("support_loop") / "loop.db"
    mp.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    mp.setenv("OPERATOR_PROFILE", "safaricom")
    mp.setenv("AUTH_DISABLED", "true")
    mp.setenv("NOC_ENV", "demo")
    mp.setenv("SUPPORT_DESK_ENABLED", "true")

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
        importlib.reload(main)
        mp.undo()
        cfg.clear_settings_cache()


@pytest.fixture(autouse=True)
def fresh_limiter():
    complaint_limiter.reset()
    yield
    complaint_limiter.reset()


# ------------------------------------------------------------------------------- helpers


def number() -> str:
    return f"0745{next(_numbers):06d}"


def e164(local: str) -> str:
    return "+254" + local[1:]


def incident(client, place: str, *, region: str, users: int = 60000) -> dict:
    """An open incident on a site named after ``place``: P3 at 60,000 users, P2 at 150,000."""
    r = client.post("/api/v1/events", json={
        "site_id": f"LOOP-{place.upper()}-{users}", "site_name": f"{place.title()} Loop BTS", "site_type": "BTS",
        "region_code": region, "alarm_code": "SITE_DOWN", "failure_domain": "POWER", "users_affected": users})
    assert r.status_code == 200, r.text
    return r.json()["incident"]


def complain(client, text: str, msisdn: str | None = None, **extra) -> dict:
    r = client.post(f"{BASE}/complaints", json={"body": text, "msisdn": msisdn or number(), **extra})
    assert r.status_code == 201, r.text
    return r.json()


def outage_complaint(client, place: str, inc: dict, msisdn: str | None = None, *, sw: bool = False) -> dict:
    text = (f"Hakuna mtandao {place.title()} tangu asubuhi, siwezi kupiga simu." if sw
            else f"No network in {place.title()} since morning, calls keep failing.")
    detail = complain(client, text, msisdn)
    linked = detail["complaint"]["linked_incident"]
    assert linked and linked["id"] == inc["id"], (place, linked)
    return detail["complaint"]


def read(fn):
    session = get_session()
    try:
        return fn(session)
    finally:
        session.close()


def restore_rows(incident_id: str) -> list[tuple[str, str, int, str | None]]:
    return read(lambda s: [(r.idempotency_key, r.status, r.requires_hitl, r.approved_by) for r in s.scalars(
        select(OutboxRow).where(OutboxRow.idempotency_key.like(f"support-restore:{incident_id}:%")))])


def complaint_row(complaint_id: str) -> SupportComplaintRow:
    return read(lambda s: s.get(SupportComplaintRow, complaint_id))


def cards(client, task_type: str, incident_id: str | None = None) -> list[dict]:
    return [t for t in client.get("/api/v1/hitl/pending").json()
            if t["task_type"] == task_type and (incident_id is None or t["incident_id"] == incident_id)]


def events(kind: str) -> list[dict]:
    return [e for e in hub._history if e["type"] == kind]


def restore(client, inc: dict):
    r = client.post(f"/api/v1/incidents/{inc['id']}/restore", json={"note": "Service restored after the site fix"})
    assert r.status_code == 200, r.text
    return r


def close(client, inc: dict):
    r = client.post(f"/api/v1/incidents/{inc['id']}/close", json={"closed_by": "NOC", "resolution_summary": "done"})
    assert r.status_code == 200, r.text
    return r


def track(client, ref: str, msisdn: str, path: str = "/track", **extra):
    return client.post(f"{BASE}{path}", json={"ref": ref, "msisdn": msisdn, **extra})


# ============================================================ 1. the NOC's routes tell customers


def test_restoring_a_p3_tells_its_customers_at_once(client):
    inc = incident(client, "kilifi", region="CST")
    a, b = number(), number()
    first = outage_complaint(client, "kilifi", inc, a)
    second = outage_complaint(client, "kilifi", inc, b, sw=True)
    restore(client, inc)
    rows = restore_rows(inc["id"])
    assert len(rows) == 2 and {(status, hitl) for _k, status, hitl, _a in rows} == {(outbox.SENT, 0)}  # the mock adapter
    for c in (first, second):
        row = complaint_row(c["id"])
        assert (row.status, row.closure_reason, row.told_incident_id) == ("closed", "service_restored", inc["id"])
    detail = client.get(f"{BASE}/complaints/{second['id']}").json()
    assert detail["messages"][-1]["body"].startswith(f"Huduma imerejea Kilifi. Lalamiko lako {second['ref']} limefungwa.")
    assert detail["steps"][-1]["agent"] == "followup" and detail["steps"][-1]["action"] == "told_restored"
    told = [e for e in events("support.customers_told") if e["payload"]["incident_number"] == inc["incident_number"]]
    assert told and told[-1]["payload"] == {"incident_number": inc["incident_number"], "count": 2}


def test_restoring_a_p2_raises_the_card_and_approving_it_tells(client):
    inc = incident(client, "malindi", region="CST", users=150000)
    complaints = [outage_complaint(client, "malindi", inc) for _ in range(2)]
    restore(client, inc)
    [card] = cards(client, "APPROVE_CUSTOMER_UPDATE", inc["id"])
    assert card["proposed_payload"]["kind"] == "restore_notice" and card["proposed_payload"]["recipients"] == 2
    assert card["incident_number"] == inc["incident_number"]
    assert {(status, hitl) for _k, status, hitl, _a in restore_rows(inc["id"])} == {(outbox.HELD, 1)}
    assert all(complaint_row(c["id"]).told_restored_at is None for c in complaints)
    assert any(e["payload"].get("task_id") == card["id"] for e in events("hitl.created"))
    # While the card waits, the Track page does not say "it's back" either.
    waiting = complaint_row(complaints[0]["id"])
    page = track(client, waiting.ref, "0" + waiting.msisdn[4:]).json()
    assert page["stage"] == "outage_known" and page["outage"]["state"] == "working" and not page["can_report_still_down"]
    r = client.post(f"/api/v1/hitl/{card['id']}/approve", json={"resolved_by": "Duty Manager"})
    assert r.status_code == 200, r.text
    assert {(status, approver) for _k, status, _h, approver in restore_rows(inc["id"])} == {(outbox.SENT, "Duty Manager")}
    assert all(complaint_row(c["id"]).status == "closed" for c in complaints)
    assert client.post(f"/api/v1/hitl/{card['id']}/approve", json={"resolved_by": "Duty Manager"}).status_code == 409
    notes = read(lambda s: [n.body for n in s.scalars(select(WorkNoteRow).where(
        WorkNoteRow.incident_id == inc["id"], WorkNoteRow.source == "hitl"))])
    assert "HITL approved the customer update: 2 customer(s) told service is back." in notes
    panel = client.get(f"{BASE}/incidents/{inc['id']}/customers").json()
    assert (panel["told"], panel["waiting"], panel["notice"]["state"]) == (2, 0, "sent")


def test_rejecting_the_card_suppresses_tells_nobody_and_the_close_cannot_resend(client):
    inc = incident(client, "lamu", region="CST", users=150000)
    c = outage_complaint(client, "lamu", inc)
    restore(client, inc)
    [card] = cards(client, "APPROVE_CUSTOMER_UPDATE", inc["id"])
    r = client.post(f"/api/v1/hitl/{card['id']}/reject", json={"resolved_by": "Duty Manager", "reason": "site flapping"})
    assert r.status_code == 200, r.text
    assert [status for _k, status, _h, _a in restore_rows(inc["id"])] == [outbox.SUPPRESSED]
    assert complaint_row(c["id"]).told_restored_at is None
    close(client, inc)
    assert [status for _k, status, _h, _a in restore_rows(inc["id"])] == [outbox.SUPPRESSED]
    assert cards(client, "APPROVE_CUSTOMER_UPDATE", inc["id"]) == []
    row = next(o for o in client.get(f"{BASE}/outages").json() if o["incident_id"] == inc["id"])
    assert row["notice"]["state"] == "rejected" and row["told"] == 0 and row["waiting"] == 1


def test_a_mark_restored_note_tells_and_a_note_that_merely_says_restored_does_not(client):
    inc = incident(client, "diani", region="CST")
    c = outage_complaint(client, "diani", inc)
    note = {"author": "MSP Lead", "author_role": "MSP", "body": "Power restored at the site, monitoring."}
    assert client.post(f"/api/v1/incidents/{inc['id']}/notes", json=note).status_code == 200
    assert read(lambda s: s.get(IncidentRow, inc["id"]).restored_source) == "VENDOR_NOTE_INFERRED"
    assert restore_rows(inc["id"]) == [] and complaint_row(c["id"]).told_restored_at is None
    marked = {**note, "body": "Confirmed on site.", "mark_restored": True}
    assert client.post(f"/api/v1/incidents/{inc['id']}/notes", json=marked).status_code == 200
    assert len(restore_rows(inc["id"])) == 1 and complaint_row(c["id"]).status == "closed"
    # Ticking it again changes nothing.
    assert client.post(f"/api/v1/incidents/{inc['id']}/notes", json=marked).status_code == 200
    assert len(restore_rows(inc["id"])) == 1


def test_the_close_tells_after_a_guess_and_a_second_restore_or_close_never_sends_again(client):
    guessed = incident(client, "nyali", region="CST")
    c = outage_complaint(client, "nyali", guessed)
    client.post(f"/api/v1/incidents/{guessed['id']}/notes", json={"author_role": "MSP", "body": "Site restored."})
    assert restore_rows(guessed["id"]) == []
    close(client, guessed)
    assert len(restore_rows(guessed["id"])) == 1 and complaint_row(c["id"]).status == "closed"
    notice = read(lambda s: s.scalar(select(SupportNoticeRow).where(SupportNoticeRow.incident_id == guessed["id"])))
    assert notice.restore_source == "CLOSED"
    again = incident(client, "bamburi", region="CST")
    outage_complaint(client, "bamburi", again)
    restore(client, again)
    restore(client, again)  # RESTORED is not terminal: a second restore is allowed, and sends nothing
    close(client, again)
    close(client, again)
    assert len(restore_rows(again["id"])) == 1


def test_a_support_failure_never_blocks_the_restore_or_the_close(client, monkeypatch):
    inc = incident(client, "shanzu", region="CST")
    c = outage_complaint(client, "shanzu", inc)

    def broken(*_args, **_kwargs):
        raise RuntimeError("support is down")

    monkeypatch.setattr(loop, "on_incident_restored", broken)
    hub._history.clear()
    r = client.post(f"/api/v1/incidents/{inc['id']}/restore", json={"note": "Back"})
    assert r.status_code == 200 and r.json()["incident"]["status"] == "RESTORED"
    assert read(lambda s: s.get(IncidentRow, inc["id"]).status) == "RESTORED"
    assert restore_rows(inc["id"]) == [] and complaint_row(c["id"]).told_restored_at is None
    r = client.post(f"/api/v1/incidents/{inc['id']}/close", json={"closed_by": "NOC"})
    assert r.status_code == 200 and read(lambda s: s.get(IncidentRow, inc["id"]).status) == "CLOSED"
    # The close's own realtime event survives the support savepoint's rollback.
    assert any(e["incident_id"] == inc["id"] for e in events("incident.closed"))


def test_a_support_failure_half_way_rolls_back_only_the_support_work(client, monkeypatch):
    inc = incident(client, "tudor", region="CST")
    complaints = [outage_complaint(client, "tudor", inc) for _ in range(2)]
    real = outbox.enqueue
    calls = []

    def second_fails(*args, **kwargs):
        calls.append(kwargs.get("idempotency_key"))
        if len(calls) == 2:
            raise RuntimeError("outbox full")
        return real(*args, **kwargs)

    monkeypatch.setattr(outbox, "enqueue", second_fails)
    restore(client, inc)
    assert len(calls) == 2  # the first customer was told inside the savepoint ...
    assert restore_rows(inc["id"]) == []  # ... and rolled back with it
    assert all(complaint_row(c["id"]).told_restored_at is None for c in complaints)
    assert read(lambda s: s.get(IncidentRow, inc["id"]).status) == "RESTORED"
    assert read(lambda s: s.scalar(select(WorkNoteRow).where(WorkNoteRow.incident_id == inc["id"],
                                                             WorkNoteRow.source == "restore"))) is not None


def test_the_desk_switched_off_tells_nobody(client, monkeypatch):
    inc = incident(client, "changamwe", region="CST")
    c = outage_complaint(client, "changamwe", inc)
    monkeypatch.setenv("SUPPORT_DESK_ENABLED", "false")
    restore(client, inc)
    assert restore_rows(inc["id"]) == [] and complaint_row(c["id"]).told_restored_at is None


# ================================================================================= 2. the race


def test_two_simultaneous_approvals_of_one_customer_update_send_once(client, monkeypatch):
    inc = incident(client, "kwale", region="CST", users=150000)
    complaints = [outage_complaint(client, "kwale", inc) for _ in range(3)]
    restore(client, inc)
    [card] = cards(client, "APPROVE_CUSTOMER_UPDATE", inc["id"])
    sent: list[str] = []
    guard = threading.Lock()
    real = outbox._TRANSMITTERS[outbox.SMS]

    def spy(row):
        with guard:
            sent.append(row.idempotency_key)
        return real(row)

    monkeypatch.setitem(outbox._TRANSMITTERS, outbox.SMS, spy)
    barrier = threading.Barrier(2)
    codes: list[int] = []

    def approve(name: str):
        def run():
            barrier.wait(JOIN_TIMEOUT_S)
            r = client.post(f"/api/v1/hitl/{card['id']}/approve", json={"resolved_by": name})
            with guard:
                codes.append(r.status_code)
        return run

    threads = [threading.Thread(target=approve(n), daemon=True) for n in ("Supervisor A", "Supervisor B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(JOIN_TIMEOUT_S)
    assert not any(t.is_alive() for t in threads), "deadlock"
    assert sorted(codes) == [200, 409], codes
    mine = [key for key in sent if key.startswith(f"support-restore:{inc['id']}:")]
    assert len(mine) == 3 and len(set(mine)) == 3, mine  # each number transmitted exactly once
    for c in complaints:
        told = read(lambda s: s.scalars(select(SupportStepRow).where(
            SupportStepRow.complaint_id == c["id"], SupportStepRow.action == "told_restored")).all())
        assert len(told) == 1, c["ref"]


# ============================================================================== 3. the Track page


def test_track_answers_the_same_404_for_every_mismatch(client):
    inc = incident(client, "meru", region="MTK")
    mine = number()
    c = outage_complaint(client, "meru", inc, mine)
    read(lambda s: (s.add(SupportComplaintRow(operator_id="airtel", ref="CMP-990001", msisdn=e164(mine),
                                              msisdn_masked="x", body="x", body_hash="x")), s.commit()))
    expected = {"detail": "We could not find a complaint with that reference and number."}
    bad = [
        {"ref": c["ref"], "msisdn": number()},  # the wrong number
        {"ref": "CMP-999999", "msisdn": mine},  # the wrong reference
        {"ref": "CMP-990001", "msisdn": mine},  # another operator's complaint, right number
        {"ref": c["ref"]}, {"msisdn": mine}, {"ref": 7, "msisdn": mine}, {"ref": c["ref"], "msisdn": 712345678},
        {"ref": c["ref"], "msisdn": "12345"}, {"ref": "", "msisdn": mine},
    ]
    for body in bad:
        r = client.post(f"{BASE}/track", json=body)
        assert (r.status_code, r.json()) == (404, expected), body
    for raw in (b"", b"not json", b"[1, 2]", b'"CMP-000001"', b"{" + b" " * 5000 + b"}"):
        r = client.post(f"{BASE}/track", content=raw, headers={"content-type": "application/json"})
        assert (r.status_code, r.json()) == (404, expected), raw[:20]
        r = client.post(f"{BASE}/track/still-down", content=raw, headers={"content-type": "application/json"})
        assert (r.status_code, r.json()) == (404, expected), raw[:20]
    # POST only: the number never sits in a URL or an access log.
    assert client.get(f"{BASE}/track", params={"ref": c["ref"], "msisdn": mine}).status_code != 200
    from noc_agents.api.routers import support as support_routes

    assert {m for r in support_routes._lane.routes if r.path in (f"{BASE}/track", f"{BASE}/track/still-down")
            for m in r.methods} == {"POST"}
    complaint_limiter.reset()  # the twenty-odd refusals above used up this address's allowance
    ok = track(client, c["ref"], mine)
    assert ok.status_code == 200 and set(ok.json()) == TRACKED_KEYS
    # The number in any spelling the form accepts.
    assert track(client, c["ref"], "+254 " + mine[1:4] + " " + mine[4:]).status_code == 200


def test_track_shows_customer_words_only(client):
    """An account-derived escalation and a staff reply: the Track page names neither the account
    holder, nor the account, nor the staff member, nor a policy line or a reason code."""
    text = "I have been charged KES 1,500 for a betting tips subscription I never subscribed to. I want a refund."
    detail = complain(client, text, "0700000789")
    c = detail["complaint"]
    assert c["status"] == "awaiting_approval" and c["escalation"]["reason_code"] == "over_refund_limit"
    page = track(client, c["ref"], "0700000789").json()
    assert page["stage"] == "with_a_person" and page["reply_due_at"] is not None
    assert "we need to check some account details" in page["detail"]
    assert client.post("/api/v1/session", json={"display_name": "Agent Wekesa", "role": "shift_supervisor"}).status_code == 200
    try:
        assert client.post(f"{BASE}/complaints/{c['id']}/claim").status_code == 200
        reply = "We have refunded the subscription charge and blocked premium SMS on your line."
        resolved = client.post(f"{BASE}/complaints/{c['id']}/resolve", json={"reply": reply})
        assert resolved.status_code == 200 and resolved.json()["complaint"]["escalation"]["claimed_by"] == "Agent Wekesa"
    finally:
        client.cookies.clear()
    page = track(client, c["ref"], "0700000789").json()
    dumped = json.dumps(page)
    for secret in ("Omondi", "ACC-100789", "over_refund_limit", "account_review", "needs_approval", "issue_refund",
                   "policy", "auto limit", "Agent Wekesa", "CHG-789", "+254"):
        assert secret not in dumped, secret
    assert page["stage"] == "fixed" and {m["from"] for m in page["messages"]} == {"you", "us"}
    assert page["messages"][-1]["body"] == reply and set(page["messages"][0]) == {"at", "from", "body"}


def test_track_follows_the_complaint_from_outage_to_restored_to_still_down(client):
    inc = incident(client, "nyeri", region="MTK")
    mine = number()
    c = outage_complaint(client, "nyeri", inc, mine)
    page = track(client, c["ref"], mine).json()
    assert page["stage"] == "outage_known" and page["headline"] == "Engineers are working on the outage in Nyeri"
    assert page["outage"] == {"place": "Nyeri", "ticket": inc["incident_number"], "state": "working", "restored_at": None}
    assert page["can_report_still_down"] is False
    refused = track(client, c["ref"], mine, "/track/still-down")
    assert refused.status_code == 409  # not told yet: the outage is known and the SMS will come
    assert refused.json()["detail"] == (f"We already know about the outage (ticket {inc['incident_number']}); "
                                        "we will tell you by SMS when service is back.")
    restore(client, inc)
    page = track(client, c["ref"], mine).json()
    assert page["stage"] == "restored" and page["headline"] == "Service is back in Nyeri"
    assert page["outage"]["state"] == "restored" and page["outage"]["restored_at"] and page["can_report_still_down"]
    texts = [t["text"] for t in page["timeline"]]
    assert texts[0] == "We received your complaint." and "Service was restored." in texts
    assert texts[-1] == "We told you by SMS that service is back, and closed your complaint."
    assert [t["at"] for t in page["timeline"]] == sorted(t["at"] for t in page["timeline"])
    hub._history.clear()
    down = track(client, c["ref"], mine, "/track/still-down", note="Bado hakuna kitu huku")
    assert down.status_code == 200, down.text
    page = down.json()
    assert page["stage"] == "with_a_person" and page["can_report_still_down"] is False
    assert "you told us service is still down, so a person will check it" in page["detail"]
    assert any(m == {"at": m["at"], "from": "you", "body": "Bado hakuna kitu huku"} for m in page["messages"])
    [event] = events("support.still_down")
    assert event["payload"] == {"ref": c["ref"], "incident_number": inc["incident_number"], "place": "Nyeri"}
    again = track(client, c["ref"], mine, "/track/still-down")
    assert again.status_code == 409 and again.json()["detail"].startswith("You already told us")
    row = complaint_row(c["id"])
    assert (row.status, row.escalation_reason_code, row.claimed_by) == ("escalated", "still_down_after_restore", None)
    notes = read(lambda s: [n.body for n in s.scalars(select(WorkNoteRow).where(
        WorkNoteRow.incident_id == inc["id"], WorkNoteRow.source == "support"))])
    assert notes == [f"Customer {c['ref']} reports service is still down in Nyeri after the restore (1 of 1 told)"]
    too_long = track(client, c["ref"], mine, "/track/still-down", note="x" * 1001)
    assert too_long.status_code == 404


def test_track_is_rate_limited_per_reference_and_per_address(client):
    inc = incident(client, "embu", region="MTK")
    c = outage_complaint(client, "embu", inc)
    complaint_limiter.reset()
    for _ in range(10):
        assert track(client, c["ref"], number()).status_code == 404
    blocked = track(client, c["ref"], number())
    assert blocked.status_code == 429 and int(blocked.headers["Retry-After"]) >= 1
    complaint_limiter.reset()
    for n in range(20):
        assert track(client, f"CMP-8{n:05d}", number()).status_code == 404
    assert track(client, "CMP-800099", number()).status_code == 429
    assert client.post(f"{BASE}/track", content=b"junk").status_code == 429  # malformed requests count too


# ================================================================================== 4. surges


def _burst(client, place: str, count: int = 3) -> list[dict]:
    out = []
    for _ in range(count):
        detail = complain(client, f"No network in {place.title()} since 2pm, calls are not going through.")
        assert detail["complaint"]["linked_incident"] is None, place
        out.append(detail["complaint"])
    return out


def _surge_for(client, place: str) -> dict:
    return next(s for s in client.get(f"{BASE}/surges").json() if s["place"] == place)


def test_three_complaints_about_a_quiet_place_raise_a_card_and_confirming_opens_a_ticket(client):
    hub._history.clear()
    complaints = _burst(client, "kisii")
    [card] = [t for t in cards(client, "CONFIRM_POSSIBLE_OUTAGE") if t["proposed_payload"].get("place") == "Kisii"]
    assert card["incident_id"] is None and card["proposed_payload"]["complaints"] == 3
    surge = _surge_for(client, "Kisii")
    assert (surge["status"], surge["origin"], surge["region_code"], surge["card_id"]) == ("open", "complaints", "WNY", card["id"])
    assert set(surge["complaint_refs"]) == {c["ref"] for c in complaints}
    assert any(e["payload"] == {"surge_id": surge["id"], "place": "Kisii", "complaints": 3} for e in events("support.surge"))
    region = next(r for r in client.get("/api/v1/dashboard/regions").json()["regions"] if r["region_code"] == "WNY")
    assert region["complaint_surge"]["surge_id"] == surge["id"] and region["complaint_surge"]["numbers"] == 3
    assert client.post(f"/api/v1/hitl/{card['id']}/approve", json={"resolved_by": "Duty Manager"}).status_code == 200
    surge = _surge_for(client, "Kisii")
    assert surge["status"] == "confirmed" and surge["incident_number"] and surge["error"] is None
    inc = read(lambda s: s.get(IncidentRow, surge["incident_id"]))
    assert (inc.site_id, inc.site_name, inc.alarm_code, inc.failure_domain) == (
        "CUST-WNY-KISII", "Kisii (customer reports)", "CUSTOMER_REPORTED_OUTAGE", "UNKNOWN")
    assert re.fullmatch(r"3 customers reported no service in Kisii (between \d\d:\d\d and \d\d:\d\d|at \d\d:\d\d); "
                        r"no network alarm", inc.description), inc.description
    for c in complaints:
        detail = client.get(f"{BASE}/complaints/{c['id']}").json()
        assert detail["complaint"]["linked_incident"]["incident_number"] == inc.incident_number
        assert detail["steps"][-1]["action"] == "linked_confirmed_outage"
        assert detail["messages"][-1]["body"] == (f"We have confirmed an outage in Kisii (ticket {inc.incident_number}). "
                                                  "Engineers are on it; we will tell you when service is back.")
    rows = read(lambda s: [(r.status, r.approved_by) for r in s.scalars(select(OutboxRow).where(
        OutboxRow.idempotency_key.like(f"support-confirmed:{inc.id}:%")))])
    assert rows == [(outbox.SENT, "Duty Manager")] * 3
    outage = next(o for o in client.get(f"{BASE}/outages").json() if o["incident_id"] == inc.id)
    assert outage["from_customer_reports"] is True and outage["customers"] == 3
    assert client.get(f"{BASE}/loop").json()["spotted_by_customers"] >= 1
    region = next(r for r in client.get("/api/v1/dashboard/regions").json()["regions"] if r["region_code"] == "WNY")
    assert region["complaint_surge"] is None
    assert inc.parent_incident_id is None
    # Confirming again (a double call, a retry racing a success) opens nothing and writes nothing.
    from noc_agents.support import surge as surge_service

    notes_before = read(lambda s: len(s.scalars(select(WorkNoteRow).where(WorkNoteRow.incident_id == inc.id)).all()))
    again = surge_service.confirm_surge(surge["id"], actor="Someone Else", approved_at=inc.created_at)
    assert again.incident_id == inc.id
    assert read(lambda s: len(s.scalars(select(WorkNoteRow).where(WorkNoteRow.incident_id == inc.id)).all())) == notes_before
    assert read(lambda s: s.scalar(select(IncidentRow.id).where(IncidentRow.site_id == "CUST-WNY-KISII",
                                                                IncidentRow.id != inc.id))) is None
    later = complain(client, "No network in Kisii again this evening, calls drop.")["complaint"]
    assert later["linked_incident"]["id"] == inc.id  # a later complaint about Kisii joins the open ticket
    close(client, {"id": inc.id})  # leave WNY quiet for the next surge test (the close tells them: fine)


def test_a_failed_ingest_keeps_the_surge_confirmed_with_the_error_and_retry_opens_the_ticket(client, monkeypatch):
    import noc_agents.graph.pipeline as pipeline

    _burst(client, "bungoma")
    [card] = [t for t in cards(client, "CONFIRM_POSSIBLE_OUTAGE") if t["proposed_payload"].get("place") == "Bungoma"]

    def down(*_args, **_kwargs):
        raise RuntimeError("ingest down")

    monkeypatch.setattr(pipeline, "process_event", down)
    assert client.post(f"/api/v1/hitl/{card['id']}/approve", json={"resolved_by": "Duty Manager"}).status_code == 200
    surge = _surge_for(client, "Bungoma")
    assert (surge["status"], surge["incident_id"], surge["error"]) == ("confirmed", None, "RuntimeError: ingest down")
    assert read(lambda s: s.get(HitlTaskRow, card["id"]).status) == "APPROVED"
    late = _burst(client, "bungoma", 1)[0]  # still collecting: the retry will link it too
    assert _surge_for(client, "Bungoma")["complaints"] == 4
    monkeypatch.undo()
    r = client.post(f"{BASE}/surges/{surge['id']}/retry")
    assert r.status_code == 200, r.text
    assert r.json()["incident_number"] and r.json()["error"] is None
    assert client.get(f"{BASE}/complaints/{late['id']}").json()["complaint"]["linked_incident"]["id"] == r.json()["incident_id"]
    assert client.post(f"{BASE}/surges/{surge['id']}/retry").status_code == 409
    assert client.post(f"{BASE}/surges/does-not-exist/retry").status_code == 404
    close(client, {"id": r.json()["incident_id"]})


def test_dismissing_a_surge_leaves_the_complaints_as_they_were(client):
    complaints = _burst(client, "busia")
    [card] = [t for t in cards(client, "CONFIRM_POSSIBLE_OUTAGE") if t["proposed_payload"].get("place") == "Busia"]
    assert client.post(f"/api/v1/hitl/{card['id']}/reject",
                       json={"resolved_by": "Duty Manager", "reason": "known fibre cut"}).status_code == 200
    surge = _surge_for(client, "Busia")
    assert (surge["status"], surge["reason"], surge["decided_by"]) == ("dismissed", "known fibre cut", "Duty Manager")
    assert all(complaint_row(c["id"]).linked_incident_id is None for c in complaints)
    _burst(client, "busia", 1)
    assert [t for t in cards(client, "CONFIRM_POSSIBLE_OUTAGE") if t["proposed_payload"].get("place") == "Busia"] == []


def test_still_down_reports_raise_a_card_and_the_new_ticket_stays_top_level_and_catches_later_complaints(client):
    inc = incident(client, "kericho", region="RFT")
    pairs = [(number(), None) for _ in range(2)]
    pairs = [(m, outage_complaint(client, "kericho", inc, m)) for m, _ in pairs]
    restore(client, inc)
    for m, c in pairs:
        assert track(client, c["ref"], m, "/track/still-down").status_code == 200
    [card] = [t for t in cards(client, "CONFIRM_POSSIBLE_OUTAGE") if t["proposed_payload"].get("place") == "Kericho"]
    payload = card["proposed_payload"]
    assert (payload["origin"], payload["parent_incident_number"], payload["numbers"]) == ("still_down", inc["incident_number"], 2)
    assert client.post(f"/api/v1/hitl/{card['id']}/approve", json={"resolved_by": "Duty Manager"}).status_code == 200
    surge = _surge_for(client, "Kericho")
    new = read(lambda s: s.get(IncidentRow, surge["incident_id"]))
    # A NEW ticket, and a top-level one: the relation to the restored incident is on the surge and in
    # a work note on both incidents, never parent_incident_id (link_incident skips child tickets).
    assert new.id != inc["id"] and new.parent_incident_id is None
    assert read(lambda s: s.get(SupportSurgeRow, surge["id"]).parent_incident_id) == inc["id"]
    after = (f"after {inc['incident_number']} was restored; 2 customers say service is still down in Kericho "
             "(confirmed by Duty Manager).")
    notes = {incident_id: read(lambda s, i=incident_id: [n.body for n in s.scalars(select(WorkNoteRow).where(
        WorkNoteRow.incident_id == i, WorkNoteRow.source == "support"))]) for incident_id in (new.id, inc["id"])}
    assert f"Opened from customer reports {after}" in notes[new.id]
    assert f"{new.incident_number} opened from customer reports {after}" in notes[inc["id"]]
    m, c = pairs[0]
    page = track(client, c["ref"], m).json()
    assert page["outage"]["ticket"] == new.incident_number and page["outage"]["state"] == "working"
    assert page["can_report_still_down"] is False  # the new outage is already known
    # The loop stays closed: a later complaint about Kericho links to the open customer-reports ticket
    # (it is not a child, so link_incident finds it) instead of feeding another surge.
    later = complain(client, "Bado hakuna network Kericho, simu haziingii kabisa.")["complaint"]
    assert later["linked_incident"] and later["linked_incident"]["id"] == new.id, later["linked_incident"]
    assert [t for t in cards(client, "CONFIRM_POSSIBLE_OUTAGE") if t["proposed_payload"].get("place") == "Kericho"] == []
    restore(client, {"id": new.id})  # ... and is told when that ticket is restored
    assert complaint_row(later["id"]).told_incident_id == new.id
    close(client, {"id": new.id})


# =========================================================================== 5. the read routes


def test_the_read_routes_answer_the_contracts_shapes(client):
    loop_numbers = client.get(f"{BASE}/loop", params={"hours": 24}).json()
    assert set(loop_numbers) == {"waiting_to_hear", "told", "told_median_minutes", "told_p90_minutes", "notices_waiting",
                                 "recipients_waiting", "still_down_reports", "repeat_contacts", "outages_with_complaints",
                                 "repeat_contacts_per_outage", "spotted_by_customers", "surges"}
    assert set(loop_numbers["surges"]) == {"open", "confirmed", "dismissed"}
    assert client.get(f"{BASE}/loop", params={"hours": -1}).status_code == 422
    surges = client.get(f"{BASE}/surges").json()
    assert surges and set(surges[0]) == {"id", "place", "region_code", "status", "origin", "complaints", "numbers",
                                         "first_at", "last_at", "card_id", "incident_id", "incident_number", "error",
                                         "decided_by", "decided_at", "reason", "complaint_refs"}
    outages = client.get(f"{BASE}/outages").json()
    assert outages and all("+2547" not in json.dumps(o) for o in outages)
    assert client.get(f"{BASE}/incidents/does-not-exist/customers").status_code == 404
    read(lambda s: (s.add(IncidentRow(operator_id="airtel", incident_number="AIR000001", status="NEW", site_id="X", region_code="CST",
                                      correlation_fingerprint="air-x", id="airtel-incident")), s.commit()))
    assert client.get(f"{BASE}/incidents/airtel-incident/customers").status_code == 404  # another operator's: same 404


def test_the_surge_list_is_newest_first(client):
    created = read(lambda s: [r.created_at for r in s.scalars(select(SupportSurgeRow).order_by(SupportSurgeRow.created_at.desc()))])
    listed = [s["id"] for s in client.get(f"{BASE}/surges").json()]
    assert listed == read(lambda s: [r.id for r in s.scalars(select(SupportSurgeRow).order_by(
        SupportSurgeRow.created_at.desc(), SupportSurgeRow.id.desc()))])
    assert created == sorted(created, reverse=True)
