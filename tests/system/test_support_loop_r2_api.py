"""Close the loop, revision 2 over HTTP (docs/CLOSE_THE_LOOP.md section 7 and the security review):
the flagship link, late linking on the ingest routes and the storm, raising a held-back update
again, the Track page's limits (per address, per reference and per number, failures only), the
proxy header, the body cap, a covered surge and the retry's guards."""

from __future__ import annotations

import importlib
import itertools
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from noc_agents.api import auth
from noc_agents.db.models import IncidentRow, OutboxRow, WorkNoteRow, get_session
from noc_agents.db.models_support import SupportComplaintRow, SupportSurgeRow
from noc_agents.realtime.hub import hub
from noc_agents.support import loop, surge
from noc_agents.support.ratelimit import complaint_limiter

BASE = "/api/v1/support"
_numbers = itertools.count(1)


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    db = tmp_path_factory.mktemp("support_loop_r2") / "r2.db"
    mp.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    mp.setenv("OPERATOR_PROFILE", "safaricom")
    mp.setenv("AUTH_DISABLED", "true")
    mp.setenv("NOC_ENV", "demo")
    mp.setenv("SUPPORT_DESK_ENABLED", "true")
    mp.delenv("SUPPORT_TRUSTED_PROXIES", raising=False)

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


def number() -> str:
    return f"0746{next(_numbers):06d}"


def complain(client, text: str, msisdn: str | None = None) -> dict:
    r = client.post(f"{BASE}/complaints", json={"body": text, "msisdn": msisdn or number()})
    assert r.status_code == 201, r.text
    return r.json()["complaint"]


def read(fn):
    session = get_session()
    try:
        return fn(session)
    finally:
        session.close()


def row(complaint_id: str) -> SupportComplaintRow:
    return read(lambda s: s.get(SupportComplaintRow, complaint_id))


def event(client, place: str, *, region: str, users: int = 60000, site_type: str = "BTS") -> dict:
    r = client.post("/api/v1/events", json={
        "site_id": f"R2-{place.upper()}-{users}", "site_name": f"{place.title()} Town {site_type}", "site_type": site_type,
        "region_code": region, "alarm_code": "SITE_DOWN", "failure_domain": "POWER", "users_affected": users})
    assert r.status_code == 200, r.text
    return r.json()["incident"]


def track(client, ref, msisdn, path="/track", **extra):
    return client.post(f"{BASE}{path}", json={"ref": ref, "msisdn": msisdn, **extra})


# =================================================================== flagship and late linking


def test_the_storm_late_links_an_earlier_complaint_and_kayole_links_its_hub_as_wide_area(client):
    mine = number()
    early = complain(client, "No network in Thika since morning, calls keep failing.", mine)
    assert early["linked_incident"] is None  # nothing open yet: answered, honestly
    page = track(client, early["ref"], mine).json()
    assert page["headline"] == "We have passed your report to our network team"
    assert client.post("/api/v1/demo/rain-storm").status_code == 200
    linked = row(early["id"])
    thika = read(lambda s: s.scalar(select(IncidentRow).where(IncidentRow.site_name == "Thika Mt Kenya HUB")))
    assert (linked.linked_incident_id, linked.link_strength) == (thika.id, "site")  # the promise was kept
    page = track(client, early["ref"], mine).json()
    assert page["stage"] == "outage_known" and page["outage"]["ticket"] == thika.incident_number
    assert any(t["text"] == f"Linked to the outage in Thika (ticket {thika.incident_number})." for t in page["timeline"])
    kayole = complain(client, "Manze hakuna network huku Kayole tangu saa nne, kuna shida gani?")
    assert kayole["linked_incident"]["title"].find("Embakasi East Aggregation HUB") >= 0
    assert row(kayole["id"]).link_strength == "wide_area"
    # Staff are told how a complaint was matched (with auth off the form answers the staff view too).
    staff = client.get(f"{BASE}/complaints/{kayole['id']}").json()["complaint"]
    assert staff["link_strength"] == "wide_area"
    hub = kayole["linked_incident"]["id"]
    panel = client.get(f"{BASE}/incidents/{hub}/customers").json()
    assert {c["ref"]: c["link_strength"] for c in panel["complaints"]}[kayole["ref"]] == "wide_area"


def test_the_events_routes_late_link_and_a_merged_duplicate_does_not(client):
    # The Coast has no storm hub, so these complaints have nothing to link to yet.
    single = complain(client, "No network in Kilifi since this morning, calls keep failing.")
    batch = complain(client, "No network in Malindi since this morning, calls keep failing.")
    assert single["linked_incident"] is None and batch["linked_incident"] is None
    kilifi = event(client, "kilifi", region="CST")
    assert row(single["id"]).linked_incident_id == kilifi["id"] and row(single["id"]).link_strength == "site"
    assert row(batch["id"]).linked_incident_id is None  # Malindi: only a single-site outage in its region (weak)
    r = client.post("/api/v1/events/batch", json=[{
        "site_id": "R2-MALINDI-1", "site_name": "Malindi Town BTS", "site_type": "BTS", "region_code": "CST",
        "alarm_code": "SITE_DOWN", "failure_domain": "POWER", "users_affected": 60000}])
    assert r.status_code == 200
    assert row(batch["id"]).linked_incident_id == r.json()["incidents"][0]["id"]
    # The same alarm again is merged into the open ticket: not a NEW incident, so nothing is re-linked.
    again = complain(client, "Hakuna mtandao Kilifi tangu asubuhi, siwezi kupiga simu.")
    assert again["linked_incident"]["id"] == kilifi["id"]  # (linked at intake now, of course)
    assert event(client, "kilifi", region="CST")["id"] == kilifi["id"]


# ======================================================================= 7.1 raising again


def test_a_person_raises_a_held_back_update_again(client):
    inc = event(client, "naivasha", region="RFT", users=150000)  # P2: the update waits for a person
    c = complain(client, "No network in Naivasha since morning, calls keep failing.")
    assert c["linked_incident"]["id"] == inc["id"]
    assert client.post(f"{BASE}/incidents/{inc['id']}/customer-update").status_code == 409  # still open
    assert client.post(f"/api/v1/incidents/{inc['id']}/restore", json={"note": "Back on grid"}).status_code == 200
    [card] = [t for t in client.get("/api/v1/hitl/pending").json()
              if t["task_type"] == "APPROVE_CUSTOMER_UPDATE" and t["incident_id"] == inc["id"]]
    assert card["proposed_payload"]["restore_note"] == "Back on grid"
    assert client.post(f"{BASE}/incidents/{inc['id']}/customer-update").status_code == 409  # a card waits
    assert client.post(f"/api/v1/hitl/{card['id']}/reject",
                       json={"resolved_by": "Duty Manager", "reason": "wording"}).status_code == 200
    r = client.post(f"{BASE}/incidents/{inc['id']}/customer-update")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["notice"]["state"] == "awaiting_approval" and body["waiting"] == 1  # the same ladder: a fresh card
    assert set(body) == {"customers", "told", "waiting", "still_down", "notice", "complaints", "follow_up"}
    assert client.post(f"{BASE}/incidents/does-not-exist/customer-update").status_code == 404


# ======================================================================= B2. the Track page


def _filed(client):
    mine = number()
    c = complain(client, "How do I check my data balance on my phone?", mine)
    return c["ref"], mine


def test_failed_attempts_are_budgeted_per_reference_and_a_real_customer_is_not_locked_out(client):
    ref, mine = _filed(client)
    for _ in range(9):
        assert track(client, ref, number()).status_code == 404
    for _ in range(5):  # the customer's own look-ups never spend the failure budget
        assert track(client, ref, mine).status_code == 200
    complaint_limiter.reset()
    for _ in range(15):  # junk without a number never spends the reference's budget either
        assert client.post(f"{BASE}/track", json={"ref": ref}).status_code == 404
    assert track(client, ref, mine).status_code == 200
    complaint_limiter.reset()
    for _ in range(10):
        assert track(client, ref, number()).status_code == 404
    assert track(client, ref, number()).status_code == 429  # the 11th failure on one reference


def test_failed_attempts_are_budgeted_per_number_across_references(client):
    _ref, mine = _filed(client)
    for n in range(10):  # ten references tried with one number (the address limit is 20: not what stops it)
        assert track(client, f"CMP-9{n:05d}", mine).status_code == 404
    blocked = track(client, "CMP-912345", mine)
    assert blocked.status_code == 429  # walking references with one number


def test_a_forged_forwarded_for_header_never_buys_a_fresh_allowance(client, monkeypatch):
    for n in range(20):
        r = client.post(f"{BASE}/track", json={"ref": f"CMP-7{n:05d}"}, headers={"X-Forwarded-For": f"10.0.0.{n}"})
        assert r.status_code == 404
    spoofed = client.post(f"{BASE}/track", json={"ref": "CMP-700099"}, headers={"X-Forwarded-For": "10.9.9.9"})
    assert spoofed.status_code == 429  # one bucket: the direct peer
    complaint_limiter.reset()
    monkeypatch.setenv("SUPPORT_TRUSTED_PROXIES", "testclient")  # behind a proxy the operator trusts ...
    for n in range(25):
        r = client.post(f"{BASE}/track", json={"ref": f"CMP-6{n:05d}"}, headers={"X-Forwarded-For": f"203.0.113.{n}"})
        assert r.status_code == 404  # ... each forwarded client has its own allowance


def test_a_valid_pair_padded_past_four_kilobytes_is_the_same_404(client):
    ref, mine = _filed(client)
    padded = json.dumps({"ref": ref, "msisdn": mine, "pad": "x" * 5000})
    for path in ("/track", "/track/still-down"):
        r = client.post(f"{BASE}{path}", content=padded, headers={"content-type": "application/json"})
        assert (r.status_code, r.json()) == (404, {"detail": loop.NOT_FOUND}), path
    # A Content-Length larger than the cap is refused before a byte of the body is read.
    lying = client.post(f"{BASE}/track", content=json.dumps({"ref": ref, "msisdn": mine}),
                        headers={"content-type": "application/json", "content-length": "999999"})
    assert lying.status_code in (400, 404)  # the transport may refuse the mismatch itself; never a 200
    assert track(client, ref, mine).status_code == 200  # the same pair, unpadded


# ============================================================================ surges


def _burst(client, place: str) -> dict:
    for _ in range(3):
        c = complain(client, f"No network in {place.title()} since 2pm, calls are not going through.")
        assert c["linked_incident"] is None, place
    return next(t for t in client.get("/api/v1/hitl/pending").json()
                if t["task_type"] == "CONFIRM_POSSIBLE_OUTAGE" and t["proposed_payload"].get("place") == place.title())


def test_confirming_a_surge_a_real_incident_now_covers_links_to_it_and_opens_nothing(client):
    """MINOR 11: an incident that opened without the late-link hook (here: written straight to the
    database) covers Bungoma by the time the card is approved."""
    card = _burst(client, "bungoma")
    session = get_session()
    try:
        covering = IncidentRow(operator_id="safaricom", incident_number="INC900001", status="IN_PROGRESS", priority="P3",
                               site_id="R2-BUNGOMA-NMS", site_name="Bungoma Town BTS", site_type="BTS", region_code="WNY",
                               county="Bungoma", correlation_fingerprint="r2-bungoma")
        session.add(covering)
        session.commit()
        covering_id = covering.id
    finally:
        session.close()
    assert client.post(f"/api/v1/hitl/{card['id']}/approve", json={"resolved_by": "Duty Manager"}).status_code == 200
    s = next(x for x in client.get(f"{BASE}/surges").json() if x["place"] == "Bungoma")
    assert (s["status"], s["incident_id"], s["outcome"]) == ("confirmed", covering_id, "linked_existing")
    assert read(lambda s_: s_.scalar(select(IncidentRow.id).where(IncidentRow.site_id.like("CUST-%BUNGOMA%")))) is None
    members = read(lambda s_: s_.scalars(select(SupportComplaintRow).where(SupportComplaintRow.ref.in_(s["complaint_refs"]))).all())
    assert {(m.linked_incident_id, m.link_strength) for m in members} == {(covering_id, "person")}
    notes = read(lambda s_: [n.body for n in s_.scalars(select(WorkNoteRow).where(WorkNoteRow.incident_id == covering_id))])
    assert any("already covered the place, so no new ticket was opened" in n for n in notes)
    outage = next(o for o in client.get(f"{BASE}/outages").json() if o["incident_id"] == covering_id)
    assert outage["from_customer_reports"] is False  # customers did not spot it; it was already open


def test_retry_is_only_for_a_failed_confirm_and_one_claim_wins(client):
    card = _burst(client, "siaya")
    assert client.post(f"/api/v1/hitl/{card['id']}/approve", json={"resolved_by": "Duty Manager"}).status_code == 200
    s = next(x for x in client.get(f"{BASE}/surges").json() if x["place"] == "Siaya")
    assert s["incident_id"] and client.post(f"{BASE}/surges/{s['id']}/retry").status_code == 409  # a ticket is open
    session = get_session()
    try:
        surge_row = session.get(SupportSurgeRow, s["id"])
        surge_row.status, surge_row.incident_id, surge_row.error, surge_row.open_place = "confirmed", None, None, None
        session.commit()
    finally:
        session.close()
    r = client.post(f"{BASE}/surges/{s['id']}/retry")
    assert r.status_code == 409 and "nothing has failed" in r.json()["detail"]  # MINOR 2: an error is required
    session = get_session()
    try:
        assert surge._claim(session, s["id"], "safaricom") is True
        assert surge._claim(session, s["id"], "safaricom") is False  # the compare-and-set: one claim at a time
        surge_row = session.get(SupportSurgeRow, s["id"])
        surge_row.error = "RuntimeError: earlier failure"
        session.commit()
    finally:
        session.close()
    r = client.post(f"{BASE}/surges/{s['id']}/retry")
    assert r.status_code == 409 and "being confirmed" in r.json()["detail"]  # ingesting: held by another confirm
    sms = read(lambda s_: s_.scalars(select(OutboxRow).where(OutboxRow.idempotency_key.like("support-confirmed:%"))).all())
    import re

    assert sms and all("msisdn_hash" in json.loads(r_.payload_json) and not re.search(r"\+254[17]\d{8}", r_.payload_json)
                       for r_ in sms)  # the number itself never enters the outbox
