"""The support desk's HTTP surface: every route's status codes and shapes, validation, the rate
limit, the flag, RBAC with auth enforced, the public view, the storm tie-in and the production
guard. The RBAC matrix (``test_rbac_matrix.py``) asserts every route's gate role by role; this
file checks the behaviour behind the gates."""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from noc_agents.api import auth
from noc_agents.realtime.hub import hub
from noc_agents.support.ratelimit import complaint_limiter

BASE = "/api/v1/support"
SECRET = "support-api-secret"

COMPLAINT_KEYS = {"id", "ref", "created_at", "updated_at", "channel", "customer", "language", "subject", "body",
                  "category", "urgency", "sentiment", "route", "status", "outcome", "confidence", "escalation",
                  "reply", "citations", "linked_incident", "sla_due_at"}
STEP_KEYS = {"seq", "agent", "action", "summary", "detail", "duration_ms", "at"}
TOOL_CALL_KEYS = {"id", "tool", "args", "result", "status", "policy", "at", "decided_by"}
MESSAGE_KEYS = {"id", "author", "name", "body", "at"}


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    db = tmp_path_factory.mktemp("support_api") / "support.db"
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


@pytest.fixture()
def enforced(client, monkeypatch):
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("NOC_SESSION_SECRET", SECRET)
    client.cookies.clear()
    yield client
    client.cookies.clear()


def _as(client, role):
    client.cookies.clear()
    client.cookies.set(auth.SESSION_COOKIE, auth.sign_session({"sub": f"u-{role}", "role": role, "name": f"{role} user"}, SECRET))


def _file(client, body, msisdn="0711002001", **extra):
    return client.post(f"{BASE}/complaints", json={"body": body, "msisdn": msisdn, **extra})


# ------------------------------------------------------------------------------ the form


def test_registering_runs_the_pipeline_and_answers_the_contract_shape(client):
    r = _file(client, "How do I activate roaming before I travel to Uganda?", name="Achieng Atieno", channel="app")
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"complaint", "steps", "tool_calls", "messages"}
    c = body["complaint"]
    assert set(c) == COMPLAINT_KEYS
    assert c["ref"].startswith("CMP-") and c["channel"] == "app" and c["created_at"].endswith("Z")
    assert c["customer"] == {"name": "Achieng Atieno", "msisdn_masked": "+254 7•• ••• 001", "account_ref": None}
    assert (c["category"], c["route"], c["status"]) == ("roaming", "resolver", "answered")
    assert all(set(s) == STEP_KEYS for s in body["steps"]) and all(set(m) == MESSAGE_KEYS for m in body["messages"])


def test_an_action_case_returns_its_tool_calls(client):
    r = _file(client, "I was charged twice for my weekly bundle an hour ago, please refund the extra.", msisdn="0700000678")
    assert r.status_code == 201
    calls = r.json()["tool_calls"]
    assert all(set(t) == TOOL_CALL_KEYS for t in calls)
    assert [(t["tool"], t["status"]) for t in calls] == [("lookup_account", "ok"), ("issue_refund", "ok"), ("update_ticket", "ok")]


def test_an_identical_complaint_within_two_minutes_is_200_with_the_same_case(client):
    first = _file(client, "My calls keep dropping in Thika town", msisdn="0711002002")
    again = _file(client, "my calls keep dropping in thika town!", msisdn="+254711002002")
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["complaint"]["id"] == first.json()["complaint"]["id"]


@pytest.mark.parametrize(
    "payload",
    [
        {"body": "hey", "msisdn": "0711002003"},
        {"body": "     a     ", "msisdn": "0711002003"},
        {"body": "x" * 4001, "msisdn": "0711002003"},
        {"body": "My calls keep dropping", "msisdn": "020 123 4567"},
        {"body": "My calls keep dropping", "msisdn": "+255712345678"},
        {"body": "My calls keep dropping", "msisdn": "0711002003", "channel": "fax"},
        {"body": "My calls keep dropping", "msisdn": "0711002003", "subject": "s" * 91},
        {"msisdn": "0711002003"},
        {},
    ],
)
def test_bad_registrations_are_422(client, payload):
    assert client.post(f"{BASE}/complaints", json=payload).status_code == 422


def test_the_form_is_rate_limited_per_number_and_says_when_to_retry(client):
    for n in range(5):
        assert _file(client, f"My calls keep dropping, attempt {n}", msisdn="0711002004").status_code == 201
    refused = _file(client, "My calls keep dropping, attempt 6", msisdn="+254 711 002 004")  # same number, other spelling
    assert refused.status_code == 429 and int(refused.headers["Retry-After"]) >= 1
    assert _file(client, "My calls keep dropping, attempt 6", msisdn="0711002005").status_code == 201


def test_a_storm_complaint_links_the_live_incident(client):
    assert client.post("/api/v1/demo/rain-storm").status_code == 200
    c = _file(client, "Hakuna network huku Eldoret tangu asubuhi", msisdn="0711002006").json()["complaint"]
    assert c["status"] == "action_taken" and c["linked_incident"]["title"].endswith("Eldoret Rift HUB (Rift Valley)")
    assert c["linked_incident"]["incident_number"] in c["reply"]


# ---------------------------------------------------------------------------------- reads


def test_the_queue_filters_and_validates(client):
    _file(client, "Someone did a SIM swap on my line last night", msisdn="0711002007")
    queue = client.get(f"{BASE}/complaints").json()
    assert set(queue) == {"items", "counts"} and set(queue["counts"]) == {"by_status", "by_route", "by_category"}
    created = [i["created_at"] for i in queue["items"]]
    assert created == sorted(created, reverse=True)
    escalated = client.get(f"{BASE}/complaints", params={"status": "escalated"}).json()["items"]
    assert escalated and all(i["status"] == "escalated" for i in escalated)
    assert client.get(f"{BASE}/complaints", params={"q": "SIM swap", "limit": 1}).json()["items"][0]["category"] == "sim_and_fraud"
    for bad in ({"status": "lost"}, {"route": "robot"}, {"category": "weather"}, {"limit": 0}, {"limit": 201}):
        assert client.get(f"{BASE}/complaints", params=bad).status_code == 422, bad


def test_one_complaint_by_id_and_404_otherwise(client):
    cid = _file(client, "How do I port my number to another network?", msisdn="0711002008").json()["complaint"]["id"]
    detail = client.get(f"{BASE}/complaints/{cid}")
    assert detail.status_code == 200 and detail.json()["complaint"]["id"] == cid
    assert client.get(f"{BASE}/complaints/does-not-exist").status_code == 404


def test_knowledge_base_routes(client):
    articles = client.get(f"{BASE}/kb").json()["articles"]
    assert len(articles) == 20 and set(articles[0]) == {"id", "title", "category", "summary", "body", "updated_at"}
    results = client.get(f"{BASE}/kb/search", params={"q": "bando imeisha mapema"}).json()["results"]
    assert results[0]["article_id"] == "KB-DATA-BUNDLE-EXPIRED" and set(results[0]) == {"article_id", "title", "score", "snippet"}
    assert client.get(f"{BASE}/kb/search").status_code == 422
    assert client.get(f"{BASE}/kb/search", params={"q": "x"}).status_code == 422


def test_metrics(client):
    m = client.get(f"{BASE}/metrics").json()
    assert set(m) == {"total", "auto_resolved", "action_completed", "escalated", "human_resolved", "awaiting_approval",
                      "resolution_rate", "escalation_rate", "median_handle_ms", "by_category"}
    assert m["total"] >= 1 and 0 <= m["resolution_rate"] <= 1
    assert client.get(f"{BASE}/metrics", params={"hours": 1}).status_code == 200
    assert client.get(f"{BASE}/metrics", params={"hours": -1}).status_code == 422


# ---------------------------------------------------------------------------- the people


def test_claim_and_resolve(client):
    cid = _file(client, "My lawyer will write to you about my missing M-PESA money", msisdn="0711002009").json()["complaint"]["id"]
    claimed = client.post(f"{BASE}/complaints/{cid}/claim")
    assert claimed.status_code == 200 and claimed.json()["complaint"]["status"] == "in_progress"
    assert claimed.json()["complaint"]["escalation"]["claimed_by"]
    assert client.post(f"{BASE}/complaints/{cid}/claim").status_code == 409
    assert client.post(f"{BASE}/complaints/{cid}/resolve", json={"reply": "hi"}).status_code == 422
    resolved = client.post(f"{BASE}/complaints/{cid}/resolve", json={"reply": "We have traced the transfer and called you.", "note": "ok"})
    assert resolved.status_code == 200 and resolved.json()["complaint"]["status"] == "resolved"
    assert client.post(f"{BASE}/complaints/{cid}/resolve", json={"reply": "Again, resolved."}).status_code == 409
    assert client.post(f"{BASE}/complaints/does-not-exist/claim").status_code == 404


REVERSAL_OVER_LIMIT = ("Nimetuma 12000 kwa namba mbaya, code ni SHR2M9PL4Q. Tafadhali rudisha.", "0700000118")
REVERSAL_TOO_OLD = ("Two days ago I sent KES 800 to the wrong number by mistake, transaction SGT7KD2PQ1. Reverse it.", "0700000233")


def _parked(client, text, msisdn):
    detail = _file(client, text, msisdn=msisdn).json()
    assert detail["complaint"]["status"] == "awaiting_approval"
    call = next(t for t in detail["tool_calls"] if t["status"] == "needs_approval")
    return detail["complaint"]["id"], call["id"]


def test_approve_runs_the_parked_call(client):
    cid, call_id = _parked(client, *REVERSAL_OVER_LIMIT)
    r = client.post(f"{BASE}/complaints/{cid}/actions/{call_id}/approve")
    assert r.status_code == 200 and r.json()["complaint"]["status"] == "action_taken"
    assert client.post(f"{BASE}/complaints/{cid}/actions/{call_id}/approve").status_code == 409
    assert client.post(f"{BASE}/complaints/{cid}/actions/no-such-call/approve").status_code == 404


def test_reject_sends_the_case_back(client):
    cid, call_id = _parked(client, *REVERSAL_TOO_OLD)
    assert client.post(f"{BASE}/complaints/{cid}/actions/{call_id}/reject", json={}).status_code == 422
    r = client.post(f"{BASE}/complaints/{cid}/actions/{call_id}/reject", json={"reason": "the recipient disputes it"})
    assert r.status_code == 200 and r.json()["complaint"]["status"] == "escalated"
    rejected = next(t for t in r.json()["tool_calls"] if t["id"] == call_id)
    assert rejected["status"] == "rejected" and rejected["decided_by"]


# ------------------------------------------------------------------------- evals and seed


def test_evals_latest_is_404_until_a_run_then_the_run(client):
    assert client.get(f"{BASE}/evals/latest").status_code == 404
    report = client.post(f"{BASE}/evals/run").json()
    assert set(report) >= {"run_id", "ran_at", "mode", "dataset", "metrics", "gates", "passed", "confusion", "by_category", "failures"}
    assert report["passed"] is True and report["mode"] == "deterministic"
    latest = client.get(f"{BASE}/evals/latest")
    assert latest.status_code == 200 and latest.json()["run_id"] == report["run_id"]


def test_the_demo_seed_spans_every_route_and_status_and_is_idempotent_for_two_minutes(client):
    first = client.post(f"{BASE}/demo/seed")
    assert first.status_code == 200 and first.json()["created"] >= 12
    assert client.post(f"{BASE}/demo/seed").json() == {"created": 0}
    counts = client.get(f"{BASE}/complaints").json()["counts"]
    assert {"answered", "action_taken", "awaiting_approval", "escalated", "in_progress", "resolved"} <= set(counts["by_status"])
    assert set(counts["by_route"]) == {"resolver", "action", "human"}
    linked = [i for i in client.get(f"{BASE}/complaints", params={"q": "Nakuru"}).json()["items"] if i["linked_incident"]]
    assert linked, "the storm is open in this module, so the Nakuru complaint links to it"


# ------------------------------------------------------------------------ flag and RBAC


def test_with_the_flag_off_every_route_is_404(client, monkeypatch):
    monkeypatch.setenv("SUPPORT_DESK_ENABLED", "false")
    from noc_agents.api.routers import support

    for spec in support.PRODUCTION_GUARDED_ROUTES:
        method, path = spec.split(" ", 1)
        url = path.replace("{complaint_id}", "x").replace("{tool_call_id}", "y")
        assert client.request(method, url, json={}).status_code == 404, spec


def test_the_public_form_needs_no_login_and_returns_the_public_view(enforced):
    r = _file(enforced, "I sent KES 1,500 to the wrong number, code SJK4H7QW2L, please reverse.", msisdn="0700000412")
    assert r.status_code == 201
    body = r.json()
    assert body["complaint"]["customer"] == {"name": None, "msisdn_masked": "+254 7•• ••• 412", "account_ref": None}
    assert all(t["result"] is None and t["args"] == {} for t in body["tool_calls"])
    assert all(s["detail"] == {} for s in body["steps"])
    assert "Wanjiku" not in r.text and body["complaint"]["reply"].startswith("Hi there,")


def test_a_support_reader_filing_the_form_sees_the_full_trace(enforced):
    _as(enforced, "noc_analyst")
    body = _file(enforced, "My daily bundle expired early again, sijatumia", msisdn="0700000345").json()
    assert any(t["result"] for t in body["tool_calls"])


def test_reads_and_writes_follow_the_support_gates_with_auth_on(enforced):
    enforced.cookies.clear()
    assert enforced.get(f"{BASE}/complaints").status_code == 401
    _as(enforced, "field_engineer")
    assert enforced.get(f"{BASE}/complaints").status_code == 403
    _as(enforced, "management")
    assert enforced.get(f"{BASE}/complaints").status_code == 200
    assert enforced.post(f"{BASE}/complaints/x/claim").status_code == 403
    _as(enforced, "shift_supervisor")
    assert enforced.post(f"{BASE}/complaints/x/claim").status_code == 404  # through the gate, no such case


def test_no_support_route_exists_in_an_unauthenticated_production_deployment(monkeypatch):
    from noc_agents.api.routers import support as router_module

    monkeypatch.setenv("AUTH_DISABLED", "true")
    monkeypatch.setenv("NOC_ENV", "production")
    try:
        guarded = importlib.reload(router_module)
        assert guarded.router.routes == []
        assert "POST /api/v1/support/complaints" in guarded.PRODUCTION_GUARDED_ROUTES
    finally:
        monkeypatch.setenv("NOC_ENV", "demo")
        importlib.reload(router_module)
    assert router_module.router.routes
