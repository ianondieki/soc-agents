"""``GET /api/v1/memory/sites/{site_id}`` — the M0 read surface (spec §7.11.4, §7.11.11).

What this route has to get right is unusual for a read route, because of where it is rendered:
the incident workspace, on every load, while somebody is working an outage.

* **It must never be the reason the workspace fails to load.** A site with no history, an
  unknown site id, a typo in a query string — every one of those is a 200 with nothing in it
  or a 422, never a 500 (MEM4).
* **Empty because the feature is off must not look like empty because the site is clean.**
  ``MEMORY_ENABLED`` defaults to false (§7.11.3), so on every deployment that has not opted in
  this route answers with ``enabled: false`` and an empty list, and the panel says so — the
  same honesty the Wallboard's ``STALE`` badges exist for (§7.10, the 3 a.m. rules).
* **It is operator-scoped like every other read.** Asserted end-to-end here, not only at the
  service level, because the route is where a future refactor would be tempted to build its
  own query.
* **It is inert.** Calling it writes nothing and changes nothing about the incident.

The response is an object rather than a bare list — see the module docstring of
``api/routers/memory.py`` for why, and note that ``facts`` is the M3 half shipping early and
empty so a renderer written today keeps working when it fills.
"""

from __future__ import annotations

import importlib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from noc_agents.db.models import IncidentRow, WorkNoteRow, get_session, utcnow
from noc_agents.realtime.hub import hub

SITE = "SFC-NBIE-HUB-EMB"
OTHER_SITE = "SFC-MTK-BTS-MCH04"
ROUTE = "/api/v1/memory/sites"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """The app on its own database file, built the way ``test_operator_isolation.py`` does it.

    ``main`` is reloaded because the module binds settings and the router table at import; the
    engine globals are cleared so the lifespan's ``init_db`` binds this file rather than the
    developer's ``data/noc_agents.db``.
    """
    db = tmp_path / "memory_api.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path / "ledgers"))

    import noc_agents.config as cfg
    import noc_agents.db.models as models
    import noc_agents.main as main

    cfg.clear_settings_cache()
    models._engine = None
    models.SessionLocal = None
    importlib.reload(main)
    hub._history.clear()
    with TestClient(main.app) as c:
        yield c
    hub._history.clear()
    cfg.clear_settings_cache()


def _seed(
    *,
    number: str,
    site_id: str = SITE,
    operator_id: str = "safaricom",
    days_ago: float = 5,
    resolution_summary: str = "Generator refuelled and mains restored",
    note: str | None = None,
) -> str:
    """One closed outage, written straight to the table the route reads."""
    ended = utcnow() - timedelta(days=days_ago)
    started = ended - timedelta(minutes=90)
    session = get_session()
    try:
        inc = IncidentRow(
            operator_id=operator_id,
            incident_number=number,
            status="CLOSED",
            priority="P2",
            users_affected=450_000,
            site_id=site_id,
            site_name=f"{site_id} site",
            site_type="HUB",
            region_code="NBI_E",
            failure_domain="POWER",
            alarm_code="POWER_GRID_FAIL",
            correlation_fingerprint=f"{site_id}|POWER_GRID_FAIL|POWER",
            created_at=started,
            outage_start_at=started,
            restored_at=ended,
            restored_source="MARK_RESTORED",
            closed_at=ended,
            resolution_code="FIELD_RESTORED",
            resolution_summary=resolution_summary,
        )
        session.add(inc)
        session.flush()
        if note:
            session.add(
                WorkNoteRow(
                    incident_id=inc.id,
                    author="Vendor Desk",
                    author_role="MSP",
                    body=note,
                    created_at=ended,
                    source="ui",
                )
            )
        session.commit()
        return inc.id
    finally:
        session.close()


def _get(client, site_id: str = SITE, **params):
    return client.get(f"{ROUTE}/{site_id}", params=params)


# =================================================================================
# The flag
# =================================================================================


def test_with_the_flag_off_the_route_answers_empty_and_says_why(client, monkeypatch):
    """``MEMORY_ENABLED`` defaults to false, and the off state is reported, not disguised.

    ``tests/conftest.py`` pins ``EMAIL_ENABLED``, ``LLM_ENABLED`` and ``SCHEDULER_ENABLED``
    but not ``MEMORY_ENABLED``, so a developer with it exported in their shell would otherwise
    run this assertion against a flag that is on. Deleted here rather than added to conftest
    because this lane does not own that file; pinning it there alongside the other three is
    the right home for it.
    """
    monkeypatch.delenv("MEMORY_ENABLED", raising=False)
    _seed(number="INC-HIST-1")
    body = _get(client).json()
    assert body["enabled"] is False
    assert body["degraded"] is True
    assert body["episodes"] == []
    assert body["facts"] == []


def test_with_the_flag_on_the_site_history_comes_back_newest_first(client, monkeypatch):
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    for i, days in enumerate((30, 2, 11)):
        _seed(number=f"INC-HIST-{i}", days_ago=days)

    body = _get(client).json()
    assert body["enabled"] is True
    assert body["degraded"] is False
    assert [e["incident_number"] for e in body["episodes"]] == ["INC-HIST-1", "INC-HIST-2", "INC-HIST-0"]
    assert body["site_id"] == SITE
    assert body["lookback_days"] == 365


def test_only_an_explicit_true_value_switches_recall_on(client, monkeypatch):
    """Same reading as ``WEATHER_ENABLED``: a stray value is off, never "probably on"."""
    for value, expected in (("true", True), ("1", True), ("on", True), ("false", False), ("maybe", False), ("", False)):
        monkeypatch.setenv("MEMORY_ENABLED", value)
        assert _get(client).json()["enabled"] is expected, value


# =================================================================================
# Degrading, not failing
# =================================================================================


def test_an_unknown_site_is_two_hundred_with_an_empty_list_not_a_four_oh_four(client, monkeypatch):
    """§7.11.4's route table says so explicitly, and the workspace is why.

    A site id is not a row id: "nothing has happened here" and "there is no such site" are the
    same answer to the question the panel asks, and a 404 would render an error where an empty
    panel is the truthful result.
    """
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    _seed(number="INC-HIST-1")
    r = _get(client, "NO-SUCH-SITE-ANYWHERE")
    assert r.status_code == 200, r.text
    assert r.json()["episodes"] == []


def test_a_site_with_no_history_is_two_hundred_with_an_empty_list(client, monkeypatch):
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    _seed(number="INC-HIST-1", site_id=OTHER_SITE)
    assert _get(client, SITE).json()["episodes"] == []


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 10_000},
        {"limit": "lots"},
        {"lookback_days": -1},
        {"lookback_days": "forever"},
    ],
)
def test_a_nonsense_query_string_is_rejected_rather_than_crashing(client, monkeypatch, params):
    """422, never 500: the bounds are declared on the route so a hand-typed URL cannot turn
    an advisory read into a full table scan or a stack trace."""
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    assert _get(client, SITE, **params).status_code == 422


def test_the_limit_and_lookback_parameters_do_what_they_say(client, monkeypatch):
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    _seed(number="INC-RECENT", days_ago=5)
    _seed(number="INC-OLD", days_ago=20)
    _seed(number="INC-ANCIENT", days_ago=800)

    assert [e["incident_number"] for e in _get(client, SITE, limit=1).json()["episodes"]] == ["INC-RECENT"]
    assert [e["incident_number"] for e in _get(client, SITE, lookback_days=10).json()["episodes"]] == ["INC-RECENT"]


# =================================================================================
# Shape, timestamps, isolation, inertness
# =================================================================================


def test_every_timestamp_on_the_wire_carries_an_explicit_z(client, monkeypatch):
    """Spec §7.0.6, defect #41. A naive ISO string is read by a browser as *local* time, so in
    Nairobi every episode would silently move three hours — and "the last outage here was at
    01:00, not 04:00" is exactly the quiet wrongness a memory panel must not introduce."""
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    _seed(number="INC-HIST-1")
    closed_at = _get(client).json()["episodes"][0]["closed_at"]
    assert isinstance(closed_at, str) and closed_at.endswith("Z"), closed_at


def test_the_episode_payload_is_the_shape_the_panel_renders(client, monkeypatch):
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    _seed(number="INC-HIST-1", resolution_summary="", note="Generator refuelled, SERVICE RESTORED")

    episode = _get(client).json()["episodes"][0]
    assert episode["incident_number"] == "INC-HIST-1"
    assert episode["site_id"] == SITE
    assert episode["fault_class"] == "POWER|POWER_GRID_FAIL|HUB"
    assert episode["restore_minutes"] == 90
    assert episode["resolution_code"] == "FIELD_RESTORED"
    assert "Generator refuelled" in episode["resolution_summary"]
    assert episode["match_reason"] == "same site"
    assert isinstance(episode["score"], float)


def test_the_route_never_returns_the_other_operators_history(client, monkeypatch):
    """The same colocation case as ``test_memory_recall.py``, asserted through HTTP.

    Both licensees fail at one mast; a safaricom request must see three rows, not six, and no
    airtel ticket number may appear anywhere in the response body.
    """
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    for i in range(3):
        _seed(number=f"SAF-{i}", operator_id="safaricom", days_ago=1 + i)
        _seed(number=f"ATL-{i}", operator_id="airtel", days_ago=1.5 + i)

    r = _get(client)
    assert r.status_code == 200
    assert [e["incident_number"] for e in r.json()["episodes"]] == ["SAF-0", "SAF-1", "SAF-2"]
    assert "ATL-" not in r.text, "an airtel incident number reached a safaricom response body"


def test_reading_memory_writes_nothing_and_changes_nothing(client, monkeypatch):
    """MEM1 at the API boundary: recall is advisory, so the ticket is byte-identical after it.

    Compared over every column rather than a chosen few, because the claim being made is
    "nothing", not "nothing important".
    """
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    incident_id = _seed(number="INC-HIST-1")

    def _row() -> dict:
        session = get_session()
        try:
            inc = session.scalar(select(IncidentRow).where(IncidentRow.id == incident_id))
            return {c.name: getattr(inc, c.name) for c in IncidentRow.__table__.columns}
        finally:
            session.close()

    before = _row()
    for _ in range(3):
        assert _get(client).status_code == 200
    assert _row() == before


def test_the_memory_route_is_registered_and_reachable_under_the_versioned_prefix(client):
    """It is included through ``api/routers/__init__.ROUTERS``; a lane owns one file and never
    edits ``main.py``. If the router stopped being registered, the SPA fallback would answer
    this path with ``index.html`` and a 200 — so the body is checked, not just the status."""
    r = _get(client)
    assert r.status_code == 200
    assert set(r.json()) == {"site_id", "enabled", "lookback_days", "episodes", "facts", "degraded"}


# =================================================================================
# M1: the additive ``advisory`` key on the single-incident serializer (§7.11.4, §7.11.5)
# =================================================================================
#
# The helper is ``services/memory.advisory_for_incident`` (pinned inert in
# ``tests/unit/test_memory_advisory_is_inert.py``); ``get_incident()`` in ``main.py``
# calls it and sets ``payload["advisory"]``. Nothing goes in ``api/serializers.py``:
# ``incident_out`` is shared with the LIST route, and §7.11.4 forbids ``advisory``
# there -- a list of thirty tickets would mean thirty recalls per refresh on the
# wallboard's polling interval. These three tests are the acceptance check for that
# wiring.


def test_the_single_incident_route_carries_the_advisory_key(client, monkeypatch):
    """§7.11.11 test 23: every field the UI reads is still there and ``advisory`` is additive."""
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    _seed(number="INC-HIST-1", days_ago=30)
    incident_id = _seed(number="INC-HIST-2", days_ago=1)

    body = client.get(f"/api/v1/incidents/{incident_id}").json()
    assert body["incident_number"] == "INC-HIST-2"
    assert body["site_id"] == SITE  # the existing shape is untouched
    assert body["advisory"]["enabled"] is True
    assert body["advisory"]["similar"], "the prior outage at this site should be recalled"


def test_with_the_flag_off_the_advisory_key_is_null_rather_than_absent(client, monkeypatch):
    """§7.11.11 test 25. ``null``, not missing: a renderer written against the key keeps
    working on every deployment that never opted in, and "off" is visible rather than
    indistinguishable from "this ticket has no history"."""
    monkeypatch.delenv("MEMORY_ENABLED", raising=False)
    incident_id = _seed(number="INC-HIST-1")
    assert client.get(f"/api/v1/incidents/{incident_id}").json()["advisory"] is None


def test_the_list_route_never_carries_an_advisory_key(client, monkeypatch):
    """§7.11.4 is explicit, and the reason is cost: the wallboard polls the list route, so an
    advisory there would be one recall per ticket per refresh — and none of it would be read,
    because the list shows one line per incident."""
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    _seed(number="INC-HIST-1")
    for row in client.get("/api/v1/incidents").json():
        assert "advisory" not in row


# --- Who the advisory is for (§9.3's memory row; RBAC review C2) --------------------------
#
# GET /incidents/{id} is gated on §9.3 row 1, which lets msp_coordinator and field_engineer
# read the ticket they are working. The advisory is not that ticket: it is EARLIER tickets at
# the site -- possibly worked by a different MSP -- and §9.3's memory row gives those two roles
# "—". So the route serves them the incident with ``advisory: null``, the flag-off shape.
# §7.11.4's "as today" says otherwise; api/deps.MEMORY_READERS records why §9.3 wins.

_ADVISORY_SECRET = "memory-advisory-secret"
#: §9.3's memory row, read cell, as a literal -- an independent statement, not the code's tuple.
_MEMORY_ROW = ("noc_analyst", "shift_supervisor", "duty_manager", "management", "planning", "legal", "admin")


def _signed_in(client, role: str) -> None:
    """A real signed session: with AUTH_DISABLED=false the cookie, not the switcher, is who you are."""
    from noc_agents.api import auth

    client.cookies.clear()
    client.cookies.set(
        auth.SESSION_COOKIE, auth.sign_session({"sub": f"u-{role}", "role": role, "name": role}, _ADVISORY_SECRET)
    )


@pytest.fixture()
def auth_enforced(monkeypatch):
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("NOC_SESSION_SECRET", _ADVISORY_SECRET)


def test_the_advisory_is_null_for_the_roles_the_memory_row_excludes(client, monkeypatch, auth_enforced):
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    _seed(number="INC-HIST-1", days_ago=30, note="Omondi from the other MSP swapped the ATS")
    incident_id = _seed(number="INC-HIST-2", days_ago=1)
    for role in ("msp_coordinator", "field_engineer"):
        _signed_in(client, role)
        r = client.get(f"/api/v1/incidents/{incident_id}")
        assert r.status_code == 200, role  # the ticket itself: row 1 still lets them read it
        body = r.json()
        assert body["incident_number"] == "INC-HIST-2", role
        assert "advisory" in body and body["advisory"] is None, (role, body.get("advisory"))
        assert "INC-HIST-1" not in r.text and "Omondi" not in r.text, role


def test_every_role_the_memory_row_admits_gets_the_advisory(client, monkeypatch, auth_enforced):
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    _seed(number="INC-HIST-1", days_ago=30)
    incident_id = _seed(number="INC-HIST-2", days_ago=1)
    for role in _MEMORY_ROW:
        _signed_in(client, role)
        body = client.get(f"/api/v1/incidents/{incident_id}").json()
        assert body["advisory"] and body["advisory"]["similar"], role


def test_the_demo_serves_the_advisory_whichever_role_the_switcher_shows(client, monkeypatch):
    """AUTH_DISABLED=true (the demo and suite default): there is no identity -- the switcher is a
    UI affordance and every gate is inert -- so the advisory is served exactly as before."""
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    _seed(number="INC-HIST-1", days_ago=30)
    incident_id = _seed(number="INC-HIST-2", days_ago=1)
    for role in ("msp_coordinator", "field_engineer"):
        assert client.post("/api/v1/session", json={"role": role, "display_name": "Tester"}).status_code == 200
        body = client.get(f"/api/v1/incidents/{incident_id}").json()
        assert body["advisory"] and body["advisory"]["similar"], role


def test_in_the_demo_every_role_the_switcher_offers_reads_the_route(client, monkeypatch):
    """With ``AUTH_DISABLED=true`` (the demo and test default) ``require_role`` never rejects,
    exactly like every other route. With auth ON the route is gated to ``deps.MEMORY_READERS``,
    which follows §9.3's memory row and so EXCLUDES field_engineer and msp_coordinator:
    §7.11.4 says "any signed-in role", but this route serves the same earlier-ticket episodes
    as the incident advisory, and §9.3 is the stricter reading (review C1/C2). That matrix is
    pinned in tests/system/test_auth.py."""
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    _seed(number="INC-HIST-1")
    for role in ("noc_analyst", "field_engineer", "msp_coordinator", "shift_supervisor"):
        client.post("/api/v1/session", json={"role": role, "display_name": "Tester"})
        assert _get(client).status_code == 200, role
