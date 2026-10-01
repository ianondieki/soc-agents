"""Spec §10.1 security layer: the §9.3 RBAC matrix as 401/403 assertions, auth ENFORCED.

``tests/unit/test_auth_skeleton.py`` proves the seam works: the signed cookie, the
401/403/503 answers, and that every gate is inert under the demo default. This file proves
the seam is APPLIED. Every route ``main.py`` declares is either gated with the allow-list
§9.3 gives it, or open on purpose with the reason written at the route. The conformance
audit (docs/CONFORMANCE.md A-02..A-05) found routes in a third state -- nobody had decided --
and ``test_every_route_main_declares_has_a_recorded_decision`` is what keeps a new route from
landing in it again.

Three answers per gated route, all with ``AUTH_DISABLED=false``:

* no cookie -> 401;
* every one of the nine roles OUTSIDE the route's row -> 403;
* EVERY role inside it -> anything but 401/403. The handler may still answer 404 or 422;
  that is the handler's business, and it proves the gate let the caller through. Where
  sending every role for real would change state, the roles beyond ``probe`` are sent a
  request shaped to fail in the handler instead (see ``Gated.rest``).

The allow-lists are spelled out below as literals, NOT imported from ``api/deps.py`` or
``main.py``. This file is an independent statement of the matrix: widening or narrowing a
tuple in the code fails here, so it has to be a decision rather than a drift.

Also here, because a gate that admits a role is only half of it: a note's provenance (who,
in what capacity, through what channel) comes from the principal, not the body, once auth
is on -- the vendor roles were let in to write notes, and ``author_role`` starts the vendor
MTTA clock. And the PIR reads follow §9.3's PIR row, not the incident row. Plus the contracts
production guard (§7.0.5 / §7.8.6): with ``AUTH_DISABLED=true`` and ``NOC_ENV=production``
the contracts router registers no routes at all.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from noc_agents.api import auth
from noc_agents.realtime.hub import hub

SECRET = "system-auth-secret"
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"  # what main.py serves

HUB_EVENT = {
    "site_id": "SFC-NBIE-HUB-EMB",
    "site_name": "Embakasi East Aggregation HUB",
    "site_type": "HUB",
    "region_code": "NBI_E",
    "alarm_code": "POWER_GRID_FAIL",
    "failure_domain": "POWER",
    "users_affected": 450000,
}
VENDOR_NOTE = {
    "author": "EGYPRO desk",
    "author_role": "MSP",
    "body": "Crew dispatched from the Embakasi depot, ETA forty minutes.",
}


# --------------------------------------------------------------------------------- §9.3
# Literal on purpose (see the module docstring). Each names the §9.3 cell it encodes.

ROLES = frozenset(auth.ROLES)
#: Row 1 R/W ("ingest, notes, timeline, workflow, signals read") -- the operations floor.
OPERATIONS = frozenset({"noc_analyst", "shift_supervisor", "duty_manager", "admin"})
#: Alarm ingest is row 1 R/W minus the two "notes only" roles.
INGEST = OPERATIONS
#: api/deps.READERS, as the lane routers still use it for surfaces §9.3 has no row for.
READERS = OPERATIONS | {"management", "msp_coordinator", "field_engineer", "planning"}
#: Row 1's read column, read STRICTLY (round 4): legal R, and the two vendor roles "notes only"
#: -- they read nothing in row 1. tests/system/test_rbac_matrix.py derives the same set from
#: the spec's own cells; this literal is the independent statement.
INCIDENT_READERS = OPERATIONS | {"management", "planning", "legal"}
#: Row 1 "notes only": the one write msp_coordinator and field_engineer are given.
NOTE_AUTHORS = OPERATIONS | {"msp_coordinator", "field_engineer"}
#: The handover run (row 4 is its APPROVAL).
SUPERVISORS = frozenset({"shift_supervisor", "duty_manager", "admin"})
#: The four HITL routes' route-level gate: everyone who may act on at least one card TYPE --
#: row 2's supervisors, planning (schedule/window), management (handover, row 4). Which card a
#: role may act on is per type: tests/system/test_rbac_matrix.py tests every type.
HITL_ANY = SUPERVISORS | {"planning", "management"}
#: "Templates status, outbox retry, scheduler run, MCP status, agents" -- the read side.
PLATFORM_READERS = frozenset({"noc_analyst", "shift_supervisor", "duty_manager", "management", "admin"})
#: The regulator-facing audit trail.
AUDIT_READERS = frozenset({"duty_manager", "management", "legal", "admin"})
#: "Ledger xlsx download".
LEDGER_DOWNLOAD = frozenset({"shift_supervisor", "duty_manager", "management", "admin"})
#: The actions in the platform row.
ADMIN = frozenset({"admin"})
#: The PIR row, read side: edit / publish / read for everyone but the two vendor roles ("—").
PIR_READERS = OPERATIONS | {"management", "planning", "legal"}
#: The memory row, read cell ("sites / playbooks / stats read"): the two vendor roles "—".
MEMORY_READERS = OPERATIONS | {"management", "planning", "legal"}

#: How the allowed roles beyond ``probe`` are sent (``Gated.rest``).
REAL, ABSENT, INVALID = "real", "absent", "invalid"


@dataclass(frozen=True)
class Gated:
    method: str
    #: The route's own template, exactly as main.py declares it (the decision test matches on it).
    path: str
    allowed: frozenset
    body: object = None
    #: Roles sent through for real. None = every allowed role; () = a dedicated test.
    probe: tuple | None = None
    #: Substitute ids that do not exist. For the writes whose handlers change state: the gate
    #: runs before the lookup, so a 404 still proves the caller got through, and nothing moves.
    absent: bool = False
    #: The allowed roles NOT in ``probe``: ABSENT sends them ids that do not exist (404 from the
    #: handler); INVALID sends ``invalid`` -- as the body, or as the query string when it starts
    #: with "?" -- which fails validation after the gate (422); REAL sends them for real, for a
    #: route that takes no input at all, so no request can fail after the gate (the row says why).
    rest: str = REAL
    invalid: object = None


GATED: list[Gated] = [
    # --- A-02: the other two doors into the 12-node lifecycle ---------------------------
    Gated("POST", "/api/v1/events", INGEST, HUB_EVENT, probe=("noc_analyst",), rest=INVALID, invalid={}),
    Gated(
        "POST", "/api/v1/events/batch", INGEST, [HUB_EVENT], probe=("noc_analyst",), rest=INVALID, invalid=[{}]
    ),
    Gated(
        "POST",
        "/api/v1/demo/rain-storm",
        INGEST,
        probe=("noc_analyst",),
        rest=INVALID,
        invalid="?stagger_ms=not-a-number",
    ),
    # --- A-03: notes -- wider than OPERATIONS, and the reason the vendor roles exist -----
    Gated(
        "POST",
        "/api/v1/incidents/{incident_id}/notes",
        NOTE_AUTHORS,
        VENDOR_NOTE,
        probe=("msp_coordinator", "field_engineer", "noc_analyst"),
        rest=ABSENT,
    ),
    # --- A-04: row 1's reads, list and detail forms alike (legal included) --------------
    Gated("GET", "/api/v1/incidents", INCIDENT_READERS),
    Gated("GET", "/api/v1/incidents/{incident_id}", INCIDENT_READERS),
    Gated("GET", "/api/v1/incidents/{incident_id}/timeline", INCIDENT_READERS),
    Gated("GET", "/api/v1/incidents/{incident_id}/workflow", INCIDENT_READERS),
    Gated("GET", "/api/v1/runs", INCIDENT_READERS),
    Gated("GET", "/api/v1/runs/{run_id}", INCIDENT_READERS),
    Gated("GET", "/api/v1/briefs/{incident_id}", INCIDENT_READERS),
    Gated("GET", "/api/v1/problems", INCIDENT_READERS),
    Gated("GET", "/api/v1/sites", INCIDENT_READERS),
    Gated("GET", "/api/v1/signals/weather/regions", INCIDENT_READERS),
    Gated("GET", "/api/v1/demo/scenarios", INCIDENT_READERS),
    Gated("GET", "/api/v1/demo/rain-storm/events", INCIDENT_READERS),
    # An infinite stream: its "gets through" half is test_the_event_stream_lets_every_reader_through.
    Gated("GET", "/api/v1/stream/events", INCIDENT_READERS, probe=()),
    # --- A-04: the §9.3 platform row ---------------------------------------------------
    Gated("GET", "/api/v1/scheduler/status", PLATFORM_READERS),
    Gated("GET", "/api/v1/agents", PLATFORM_READERS),
    Gated("GET", "/api/v1/agents/{name}", PLATFORM_READERS),
    Gated("GET", "/api/v1/llm/status", PLATFORM_READERS),
    Gated("GET", "/api/v1/email/status", PLATFORM_READERS),
    Gated("POST", "/api/v1/email/test", ADMIN),
    Gated("POST", "/api/v1/scheduler/run/{job}", ADMIN, absent=True),
    # --- floor actions -------------------------------------------------------------------
    # REAL: the tick takes no input, so nothing can fail after the gate; the chase dedupes
    # its notes within a window, so the repeat ticks are near no-ops on this throwaway file.
    Gated("POST", "/api/v1/monitor/tick", OPERATIONS, probe=("noc_analyst",), rest=REAL),
    Gated(
        "POST", "/api/v1/incidents/{incident_id}/analysis", OPERATIONS, probe=("noc_analyst",), rest=ABSENT
    ),
    Gated(
        "POST", "/api/v1/incidents/{incident_id}/brief/draft", OPERATIONS, probe=("noc_analyst",), rest=ABSENT
    ),
    Gated("POST", "/api/v1/incidents/{incident_id}/close", OPERATIONS, {"closed_by": "NOC"}, absent=True),
    Gated("POST", "/api/v1/incidents/{incident_id}/restore", OPERATIONS, {"note": "Power back"}, absent=True),
    Gated(
        "POST",
        "/api/v1/incidents/{incident_id}/reassign",
        OPERATIONS,
        {"assignee_type": "MSP", "assignee_name": "EGYPRO", "reason": "closer team"},
        absent=True,
    ),
    # --- HITL: §9.3 row 2 gives noc_analyst "—", claim included (round 4); per-type below ---
    Gated("GET", "/api/v1/hitl/pending", HITL_ANY),
    Gated("POST", "/api/v1/hitl/{task_id}/claim", HITL_ANY, {"resolved_by": "X"}, absent=True),
    Gated("POST", "/api/v1/hitl/{task_id}/approve", HITL_ANY, {"resolved_by": "X"}, absent=True),
    Gated(
        "POST", "/api/v1/hitl/{task_id}/reject", HITL_ANY, {"resolved_by": "X", "reason": "dup"}, absent=True
    ),
    # --- shifts and the audit trail ------------------------------------------------------
    # The JSON ledger list takes row 4 like the xlsx (the stricter reading; see main.py).
    Gated("GET", "/api/v1/shifts/ledger", LEDGER_DOWNLOAD),
    Gated("GET", "/api/v1/shifts/ledger/{shift_id:path}.xlsx", LEDGER_DOWNLOAD),
    # REAL: the handover takes no input; each call queues one HELD mail behind a new approval
    # card in this module's throwaway file, and nothing leaves (EMAIL_ENABLED=false).
    Gated("POST", "/api/v1/shifts/handover", SUPERVISORS, probe=("shift_supervisor",), rest=REAL),
    Gated("GET", "/api/v1/audit", AUDIT_READERS),
]

#: Lane-router routes pinned here too, because each shares a §9.3 row with a main.py route and
#: once drifted from it (review C1: legal was 200 on /signals/weather/regions and 403 on /signals
#: beside it). The decision test covers main.py only; these ride the same three checks.
ROUTER_GATED: list[Gated] = [
    Gated("GET", "/api/v1/signals", INCIDENT_READERS),
    Gated("GET", "/api/v1/signals/county-map", INCIDENT_READERS),
    Gated("GET", "/api/v1/signals/precision", INCIDENT_READERS),
    # The memory row, not row 1: the two vendor roles may read their ticket but not the
    # site's earlier ones (api/deps.MEMORY_READERS, and the advisory tests in test_memory_api).
    Gated("GET", "/api/v1/memory/sites/{site_id}", MEMORY_READERS),
]
#: Every route the three matrix checks run over.
MATRIX: list[Gated] = GATED + ROUTER_GATED

#: Open on purpose; the reason is the comment at each route in main.py.
OPEN: list[tuple[str, str, object]] = [
    ("GET", "/health", None),  # the load balancer's liveness probe
    ("GET", "/api/v1/profile", None),  # the login screen, before anyone has a role
    ("GET", "/api/v1/session", None),  # the role switcher: grants nothing with auth on
    ("POST", "/api/v1/session", {"display_name": "Anyone", "role": "noc_analyst"}),
    ("GET", "/api/v1/shifts/current", None),  # a subset of /profile
    ("GET", "/api/v1/metrics/summary", None),  # aggregate counts; pinned by test_auth_skeleton
]
#: The SPA shell, registered only when the frontend is built (the login screen lives here).
SPA_OPEN: list[tuple[str, str, str]] = [
    ("GET", "/", "/"),
    ("GET", "/{full_path:path}", "/incidents/a-deep-link"),
]
#: Gated, but not by a dependency: no ``require_role`` can bind on a WebSocket handshake, so
#: the ops feed is gated inside the handler by ``auth.authorise_socket`` (A-14). It is a
#: decision like every row above, recorded here because the route table cannot show it.
SOCKET_GATED: set[tuple[str, str]] = {("WS", "/ws/ops")}


def _rid(route: Gated) -> str:
    return f"{route.method} {route.path}"


def _url(route: Gated, ids: dict[str, str]) -> str:
    url = route.path
    for placeholder, value in ids.items():
        url = url.replace(placeholder, "does-not-exist" if route.absent else value)
    return url


def _call(client: TestClient, route: Gated, ids: dict[str, str]):
    url = _url(route, ids)
    if route.method == "GET":
        return client.get(url)
    return client.post(url) if route.body is None else client.post(url, json=route.body)


def _call_rest(client: TestClient, route: Gated, ids: dict[str, str]):
    """The request the allowed roles beyond ``probe`` get (see ``Gated.rest``)."""
    if route.rest == ABSENT:
        return _call(client, replace(route, absent=True), ids)
    if route.rest == INVALID:
        url = _url(route, ids)
        if isinstance(route.invalid, str) and route.invalid.startswith("?"):
            return client.request(route.method, url + route.invalid)
        return client.request(route.method, url, json=route.invalid)
    return _call(client, route, ids)


def _as(client: TestClient, role: str) -> None:
    client.cookies.clear()
    client.cookies.set(
        auth.SESSION_COOKIE, auth.sign_session({"sub": f"u-{role}", "role": role, "name": role}, SECRET)
    )


# ----------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    """One app on its own SQLite file for the whole matrix, seeded under the demo default.

    Module-scoped because the matrix is ~40 routes x 10 callers and every one of those is a
    request, not a reload; the reload pattern itself is test_auth_skeleton's.
    """
    mp = pytest.MonkeyPatch()
    db = tmp_path_factory.mktemp("auth_matrix") / "auth.db"
    mp.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    mp.setenv("OPERATOR_PROFILE", "safaricom")
    # Seed with the gates inert, and pin NOC_ENV=demo: the ledger routes must be REGISTERED
    # for this file to test their gate, even if the calling shell exported production.
    mp.setenv("AUTH_DISABLED", "true")
    mp.setenv("NOC_ENV", "demo")
    mp.delenv("NOC_SESSION_SECRET", raising=False)

    import noc_agents.config as cfg
    import noc_agents.db.models as models
    import noc_agents.main as main

    cfg.clear_settings_cache()
    models._engine = None
    models.SessionLocal = None
    importlib.reload(main)
    auth.reset_sessions()
    hub._history.clear()
    # raise_server_exceptions=False: a handler that fails AFTER the gate let the caller in
    # answers 500 here instead of raising into the test. The property under test is the
    # gate, a 500 is still "not 401/403", and this matrix must not go red because some other
    # lane's handler is mid-change.
    client = TestClient(main.app, raise_server_exceptions=False)
    client.__enter__()
    try:
        created = client.post("/api/v1/events", json=HUB_EVENT)
        assert created.status_code == 200, created.text
        incident_id = created.json()["incident"]["id"]
        runs = client.get("/api/v1/runs", params={"incident_id": incident_id}).json()
        assert runs, "the seeded event should have produced a lifecycle run"
        ids = {
            "{incident_id}": incident_id,
            "{run_id}": runs[0]["id"],
            "{task_id}": "does-not-exist",
            "{job}": "no-such-job",
            "{name}": "SupervisorAgent",
            "{shift_id:path}": "2026-09-17_DAY",
            "{site_id}": HUB_EVENT["site_id"],
        }
        yield client, ids
    finally:
        client.__exit__(None, None, None)
        hub._history.clear()
        auth.reset_sessions()
        # Leave noc_agents.main in its DEFAULT (demo, auth-disabled) shape, reloaded while
        # DATABASE_URL still points at this module's file -- test_auth_skeleton's teardown.
        models._engine = None
        models.SessionLocal = None
        importlib.reload(main)
        mp.undo()
        cfg.clear_settings_cache()


@pytest.fixture()
def enforced(api, monkeypatch):
    """The same app with AUTH_DISABLED=false for the length of one test."""
    client, ids = api
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("NOC_SESSION_SECRET", SECRET)
    client.cookies.clear()
    yield client, ids
    client.cookies.clear()


# ------------------------------------------------------------------------ the decision


def test_every_route_main_declares_has_a_recorded_decision(api):
    """No route in the third state. A route added to main.py fails here until it is either
    in GATED (with its §9.3 row) or in OPEN (with the reason at the route)."""
    import noc_agents.main as main

    declared: set[tuple[str, str]] = set()
    for route in main.app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or getattr(endpoint, "__module__", None) != "noc_agents.main":
            continue  # FastAPI's /docs and /openapi.json, the lane routers, the /assets mount
        methods = getattr(route, "methods", None)
        for method in sorted(set(methods) - {"HEAD"}) if methods else ["WS"]:
            declared.add((method, route.path))

    decided = {(g.method, g.path) for g in GATED} | {(m, p) for m, p, _ in OPEN} | SOCKET_GATED
    if FRONTEND_DIST.exists():
        decided |= {(m, p) for m, p, _ in SPA_OPEN}

    assert sorted(declared - decided) == [], "routes in main.py with no recorded decision"
    assert sorted(decided - declared) == [], "this matrix names routes main.py no longer declares"


def test_every_gated_route_admits_only_real_roles():
    """A typo in this file's literals would make a 403 check vacuous; a probe-limited row with
    no ``rest`` shape would quietly check part of its row."""
    for route in MATRIX:
        assert route.allowed and route.allowed <= ROLES, _rid(route)
        assert route.rest in (REAL, ABSENT, INVALID), _rid(route)
        if route.probe:
            assert set(route.probe) <= route.allowed, _rid(route)
        if route.rest == INVALID:
            assert route.invalid is not None, _rid(route)


# ------------------------------------------------------------------------- gated routes


@pytest.mark.parametrize("route", MATRIX, ids=_rid)
def test_an_anonymous_caller_is_401(enforced, route):
    client, ids = enforced
    r = _call(client, route, ids)
    assert r.status_code == 401, f"{_rid(route)} answered an anonymous caller with {r.status_code}"


@pytest.mark.parametrize("route", MATRIX, ids=_rid)
def test_every_role_outside_the_row_is_403(enforced, route):
    client, ids = enforced
    outside = sorted(ROLES - route.allowed)
    got = {}
    for role in outside:
        _as(client, role)
        got[role] = _call(client, route, ids).status_code
    assert got == {role: 403 for role in outside}, _rid(route)


@pytest.mark.parametrize("route", [g for g in MATRIX if g.probe != ()], ids=_rid)
def test_the_probed_roles_get_through(enforced, route):
    client, ids = enforced
    probe = sorted(route.allowed) if route.probe is None else list(route.probe)
    got = {}
    for role in probe:
        _as(client, role)
        got[role] = _call(client, route, ids).status_code
    refused = {role: code for role, code in got.items() if code in (401, 403, 503)}
    assert not refused, f"{_rid(route)} refused roles its §9.3 row admits: {refused}"


@pytest.mark.parametrize(
    "route", [g for g in MATRIX if g.probe and g.allowed - set(g.probe)], ids=_rid
)
def test_the_rest_of_the_row_gets_through_too(enforced, route):
    """Every role in the literal, not just the probed ones: narrowing NOTE_AUTHORS to drop the
    supervisors must fail here. The rest are sent a request that fails in the HANDLER, so
    nothing changes state -- and for the INVALID shape the same request from outside the row
    must still be 403 and from nobody 401, because a 422 only proves the gate let a role in if
    the gate runs before validation."""
    client, ids = enforced
    rest = sorted(route.allowed - set(route.probe))
    got = {}
    for role in rest:
        _as(client, role)
        got[role] = _call_rest(client, route, ids).status_code
    expected = {ABSENT: 404, INVALID: 422}.get(route.rest)
    if expected is None:
        refused = {role: code for role, code in got.items() if code in (401, 403, 503)}
        assert not refused, f"{_rid(route)} refused roles its §9.3 row admits: {refused}"
    else:
        assert got == {role: expected for role in rest}, _rid(route)
    if route.rest == INVALID:
        client.cookies.clear()
        assert _call_rest(client, route, ids).status_code == 401, _rid(route)
        outside = sorted(ROLES - route.allowed)
        if outside:
            _as(client, outside[0])
            assert _call_rest(client, route, ids).status_code == 403, _rid(route)


def test_the_event_stream_lets_every_reader_through(enforced, monkeypatch):
    """The SSE route is an infinite stream, so its "gets through" half needs the stream to end.
    The hub's overflow sentinel (None) is what ends it in production; hand the route a queue
    that already holds one, and the response completes after the recent() replay."""
    client, _ = enforced

    def _ended_stream(*_args, **_kwargs):
        q: asyncio.Queue = asyncio.Queue()
        q.put_nowait(None)
        return q

    monkeypatch.setattr(hub, "subscribe", _ended_stream)
    for role in sorted(INCIDENT_READERS):
        _as(client, role)
        r = client.get("/api/v1/stream/events")
        assert r.status_code == 200, (role, r.status_code)
        assert r.headers["content-type"].startswith("text/event-stream"), role


def test_legal_reads_the_incident_surface_it_holds_r_on(enforced):
    """The review finding: §9.3 row 1 gives legal R, and these reads were open to it before
    they were gated -- so READERS (which omits legal) on them was a regression for that role.
    Legal can open contract clauses for an incident; it must be able to open the incident."""
    client, ids = enforced
    _as(client, "legal")
    incident = ids["{incident_id}"]
    for path in (
        "/api/v1/incidents",
        f"/api/v1/incidents/{incident}",
        f"/api/v1/incidents/{incident}/timeline",
        f"/api/v1/incidents/{incident}/workflow",
        f"/api/v1/runs/{ids['{run_id}']}",
        "/api/v1/problems",
        "/api/v1/signals/weather/regions",
        "/api/v1/signals",  # review C1: the lane router behind the same strip
        f"/api/v1/memory/sites/{ids['{site_id}']}",  # the memory row gives legal R on sites
    ):
        assert client.get(path).status_code == 200, path
    # ... and R is not W: legal still may not write a note or ingest.
    assert client.post(f"/api/v1/incidents/{incident}/notes", json=VENDOR_NOTE).status_code == 403
    assert client.post("/api/v1/events", json=HUB_EVENT).status_code == 403


def test_notes_are_the_one_write_the_vendor_roles_get(enforced):
    """A-03 in one place. The two "notes only" roles may write a note and nothing else on the
    incident; the three read-only roles of row 1 may not write one."""
    client, ids = enforced
    incident = ids["{incident_id}"]
    for role in ("msp_coordinator", "field_engineer"):
        _as(client, role)
        assert client.post(f"/api/v1/incidents/{incident}/notes", json=VENDOR_NOTE).status_code == 200, role
        assert client.post("/api/v1/events", json=HUB_EVENT).status_code == 403, role
        assert client.post(f"/api/v1/incidents/{incident}/close", json={}).status_code == 403, role
    for role in ("management", "planning", "legal"):
        _as(client, role)
        assert client.post(f"/api/v1/incidents/{incident}/notes", json=VENDOR_NOTE).status_code == 403, role


# ------------------------------------------------------- note provenance (review HIGH)
# One fresh incident per test, each on a site no other test in this module touches (and
# outside the rain-storm scenario, which the INGEST probe runs), so a note here always lands
# on a ticket still waiting for its vendor.


def _fresh_incident(client: TestClient, site_id: str, site_type: str, region: str) -> dict:
    _as(client, "noc_analyst")
    r = client.post(
        "/api/v1/events",
        json={
            "site_id": site_id,
            "site_type": site_type,
            "region_code": region,
            "alarm_code": "SITE_DOWN",
            "failure_domain": "POWER",
            "users_affected": 3000,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["incident"]


def _notes(incident_id: str) -> list:
    from noc_agents.db.models import WorkNoteRow, get_session

    session = get_session()
    try:
        return [
            (n.author, n.author_role, n.source, n.body)
            for n in session.query(WorkNoteRow).filter(WorkNoteRow.incident_id == incident_id)
        ]
    finally:
        session.close()


def test_an_analyst_cannot_file_a_note_as_the_vendor(enforced):
    """author_role="MSP" would stamp first_vendor_note_at -- the start of the vendor MTTA clock
    (§7.6.2) -- and make the vendor look faster than it was. source="vendor" would count it as
    the vendor's in the scorecard; source="monitor" would suppress the next silence chase."""
    client, _ = enforced
    inc = _fresh_incident(client, "SFC-NBIW-ENB-CBD07", "ENODEB", "NBI_W")
    _as(client, "noc_analyst")
    for source in ("vendor", "monitor"):
        r = client.post(
            f"/api/v1/incidents/{inc['id']}/notes",
            json={"author": "EGYPRO desk", "author_role": "MSP", "source": source, "body": f"Forged via {source}"},
        )
        assert r.status_code == 200, r.text
    after = client.get(f"/api/v1/incidents/{inc['id']}").json()
    assert after["first_vendor_note_at"] is None, "an analyst's note started the vendor's MTTA clock"
    forged = [n for n in _notes(inc["id"]) if n[3].startswith("Forged via")]
    assert forged and all(n[:3] == ("noc_analyst", "NOC", "ui") for n in forged), forged


def test_a_vendor_note_is_recorded_as_the_vendors_whatever_the_body_says(enforced):
    """The other direction: an MSP coordinator posting as "NOC" must still be counted as the
    vendor having answered -- otherwise a slow vendor could post and never start its clock."""
    client, _ = enforced
    inc = _fresh_incident(client, "SFC-CST-ENB-NYL12", "ENODEB", "CST")
    _as(client, "msp_coordinator")
    r = client.post(
        f"/api/v1/incidents/{inc['id']}/notes",
        json={"author": "Somebody Else", "author_role": "NOC", "source": "ui", "body": "On site, rectifier swap"},
    )
    assert r.status_code == 200, r.text
    _as(client, "noc_analyst")  # the vendor roles read nothing in row 1 ("notes only")
    after = client.get(f"/api/v1/incidents/{inc['id']}").json()
    assert after["first_vendor_note_at"] is not None, "the vendor's own note did not start its clock"
    assert ("msp_coordinator", "MSP", "ui", "On site, rectifier swap") in _notes(inc["id"])


def test_a_field_engineer_cannot_credit_the_restore_to_someone_else(enforced):
    """author becomes restored_by on a restoring note (§7.0.8): the name on the moment MTTR and
    the restore SLA are measured to."""
    client, _ = enforced
    inc = _fresh_incident(client, "SFC-MTK-TX-SOL01", "TX", "MTK")
    _as(client, "field_engineer")
    r = client.post(
        f"/api/v1/incidents/{inc['id']}/notes",
        json={
            "author": "Grace Wanjiru (Duty Manager)",
            "author_role": "NOC",
            "body": "Link back after splice",
            "mark_restored": True,
        },
    )
    assert r.status_code == 200, r.text
    _as(client, "noc_analyst")  # the vendor roles read nothing in row 1 ("notes only")
    after = client.get(f"/api/v1/incidents/{inc['id']}").json()
    assert after["restored_source"] == "MARK_RESTORED"
    assert after["restored_by"] == "field_engineer", after["restored_by"]
    assert ("field_engineer", "FE", "ui", "Link back after splice") in _notes(inc["id"])


def test_the_demo_keeps_the_note_provenance_the_body_gives(api):
    """AUTH_DISABLED=true (this module's default): there is no identity to forge -- the switcher
    is a UI affordance -- so the demo records the body's author, role and channel as before."""
    client, _ = api
    client.cookies.clear()
    inc = client.post(
        "/api/v1/events",
        json={"site_id": "SFC-WNY-HUB-KKG", "site_type": "HUB", "region_code": "WNY", "alarm_code": "SITE_DOWN"},
    ).json()["incident"]
    r = client.post(
        f"/api/v1/incidents/{inc['id']}/notes",
        json={"author": "Peter Otieno", "author_role": "MSP", "source": "msp", "body": "Demo vendor update"},
    )
    assert r.status_code == 200, r.text
    assert ("Peter Otieno", "MSP", "msp", "Demo vendor update") in _notes(inc["id"])


# --------------------------------------------------------- PIR reads (review LOW 1)


def test_pir_reads_follow_the_pir_row_not_the_incident_row(enforced, monkeypatch):
    """§9.3's PIR row gives msp_coordinator and field_engineer "—" and legal "read". READERS,
    which these routes used, had both backwards: a review's root causes and vendor-attributed
    actions are exactly what an external MSP role should not read."""
    client, _ = enforced
    monkeypatch.setenv("PIR_ENABLED", "true")  # read per call; off, the lane 404s before the gate
    for path in (
        "/api/v1/pir",
        "/api/v1/pir/awaiting-review",
        "/api/v1/pir/does-not-exist",
        "/api/v1/pir/does-not-exist/actions",
    ):
        client.cookies.clear()
        assert client.get(path).status_code == 401, path
        for role in sorted(ROLES - PIR_READERS):
            _as(client, role)
            assert client.get(path).status_code == 403, (path, role)
        for role in sorted(PIR_READERS):
            _as(client, role)
            assert client.get(path).status_code in (200, 404), (path, role)


# -------------------------------------------------------------------------- open routes


@pytest.mark.parametrize("method,path,body", OPEN, ids=[f"{m} {p}" for m, p, _ in OPEN])
def test_the_deliberately_open_routes_answer_an_anonymous_caller(enforced, method, path, body):
    client, _ = enforced
    r = client.get(path) if method == "GET" else client.post(path, json=body)
    assert r.status_code == 200, f"{method} {path} -> {r.status_code}"


@pytest.mark.skipif(not FRONTEND_DIST.exists(), reason="frontend not built: the SPA routes are not registered")
@pytest.mark.parametrize("method,template,url", SPA_OPEN, ids=[t for _, t, _ in SPA_OPEN])
def test_the_spa_shell_answers_an_anonymous_caller(enforced, method, template, url):
    client, _ = enforced
    r = client.get(url)
    assert r.status_code == 200, f"{template} ({url}) -> {r.status_code}"


def test_the_role_switcher_grants_nothing_once_auth_is_on(enforced):
    """The switcher is open because it must be; this is why that is safe."""
    client, _ = enforced
    assert client.post("/api/v1/session", json={"display_name": "Anyone", "role": "admin"}).status_code == 200
    assert client.get("/api/v1/session").json()["role"] == "admin"  # stored ...
    assert client.get("/api/v1/audit").status_code == 401  # ... and ignored by every gate
    assert client.post("/api/v1/email/test").status_code == 401


def test_the_ops_socket_refuses_an_anonymous_connection(enforced):
    """A-14, closed. This was a strict xfail while api/auth.py had no socket seam: the feed
    carries incident numbers, sites, run and HITL events, and answered anyone who could reach
    the port. ``auth.authorise_socket`` now refuses the handshake with 1008 -- a close before
    the accept, so the replay is never written -- and a row-1 reader still gets through.
    """
    client, _ = enforced
    client.cookies.clear()
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect(f"/ws/ops?since={hub.last_seq}"):
            pass
    assert refused.value.code == 1008


def test_the_ops_socket_admits_a_row_one_reader_and_refuses_a_vendor_role(enforced):
    client, _ = enforced
    _as(client, "legal")  # R on row 1, and the narrowest reader there is
    with client.websocket_connect(f"/ws/ops?since={hub.last_seq}"):
        pass  # the handshake completed
    _as(client, "msp_coordinator")  # "notes only": no row-1 read, socket included
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect(f"/ws/ops?since={hub.last_seq}"):
            pass
    assert refused.value.code == 1008


def test_the_socket_is_inert_in_the_demo(api):
    """AUTH_DISABLED=true: no identity, no refusal -- the demo and every WS test that runs
    under it (tests/system/test_contracts.py, tests/integration/test_ws_since.py) are
    untouched by the seam.
    """
    client, _ = api
    client.cookies.clear()
    with client.websocket_connect(f"/ws/ops?since={hub.last_seq}"):
        pass


# --------------------------------------------------- A-05: the contracts production guard


@pytest.mark.parametrize(
    "auth_disabled,env,registered",
    [
        ("true", "production", False),  # the misconfiguration the guard exists for
        ("false", "production", True),  # authenticated production: the gates do the work
        ("true", "demo", True),  # the demo default
    ],
)
def test_contracts_routes_do_not_exist_in_an_unauthenticated_production_deployment(
    monkeypatch, caplog, auth_disabled, env, registered
):
    """§7.8.6: "the routes cannot exist" -- stronger than "the routes answer 403", because in
    this configuration every require_role() on them is inert. Same shape as the complaints
    router's test; the router is reloaded because the guard is evaluated at import."""
    import noc_agents.api.routers.contracts as router_module

    monkeypatch.setenv("AUTH_DISABLED", auth_disabled)
    monkeypatch.setenv("NOC_ENV", env)
    try:
        with caplog.at_level(logging.WARNING, logger="noc_agents.auth"):
            reloaded = importlib.reload(router_module)
        guard_lines = [r.getMessage() for r in caplog.records if r.name == "noc_agents.auth"]
        served = {f"{m} {r.path}" for r in reloaded.router.routes for m in r.methods - {"HEAD"}}
        if registered:
            # The logged list IS the router: nothing served outside it, nothing listed that is not.
            assert served == set(reloaded.PRODUCTION_GUARDED_ROUTES)
            assert guard_lines == []
        else:
            assert reloaded.router.routes == []
            assert len(guard_lines) == 1, guard_lines
            assert "AUTH_DISABLED=true" in guard_lines[0] and "NOC_ENV=production" in guard_lines[0]
            assert "/api/v1/contracts/ask" in guard_lines[0]
    finally:
        monkeypatch.setenv("AUTH_DISABLED", "true")
        monkeypatch.setenv("NOC_ENV", "demo")
        importlib.reload(router_module)
    assert router_module.router.routes  # restored for the rest of the suite
