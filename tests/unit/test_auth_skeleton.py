"""Spec §7.0.5 — the minimal auth / RBAC skeleton.

Two directions matter and both are proved here:

* ``AUTH_DISABLED=false`` — the gates BITE: no cookie is 401, a ``noc_analyst``
  is 403 on ``/hitl/{id}/approve`` while a ``shift_supervisor`` gets through.
* ``AUTH_DISABLED=true`` (the demo default, and what the whole suite runs
  under) — every gate is INERT: nothing on the gated surface is ever rejected.

Plus the production guard (``AUTH_DISABLED=true`` AND ``NOC_ENV=production``
refuses to register the ledger download), the per-client role switcher that
replaced one global session dict, and the ``email`` block that no longer leaks
out of ``GET /api/v1/profile``.
"""

from __future__ import annotations

import importlib
import logging
import os

import pytest
from fastapi.testclient import TestClient

from noc_agents.api import auth
from noc_agents.realtime.hub import hub

HUB_EVENT = {
    "site_id": "SFC-NBIE-HUB-EMB",
    "site_name": "Embakasi East Aggregation HUB",
    "site_type": "HUB",
    "region_code": "NBI_E",
    "alarm_code": "POWER_GRID_FAIL",
    "failure_domain": "POWER",
    "users_affected": 450000,
}
SECRET = "unit-test-secret"
AUTH_ENV = ("AUTH_DISABLED", "NOC_ENV", "NOC_SESSION_SECRET", "CORS_ORIGINS")


@pytest.fixture()
def make_client(tmp_path, monkeypatch):
    """Factory: reload ``main`` under the env the test has already set, and
    hand back a live ``TestClient``. Reloading is how the startup-time
    production guard becomes observable (same pattern as the system tests)."""
    clients: list[TestClient] = []

    def _make(name: str = "app") -> TestClient:
        db = tmp_path / f"{name}.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
        monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")

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
        clients.append(c)
        return c

    yield _make

    for c in clients:
        c.__exit__(None, None, None)
    hub._history.clear()
    auth.reset_sessions()
    # Leave noc_agents.main loaded in its DEFAULT (demo, auth-disabled) shape so a
    # production-guard test cannot leak a route-less app into anything after it.
    for key in AUTH_ENV:
        os.environ.pop(key, None)
    import noc_agents.db.models as models
    import noc_agents.main as main

    models._engine = None
    models.SessionLocal = None
    importlib.reload(main)


def _pending_task_id(client: TestClient) -> str:
    """Ingest a P2 HUB event and return the gating HITL task it parks."""
    assert client.post("/api/v1/events", json=HUB_EVENT).status_code == 200
    pending = client.get("/api/v1/hitl/pending").json()
    assert pending, "the P2 HUB event should park a gating HITL task"
    return pending[0]["id"]


def _login(client: TestClient, role: str, sub: str = "u-1") -> None:
    client.cookies.set(auth.SESSION_COOKIE, auth.sign_session({"sub": sub, "role": role}, SECRET))


# --------------------------------------------------------------------------
# Roles and the signed cookie
# --------------------------------------------------------------------------


def test_roles_are_the_nine_from_the_spec():
    assert auth.ROLES == (
        "noc_analyst",
        "shift_supervisor",
        "duty_manager",
        "management",
        "msp_coordinator",
        "field_engineer",
        "planning",
        "legal",
        "admin",
    )


def test_require_role_rejects_an_unknown_role_at_import_time():
    with pytest.raises(ValueError):
        auth.require_role("superuser")  # type: ignore[arg-type]


def test_signed_session_roundtrips_and_rejects_tampering():
    token = auth.sign_session({"sub": "u-1", "role": "duty_manager"}, SECRET)
    assert auth.read_session(token, SECRET) == {"sub": "u-1", "role": "duty_manager"}

    payload, _, sig = token.partition(".")
    forged = auth.sign_session({"sub": "u-1", "role": "admin"}, SECRET).split(".")[0]
    assert auth.read_session(f"{forged}.{sig}", SECRET) is None  # swapped claims
    assert auth.read_session(token, "another-secret") is None  # wrong key
    assert auth.read_session(payload, SECRET) is None  # no signature at all
    assert auth.read_session(None, SECRET) is None
    assert auth.read_session(token, "") is None  # no secret configured


def test_expired_session_is_refused():
    fresh = auth.sign_session({"sub": "u", "role": "admin"}, SECRET, ttl_seconds=60)
    stale = auth.sign_session({"sub": "u", "role": "admin"}, SECRET, ttl_seconds=-1)
    assert auth.read_session(fresh, SECRET) is not None
    assert auth.read_session(stale, SECRET) is None


def test_auth_is_disabled_by_default_and_env_is_read_per_call(monkeypatch):
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    assert auth.auth_disabled() is True
    monkeypatch.setenv("AUTH_DISABLED", "false")
    assert auth.auth_disabled() is False
    monkeypatch.setenv("AUTH_DISABLED", "nonsense")  # unparseable falls back to the default
    assert auth.auth_disabled() is True


def test_cors_origins_default_to_the_vite_dev_server(monkeypatch):
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    assert auth.cors_origins() == ["http://localhost:5173"]
    monkeypatch.setenv("CORS_ORIGINS", "https://noc.example.co.ke, http://localhost:5173 ")
    assert auth.cors_origins() == ["https://noc.example.co.ke", "http://localhost:5173"]
    assert "*" not in auth.cors_origins()


# --------------------------------------------------------------------------
# Direction 1: AUTH_DISABLED=false — the gates bite
# --------------------------------------------------------------------------


def test_enforced_rbac_401_403_and_the_supervisor_path(make_client, monkeypatch):
    client = make_client("enforced")
    task_id = _pending_task_id(client)  # set up while auth is still disabled

    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("NOC_SESSION_SECRET", SECRET)

    # no cookie at all
    assert client.post(f"/api/v1/hitl/{task_id}/approve", json={"resolved_by": "X"}).status_code == 401
    # a cookie we did not sign
    client.cookies.set(auth.SESSION_COOKIE, "not.asignature")
    assert client.post(f"/api/v1/hitl/{task_id}/approve", json={"resolved_by": "X"}).status_code == 401
    client.cookies.clear()

    # the spec's acceptance case: noc_analyst may claim, but may NOT approve
    _login(client, "noc_analyst")
    assert client.post(f"/api/v1/hitl/{task_id}/claim", json={"resolved_by": "Analyst"}).status_code == 200
    r = client.post(f"/api/v1/hitl/{task_id}/approve", json={"resolved_by": "Analyst"})
    assert r.status_code == 403
    assert "noc_analyst" in r.json()["detail"]
    # ... nor may an analyst run the shift handover
    assert client.post("/api/v1/shifts/handover").status_code == 403
    # ... but an operations route they DO own still answers
    assert client.get("/api/v1/hitl/pending").status_code == 200

    # a supervisor gets through the very same gate
    client.cookies.clear()
    _login(client, "shift_supervisor", sub="u-2")
    assert client.post(f"/api/v1/hitl/{task_id}/approve", json={"resolved_by": "Supervisor A"}).status_code == 200

    # roles outside the allow-list are refused even though they are real roles
    client.cookies.clear()
    _login(client, "legal", sub="u-3")
    assert client.post("/api/v1/shifts/handover").status_code == 403


def test_enforced_without_a_secret_is_a_503_not_an_open_door(make_client, monkeypatch):
    client = make_client("nosecret")
    task_id = _pending_task_id(client)
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.delenv("NOC_SESSION_SECRET", raising=False)
    r = client.post(f"/api/v1/hitl/{task_id}/approve", json={"resolved_by": "X"})
    assert r.status_code == 503
    assert "NOC_SESSION_SECRET" in r.json()["detail"]


def test_deliberately_open_routes_stay_open_when_auth_is_enforced(make_client, monkeypatch):
    """Enforcement must not silently close routes nobody decided to close.

    This test used to include ``/api/v1/incidents`` in the open list, with the
    rationale "read/ingest routes carry no gate yet". A Phase 2 conformance audit
    forced AUTH_DISABLED=false + NOC_ENV=production and showed what that meant in
    practice: an anonymous caller could read every incident, read the regulator-facing
    audit trail, and POST /api/v1/events to run a complete 12-node lifecycle — writing
    an incident, 12 step rows, 12 audit rows and 4 outbox rows with no credential at
    all. Those four routes are now gated (see the companion test below).

    What stays open is deliberate and small: a liveness probe, the profile (whose
    mailbox block was removed in Phase 1 precisely because it leaked config on an
    unauthenticated route), and aggregate metrics that carry no incident detail.
    """
    client = make_client("ungated")
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("NOC_SESSION_SECRET", SECRET)
    for path in ("/health", "/api/v1/profile", "/api/v1/metrics/summary"):
        assert client.get(path).status_code == 200, path


def test_lifecycle_ingest_and_the_read_surfaces_are_not_anonymous(make_client, monkeypatch):
    """The holes the conformance audit found, pinned shut.

    Ingest is the sharp one: it is a WRITE that starts the whole agent lifecycle, so an
    unauthenticated POST does not merely read data — it manufactures incidents in a
    production NOC.
    """
    client = make_client("gated-reads")
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("NOC_SESSION_SECRET", SECRET)
    client.cookies.clear()

    for path in ("/api/v1/incidents", "/api/v1/runs", "/api/v1/audit"):
        assert client.get(path).status_code == 401, path

    r = client.post("/api/v1/events", json=dict(HUB_EVENT))
    assert r.status_code == 401, f"anonymous ingest returned {r.status_code}"

    # And a legitimate operator still gets through.
    _login(client, "noc_analyst", sub="u-ingest")
    assert client.post("/api/v1/events", json=dict(HUB_EVENT)).status_code == 200


# --------------------------------------------------------------------------
# Direction 2: AUTH_DISABLED=true (the default) — every gate is inert
# --------------------------------------------------------------------------


def test_every_gated_route_is_inert_under_the_demo_default(make_client):
    """The whole gated surface, exercised with NO cookie and the default role
    switcher (noc_analyst) — which is exactly how the other 341 tests call it."""
    client = make_client("inert")
    assert auth.auth_disabled() is True, "the suite must run with auth disabled"

    inc = client.post("/api/v1/events", json=HUB_EVENT).json()["incident"]
    task_id = client.get("/api/v1/hitl/pending").json()[0]["id"]

    calls = [
        ("GET", "/api/v1/hitl/pending", None),
        ("POST", f"/api/v1/hitl/{task_id}/claim", {"resolved_by": "Supervisor A"}),
        # approve, which a noc_analyst is 403 on once AUTH_DISABLED=false
        ("POST", f"/api/v1/hitl/{task_id}/approve", {"resolved_by": "Supervisor A"}),
        (
            "POST",
            f"/api/v1/incidents/{inc['id']}/reassign",
            {"assignee_type": "MSP", "assignee_name": "EGYPRO", "reason": "closer team"},
        ),
        ("POST", f"/api/v1/incidents/{inc['id']}/close", {"closed_by": "NOC"}),
        ("GET", "/api/v1/shifts/ledger", None),
        ("POST", "/api/v1/shifts/handover", None),
    ]
    for method, path, body in calls:
        r = client.get(path) if method == "GET" else client.post(path, json=body)
        assert r.status_code not in (401, 403, 503), f"{method} {path} -> {r.status_code}"
        assert r.status_code == 200, f"{method} {path} -> {r.status_code} {r.text[:200]}"


def test_reject_is_also_inert_under_the_demo_default(make_client):
    client = make_client("inertreject")
    task_id = _pending_task_id(client)
    r = client.post(f"/api/v1/hitl/{task_id}/reject", json={"resolved_by": "Sup", "reason": "duplicate"})
    assert r.status_code == 200


def test_switching_the_demo_role_never_causes_a_rejection(make_client):
    """Even the most restricted role in the switcher sails through while
    AUTH_DISABLED=true: the switcher is a UI convenience, not a decision."""
    client = make_client("switcher")
    task_id = _pending_task_id(client)
    assert client.post("/api/v1/session", json={"display_name": "Legal", "role": "legal"}).status_code == 200
    assert client.get("/api/v1/session").json()["role"] == "legal"
    assert client.post(f"/api/v1/hitl/{task_id}/approve", json={"resolved_by": "Legal"}).status_code == 200
    assert client.post("/api/v1/shifts/handover").status_code == 200


# --------------------------------------------------------------------------
# Per-client sessions, /profile, production guard
# --------------------------------------------------------------------------


def test_sessions_are_per_client_not_one_global_dict(make_client):
    a = make_client("sess")
    import noc_agents.main as main

    b = TestClient(main.app)  # a second browser against the same app
    with b:
        assert a.post("/api/v1/session", json={"display_name": "Sup", "role": "shift_supervisor"}).status_code == 200
        assert a.get("/api/v1/session").json()["role"] == "shift_supervisor"
        # the other client still sees the untouched default
        assert b.get("/api/v1/session").json() == {
            "display_name": "NOC Analyst",
            "role": "noc_analyst",
            "region_filter": None,
            "msp_filter": None,
        }
        b.post("/api/v1/session", json={"display_name": "Duty", "role": "duty_manager"})
        assert b.get("/api/v1/session").json()["role"] == "duty_manager"
        assert a.get("/api/v1/session").json()["role"] == "shift_supervisor"  # unchanged


def test_profile_no_longer_leaks_the_email_block(make_client):
    client = make_client("profile")
    body = client.get("/api/v1/profile").json()
    assert "email" not in body
    text = client.get("/api/v1/profile").text
    assert "@" not in text, "no address of any kind should reach /profile"
    # everything the UI actually reads is still there
    for key in ("operator_id", "display_name", "incident_prefix", "autonomy_level", "timezone",
                "regions", "locale_notes", "shift", "shift_id"):
        assert key in body, key
    # and the dedicated route still serves it for the Settings page
    assert client.get("/api/v1/email/status").status_code == 200


def test_production_guard_refuses_the_ledger_download_and_logs_once(make_client, monkeypatch, caplog):
    monkeypatch.setenv("AUTH_DISABLED", "true")
    monkeypatch.setenv("NOC_ENV", "production")
    assert auth.production_guard_active() is True

    with caplog.at_level(logging.WARNING, logger="noc_agents.auth"):
        client = make_client("prod")
    lines = [r for r in caplog.records if r.name == "noc_agents.auth"]
    assert len(lines) == 1, f"expected exactly one guard line, got {[r.getMessage() for r in lines]}"
    msg = lines[0].getMessage()
    assert "AUTH_DISABLED=true" in msg and "NOC_ENV=production" in msg
    assert "/api/v1/shifts/ledger" in msg

    import noc_agents.main as main

    assert "/api/v1/shifts/ledger" not in {getattr(r, "path", None) for r in main.app.routes}
    assert client.get("/api/v1/shifts/ledger").status_code == 404
    # the rest of the API is untouched by the guard
    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/profile").status_code == 200


@pytest.mark.parametrize(
    "auth_disabled,env,guarded",
    [
        ("true", "production", True),
        ("false", "production", False),  # authenticated production: serve it
        ("true", "demo", False),  # the demo default
        ("false", "demo", False),
    ],
)
def test_production_guard_needs_both_conditions(monkeypatch, auth_disabled, env, guarded):
    monkeypatch.setenv("AUTH_DISABLED", auth_disabled)
    monkeypatch.setenv("NOC_ENV", env)
    assert auth.production_guard_active() is guarded


def test_ledger_route_is_registered_under_the_defaults(make_client):
    client = make_client("demoledger")
    import noc_agents.main as main

    assert "/api/v1/shifts/ledger" in {getattr(r, "path", None) for r in main.app.routes}
    assert client.get("/api/v1/shifts/ledger").status_code == 200
