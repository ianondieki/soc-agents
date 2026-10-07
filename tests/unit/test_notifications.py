"""``GET /api/v1/notifications`` — the notification centre's inbox (the bell in the top bar).

What is protected here:

1. **The shape.** Every item carries exactly ``ITEM_KEYS`` (null where a key does not apply),
   every timestamp is a ``Z`` string, the items are newest first and ``counts`` add up.
2. **What counts as attention.** An open P1 opened in the window; a restore clock run out on a
   ticket not yet restored; a card still waiting for a decision, whatever its age; a failed agent
   run. A closed P1, a restored ticket, a decided card and other items older than the window stay out.
3. **One operator.** Another operator's ticket, card and run reach no item and no count.
4. **Per role.** A signed-in role sees only the cards it may decide (the /hitl/pending rule);
   the route takes §9.3 row 1's read gate and refuses a vendor role.
5. **Read-only and bounded.** The window and the limit are validated; the route writes nothing.
"""

from __future__ import annotations

import importlib
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from noc_agents.api import auth
from noc_agents.db.models import AgentRunRow, AuditRow, HitlTaskRow, IncidentRow, new_id, utcnow
from noc_agents.domain.schemas import EventIngest
from noc_agents.graph.pipeline import process_event
from noc_agents.realtime.hub import hub
from noc_agents.services import notifications as svc

TIMESTAMP_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

HUB_EVENT = {  # P2 at L2_GUARDED: parks an APPROVE_BROADCAST card
    "site_id": "SFC-NBIE-HUB-EMB",
    "site_name": "Embakasi East Aggregation HUB",
    "site_type": "HUB",
    "region_code": "NBI_E",
    "county": "Nairobi",
    "alarm_code": "POWER_GRID_FAIL",
    "failure_domain": "POWER",
    "users_affected": 450000,
}


def _incident(session, *, operator_id="safaricom", priority="P1", status="TICKETED", created=None, restore_due=None, number=None):
    inc = IncidentRow(
        operator_id=operator_id,
        incident_number=number or f"INC-{new_id()[:8]}",
        site_id="SFC-NBIW-CORE-01",
        site_name="Upper Hill Core Router",
        region_code="NBI_W",
        correlation_fingerprint=new_id(),
        priority=priority,
        status=status,
        title="t",
        narrative="n",
        created_at=created or utcnow(),
        sla_restore_due=restore_due,
    )
    session.add(inc)
    session.flush()
    return inc


def _run(session, *, operator_id="safaricom", status="FAILED", incident_id=None, started=None, node="ENRICH", error="CMDB lookup timed out"):
    run = AgentRunRow(
        id=new_id(),
        incident_id=incident_id,
        operator_id=operator_id,
        graph_name="incident_lifecycle",
        trigger="EVENT",
        status=status,
        started_at=started or utcnow(),
        finished_at=started or utcnow(),
        current_node=node,
        error_summary=error,
    )
    session.add(run)
    session.flush()
    return run


# ======================================================================================
# 1. shape
# ======================================================================================


def test_an_empty_inbox_answers_with_zeros(tmp_db):
    _, session = tmp_db
    out = svc.notifications(session)
    assert set(out) == {"generated_at", "window_hours", "since", "counts", "total", "items"}
    assert out["counts"] == {"alarm": 0, "person": 0, "agent": 0}
    assert out["total"] == 0 and out["items"] == []
    assert out["window_hours"] == svc.DEFAULT_WINDOW_HOURS
    assert TIMESTAMP_Z.match(out["generated_at"]) and TIMESTAMP_Z.match(out["since"])


def test_every_item_has_the_pinned_keys_newest_first(tmp_db):
    settings, session = tmp_db
    process_event(session, settings, EventIngest(**HUB_EVENT))  # a P2 card waiting
    p1 = _incident(session, created=utcnow() - timedelta(hours=2))
    _run(session, incident_id=p1.id, started=utcnow() - timedelta(hours=1))
    session.commit()

    out = svc.notifications(session)
    assert out["items"], "the seed raises attention items"
    for it in out["items"]:
        assert tuple(it) == svc.ITEM_KEYS
        assert it["group"] == svc.GROUP_OF[it["kind"]]
        assert TIMESTAMP_Z.match(it["at"])
    ats = [it["at"] for it in out["items"]]
    assert ats == sorted(ats, reverse=True)
    assert sum(out["counts"].values()) == out["total"] == len(out["items"])


# ======================================================================================
# 2. what counts as attention
# ======================================================================================


def test_an_open_p1_in_the_window_is_an_alarm_and_a_closed_or_old_one_is_not(tmp_db):
    _, session = tmp_db
    live = _incident(session, number="INC-LIVE")
    _incident(session, status="CLOSED", number="INC-CLOSED")
    _incident(session, status="RESTORED", number="INC-RESTORED")
    _incident(session, created=utcnow() - timedelta(hours=30), number="INC-OLD")
    _incident(session, priority="P2", number="INC-P2")
    session.commit()

    items = [it for it in svc.notifications(session)["items"] if it["kind"] == "p1_open"]
    assert [it["incident_number"] for it in items] == ["INC-LIVE"]
    it = items[0]
    assert it["id"] == f"p1:{live.id}" and it["incident_id"] == live.id
    assert it["priority"] == "P1" and it["site_name"] == "Upper Hill Core Router" and it["region_code"] == "NBI_W"
    assert it["task_id"] is None and it["node"] is None
    # The old one comes back with a wider window.
    wide = svc.notifications(session, window_hours=48)["items"]
    assert {it["incident_number"] for it in wide if it["kind"] == "p1_open"} == {"INC-LIVE", "INC-OLD"}


def test_a_ticket_raised_to_p1_arrives_when_a_person_raised_it(tmp_db):
    """The Severity agent rated it P2; a person made it P1 approving the broadcast, a day later."""
    settings, session = tmp_db
    inc = process_event(session, settings, EventIngest(**HUB_EVENT))
    session.commit()
    assert inc.priority == "P2"
    assert not [it for it in svc.notifications(session)["items"] if it["kind"] == "p1_open"]

    # Opened 30 h ago (outside the window), raised to P1 by the approval 20 minutes ago.
    decided = (utcnow() - timedelta(minutes=20)).replace(microsecond=0)
    inc.created_at = utcnow() - timedelta(hours=30)
    task = session.scalar(select(HitlTaskRow).where(HitlTaskRow.incident_id == inc.id))
    task.status, task.resolved_at = "APPROVED", decided
    inc.priority, inc.updated_at = "P1", decided
    session.commit()

    items = [it for it in svc.notifications(session)["items"] if it["kind"] == "p1_open"]
    assert [it["incident_id"] for it in items] == [inc.id]
    assert items[0]["at"] == decided.strftime("%Y-%m-%dT%H:%M:%SZ"), "stamped when it became P1, not when it opened"


def test_a_restore_clock_run_out_counts_until_the_ticket_is_restored(tmp_db):
    _, session = tmp_db
    late = _incident(session, priority="P3", restore_due=utcnow() - timedelta(minutes=30), number="INC-LATE")
    _incident(session, priority="P3", restore_due=utcnow() + timedelta(hours=2), number="INC-NOT-YET")
    _incident(session, priority="P3", status="RESTORED", restore_due=utcnow() - timedelta(minutes=30), number="INC-RESTORED")
    session.commit()

    items = [it for it in svc.notifications(session)["items"] if it["kind"] == "restore_breached"]
    assert [it["incident_number"] for it in items] == ["INC-LATE"]
    assert items[0]["id"] == f"sla:{late.id}" and items[0]["group"] == "alarm"


def test_a_waiting_card_is_for_a_person_until_it_is_decided(tmp_db):
    settings, session = tmp_db
    inc = process_event(session, settings, EventIngest(**HUB_EVENT))
    session.commit()

    cards = [it for it in svc.notifications(session)["items"] if it["kind"] == "approval_waiting"]
    assert len(cards) == 1
    card = cards[0]
    task = session.scalar(select(HitlTaskRow).where(HitlTaskRow.incident_id == inc.id))
    assert card["id"] == f"hitl:{task.id}" and card["task_id"] == task.id
    assert card["task_type"] == task.task_type and card["group"] == "person"
    assert card["incident_number"] == inc.incident_number and card["priority"] == "P2"

    # Still waiting two days later: older is more urgent, so the window does not drop it.
    task.created_at = utcnow() - timedelta(hours=50)
    session.commit()
    assert [it["task_id"] for it in svc.notifications(session)["items"] if it["kind"] == "approval_waiting"] == [task.id]

    task.status = "APPROVED"
    session.commit()
    assert not [it for it in svc.notifications(session)["items"] if it["kind"] == "approval_waiting"]


def test_a_failed_run_is_an_agent_item_with_its_step_and_a_short_error(tmp_db):
    _, session = tmp_db
    inc = _incident(session, priority="P3")
    run = _run(session, incident_id=inc.id, error="x" * 1000)
    _run(session, status="SUCCEEDED", node="MONITOR", error=None)
    _run(session, started=utcnow() - timedelta(hours=40))
    session.commit()

    items = [it for it in svc.notifications(session)["items"] if it["kind"] == "run_failed"]
    assert len(items) == 1
    it = items[0]
    assert it["id"] == f"run:{run.id}" and it["group"] == "agent"
    assert it["node"] == "ENRICH" and it["graph"] == "incident_lifecycle"
    assert it["incident_number"] == inc.incident_number
    assert len(it["error"]) == svc.ERROR_CHARS and it["error"].endswith("…")


def test_the_limit_cuts_the_items_but_not_the_counts(tmp_db):
    _, session = tmp_db
    for i in range(5):
        _incident(session, created=utcnow() - timedelta(minutes=i + 1))
    session.commit()
    out = svc.notifications(session, limit=2)
    assert len(out["items"]) == 2 and out["total"] == 5 and out["counts"]["alarm"] == 5


def test_the_limit_is_applied_in_sql_and_keeps_the_newest_failed_runs(tmp_db):
    _, session = tmp_db
    runs = [_run(session, started=utcnow() - timedelta(minutes=i + 1)) for i in range(6)]
    session.commit()
    out = svc.notifications(session, limit=3)
    assert out["counts"]["agent"] == 6 and out["total"] == 6
    assert [it["id"] for it in out["items"]] == [f"run:{r.id}" for r in runs[:3]]


# ======================================================================================
# 3. one operator
# ======================================================================================


def test_the_other_operators_rows_reach_no_item(tmp_db):
    _, session = tmp_db
    theirs = _incident(session, operator_id="airtel", number="ATL-INC-1", restore_due=utcnow() - timedelta(minutes=5))
    session.add(HitlTaskRow(operator_id="airtel", incident_id=theirs.id, task_type="APPROVE_BROADCAST", status="PENDING"))
    _run(session, operator_id="airtel", incident_id=theirs.id)
    session.commit()

    out = svc.notifications(session)
    assert out["items"] == [] and out["counts"] == {"alarm": 0, "person": 0, "agent": 0}


def test_our_run_pointing_at_another_operators_ticket_names_no_ticket(tmp_db):
    """The incident lookup behind a run is operator-scoped too. (A card cannot point across:
    db.models refuses a task whose operator differs from its incident's, HitlTaskOwnershipError.)"""
    _, session = tmp_db
    theirs = _incident(session, operator_id="airtel", number="ATL-INC-2")
    _run(session, operator_id="safaricom", incident_id=theirs.id)
    session.commit()

    items = svc.notifications(session)["items"]
    assert [it["kind"] for it in items] == ["run_failed"]
    assert items[0]["incident_id"] is None and items[0]["incident_number"] is None and items[0]["site_name"] is None


# ======================================================================================
# 4. per role
# ======================================================================================


def test_a_signed_in_role_sees_only_the_cards_it_may_decide(tmp_db):
    settings, session = tmp_db
    process_event(session, settings, EventIngest(**HUB_EVENT))  # an APPROVE_BROADCAST card
    session.commit()

    def cards(**who):
        return [it for it in svc.notifications(session, **who)["items"] if it["kind"] == "approval_waiting"]

    assert len(cards()) == 1, "the demo (not signed in) sees every card, as /hitl/pending does"
    assert len(cards(role="shift_supervisor", authenticated=True)) == 1
    assert cards(role="noc_analyst", authenticated=True) == [], "§9.3 row 2 gives noc_analyst no decision"
    assert cards(role="planning", authenticated=True) == []


# ======================================================================================
# 5. over HTTP
# ======================================================================================

SECRET = "unit-test-secret"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    def make(auth_on: bool = False) -> TestClient:
        db = tmp_path / ("auth.db" if auth_on else "demo.db")
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
        monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
        if auth_on:
            monkeypatch.setenv("AUTH_DISABLED", "false")
            monkeypatch.setenv("NOC_SESSION_SECRET", SECRET)
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
        made.append(c)
        return c

    made: list[TestClient] = []
    yield make
    for c in made:
        c.__exit__(None, None, None)
    for key in ("AUTH_DISABLED", "NOC_SESSION_SECRET"):
        monkeypatch.delenv(key, raising=False)
    hub._history.clear()
    auth.reset_sessions()
    import noc_agents.config as cfg
    import noc_agents.db.models as models
    import noc_agents.main as main

    models._engine = None
    models.SessionLocal = None
    cfg.clear_settings_cache()
    importlib.reload(main)


def test_the_route_serves_the_inbox_validates_and_writes_nothing(client):
    c = client()
    assert c.post("/api/v1/events", json=HUB_EVENT).status_code == 200

    from noc_agents.db.models import get_session

    session = get_session()
    try:
        before = (session.scalar(select(func.count()).select_from(AuditRow)), session.scalar(select(func.count()).select_from(HitlTaskRow)))
    finally:
        session.close()

    r = c.get("/api/v1/notifications")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["counts"]["person"] == 1
    assert body["items"][0]["kind"] == "approval_waiting"

    assert c.get("/api/v1/notifications?window_hours=0").status_code == 422
    assert c.get(f"/api/v1/notifications?window_hours={svc.MAX_WINDOW_HOURS + 1}").status_code == 422
    assert c.get(f"/api/v1/notifications?limit={svc.MAX_LIMIT + 1}").status_code == 422
    assert c.get("/api/v1/notifications?limit=1").json()["items"].__len__() == 1

    session = get_session()
    try:
        after = (session.scalar(select(func.count()).select_from(AuditRow)), session.scalar(select(func.count()).select_from(HitlTaskRow)))
    finally:
        session.close()
    assert after == before, "reading the inbox writes nothing"


def test_the_route_passes_the_signed_in_role_to_the_card_filter(client):
    c = client(auth_on=True)
    c.cookies.set(auth.SESSION_COOKIE, auth.sign_session({"sub": "u-0", "role": "noc_analyst"}, SECRET))
    assert c.post("/api/v1/events", json=HUB_EVENT).status_code == 200  # parks an APPROVE_BROADCAST card

    body = c.get("/api/v1/notifications").json()
    assert body["counts"]["person"] == 0, "§9.3 row 2 gives noc_analyst no decision, so no card"
    assert not [it for it in body["items"] if it["kind"] == "approval_waiting"]

    c.cookies.set(auth.SESSION_COOKIE, auth.sign_session({"sub": "u-3", "role": "shift_supervisor"}, SECRET))
    body = c.get("/api/v1/notifications").json()
    assert body["counts"]["person"] == 1
    assert [it["task_type"] for it in body["items"] if it["kind"] == "approval_waiting"] == ["APPROVE_BROADCAST"]


def test_the_route_takes_the_row_one_read_gate(client):
    c = client(auth_on=True)
    assert c.get("/api/v1/notifications").status_code == 401
    c.cookies.set(auth.SESSION_COOKIE, auth.sign_session({"sub": "u-1", "role": "msp_coordinator"}, SECRET))
    assert c.get("/api/v1/notifications").status_code == 403
    c.cookies.set(auth.SESSION_COOKIE, auth.sign_session({"sub": "u-2", "role": "noc_analyst"}, SECRET))
    assert c.get("/api/v1/notifications").status_code == 200
