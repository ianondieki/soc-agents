"""Spec §7.7.1 / §7.7.3 — known errors on ``problems``, and what they surface.

ITIL calls a problem whose cause is understood and which has a workaround a **known
error**. The columns for that arrived with Phase 4 (``root_cause``, ``workaround``,
``is_known_error``, ``known_error_since``, ``permanent_fix_plan``, ``owner_token``,
``target_date``, ``closed_at``, ``closure_summary``) and until this lane nothing read
them — which is the same as not having them.

The behaviour worth protecting is one sentence long: once a published review has written
down the cause and the workaround, the analyst who picks up the *next* incident at that
site, for that failure domain, at 03:00, is shown the answer instead of rediscovering it.
Everything below is a way of pinning that sentence, plus the two ways it silently breaks —
a signature that does not match the one RECURRENCE computes (defect #34), and a closed
known error that keeps surfacing long after the permanent fix landed.

``owner_token`` gets its own tests because §7.7.6 is explicit that these fields carry role
tokens, not names: a problem record outlives every person attached to it, and "the fix is
owned by Kevin" is a dead field the week Kevin changes team.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from noc_agents.agents.recurrence import problem_signature
from noc_agents.api import auth
from noc_agents.db.models import AuditRow, IncidentRow, ProblemRow
from noc_agents.realtime.hub import hub
from noc_agents.services import pir as pir_service

T0 = datetime(2026, 9, 16, 1, 0, 0)
SITE = "SFC-MTK-HUB-THK"


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.setenv(pir_service.ENABLED_ENV, "true")


def _incident(session, *, number: str = "INC000001", domain: str = "POWER", site: str = SITE, **overrides) -> IncidentRow:
    values = dict(
        operator_id="safaricom",
        incident_number=number,
        status="NEW",
        priority="P2",
        site_id=site,
        site_type="HUB",
        region_code="MTK",
        correlation_fingerprint=f"fp-{number}",
        failure_domain=domain,
        alarm_code="PWR_MAINS_FAIL",
        failure_time=T0,
        created_at=T0,
    )
    values.update(overrides)
    inc = IncidentRow(**values)
    session.add(inc)
    session.flush()
    return inc


def _problem(session, *, domain: str = "POWER", site: str = SITE, **overrides) -> ProblemRow:
    values = dict(
        operator_id="safaricom",
        problem_number="PRB000001",
        signature=problem_signature(site, domain),
        site_id=site,
        region_code="MTK",
        occurrence_count=3,
        dominant_failure_domain=domain,
        first_seen=T0 - timedelta(days=20),
        last_seen=T0 - timedelta(days=1),
        summary=f"Recurring {domain} at {site}",
    )
    values.update(overrides)
    problem = ProblemRow(**values)
    session.add(problem)
    session.flush()
    return problem


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A live API on its own SQLite file (the reload pattern the other route tests use)."""
    db = tmp_path / "known_error.db"
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
    try:
        yield c
    finally:
        c.__exit__(None, None, None)
        hub._history.clear()
        auth.reset_sessions()
        models._engine = None
        models.SessionLocal = None
        cfg.clear_settings_cache()
        importlib.reload(main)


def _api_session():
    from noc_agents.db.models import get_session

    return get_session()


# --------------------------------------------------------------------------
# The surfacing rule
# --------------------------------------------------------------------------


def test_an_ordinary_problem_record_surfaces_nothing(tmp_db, on):
    """A recurring fault is not yet a known error: nobody has written down the cause or a
    workaround, so there is nothing to put in front of the next analyst."""
    _settings, session = tmp_db
    _problem(session)
    inc = _incident(session, number="INC000002")
    session.commit()

    assert pir_service.known_error_for_incident(session, inc) is None


def test_a_known_error_surfaces_on_the_next_incident_with_the_same_signature(tmp_db, on):
    """The whole commercial value of problem management, in one assertion."""
    _settings, session = tmp_db
    _problem(
        session,
        root_cause="Rectifier module 2 fails above 34 °C; the site has no forced ventilation",
        workaround="Force the load onto module 1 and run the portable fan until the rectifier is swapped",
        is_known_error=1,
        known_error_since=T0 - timedelta(days=5),
        permanent_fix_plan="Replace the rectifier shelf and fit a ventilation kit",
        owner_token="MSP_POWER",
    )
    inc = _incident(session, number="INC000003")
    session.commit()

    known_error = pir_service.known_error_for_incident(session, inc)
    assert known_error is not None
    assert known_error["problem_number"] == "PRB000001"
    assert "module 1" in known_error["workaround"]
    assert known_error["owner_token"] == "MSP_POWER"


def test_a_different_failure_domain_at_the_same_site_is_a_different_problem(tmp_db, on):
    """The signature is ``site|domain`` (defect #34). A transmission fault at a site with a
    known power error must not be handed a power workaround — that is how an analyst loses
    twenty minutes on the wrong hypothesis."""
    _settings, session = tmp_db
    _problem(session, domain="POWER", is_known_error=1, workaround="Force the load onto module 1")
    inc = _incident(session, number="INC000004", domain="TRANSMISSION")
    session.commit()

    assert pir_service.known_error_for_incident(session, inc) is None


def test_a_different_site_in_the_same_domain_is_a_different_problem(tmp_db, on):
    _settings, session = tmp_db
    _problem(session, is_known_error=1, workaround="Force the load onto module 1")
    inc = _incident(session, number="INC000005", site="SFC-NBI-BTS-777")
    session.commit()

    assert pir_service.known_error_for_incident(session, inc) is None


def test_the_signature_is_exactly_the_one_recurrence_computes(tmp_db, on):
    """Sourced from ``agents.recurrence.problem_signature`` rather than re-derived here, so a
    known error can only fail to match if the problem would also have failed to open. Before
    defect #34 the two disagreed and a chronic site opened two PRBs, neither of which a known
    error could attach to."""
    _settings, session = tmp_db
    problem = _problem(session, is_known_error=1, workaround="w")
    inc = _incident(session, number="INC000006")
    session.commit()

    assert problem.signature == problem_signature(inc.site_id, inc.failure_domain)
    assert pir_service.known_error_for_incident(session, inc)["signature"] == problem.signature


def test_a_closed_known_error_stops_surfacing(tmp_db, on):
    """Once the permanent fix has landed, the workaround is actively misleading advice."""
    _settings, session = tmp_db
    _problem(
        session,
        is_known_error=1,
        workaround="Force the load onto module 1",
        closed_at=T0 - timedelta(days=1),
        status="CLOSED",
        closure_summary="Rectifier shelf replaced and ventilation kit fitted",
    )
    inc = _incident(session, number="INC000007")
    session.commit()

    assert pir_service.known_error_for_incident(session, inc) is None


def test_another_operators_known_error_never_surfaces(tmp_db, on):
    """Operator isolation is a property of the query, not of the deployment (§8)."""
    _settings, session = tmp_db
    _problem(session, operator_id="othertel", is_known_error=1, workaround="their workaround")
    inc = _incident(session, number="INC000008")
    session.commit()

    assert pir_service.known_error_for_incident(session, inc) is None


def test_the_most_recently_recorded_known_error_wins(tmp_db, on):
    """Two PRBs can carry the same signature across a status change. The newer write is the
    current understanding; showing the older one hands the analyst last quarter's answer."""
    _settings, session = tmp_db
    _problem(
        session,
        problem_number="PRB000001",
        is_known_error=1,
        workaround="old workaround",
        known_error_since=T0 - timedelta(days=30),
    )
    _problem(
        session,
        problem_number="PRB000002",
        is_known_error=1,
        workaround="current workaround",
        known_error_since=T0 - timedelta(days=2),
    )
    inc = _incident(session, number="INC000009")
    session.commit()

    assert pir_service.known_error_for_incident(session, inc)["workaround"] == "current workaround"


# --------------------------------------------------------------------------
# The note the analyst actually reads
# --------------------------------------------------------------------------


def test_the_note_leads_with_the_workaround_and_names_only_a_role(tmp_db, on):
    _settings, session = tmp_db
    _problem(
        session,
        root_cause="Rectifier module 2 fails above 34 °C",
        workaround="Force the load onto module 1",
        is_known_error=1,
        permanent_fix_plan="Replace the rectifier shelf",
        owner_token="MSP_POWER",
    )
    inc = _incident(session, number="INC000010")
    session.commit()

    note = pir_service.known_error_note(pir_service.known_error_for_incident(session, inc))
    assert note.startswith("KNOWN ERROR PRB000001")
    assert "Force the load onto module 1" in note
    assert "MSP_POWER" in note
    # §7.7.6: role tokens, not names — the note is written into an incident others will read.
    assert not pir_service.blameless_violation(note, ["Kevin Ochieng", "Grace Wanjiru"])


def test_a_known_error_with_no_permanent_fix_yet_still_gives_the_workaround(tmp_db, on):
    """The common case: the cause is understood and a workaround exists, but the capital
    spend is not approved. The note must be useful anyway rather than wait for the plan."""
    _settings, session = tmp_db
    _problem(session, is_known_error=1, workaround="Force the load onto module 1", root_cause=None)
    inc = _incident(session, number="INC000011")
    session.commit()

    note = pir_service.known_error_note(pir_service.known_error_for_incident(session, inc))
    assert "Force the load onto module 1" in note
    assert "Permanent fix" not in note


# --------------------------------------------------------------------------
# PATCH /api/v1/problems/{id} — how a known error gets recorded
# --------------------------------------------------------------------------


def test_the_route_is_a_404_while_the_flag_is_off(client, monkeypatch):
    monkeypatch.setenv(pir_service.ENABLED_ENV, "false")
    session = _api_session()
    try:
        problem = _problem(session)
        session.commit()
        problem_id = problem.id
    finally:
        session.close()

    assert client.patch(f"/api/v1/problems/{problem_id}", json={"workaround": "x"}).status_code == 404
    assert client.get("/api/v1/incidents/anything/known-error").status_code == 404


def test_recording_a_known_error_stamps_known_error_since_from_the_system_clock(client, on):
    """A hand-entered "since" is the first field that gets back-dated in a dispute about how
    long an operator sat on a fault it understood, so the system stamps it."""
    session = _api_session()
    try:
        problem = _problem(session)
        session.commit()
        problem_id = problem.id
    finally:
        session.close()

    response = client.patch(
        f"/api/v1/problems/{problem_id}",
        json={
            "root_cause": "Rectifier module 2 fails above 34 °C",
            "workaround": "Force the load onto module 1",
            "is_known_error": True,
            "permanent_fix_plan": "Replace the rectifier shelf",
            "owner_token": "MSP_POWER",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_known_error"] == 1
    assert body["known_error_since"] is not None
    assert body["owner_token"] == "MSP_POWER"


def test_the_route_makes_the_known_error_visible_to_the_next_incident(client, on):
    """End to end, through the API only: record it, then open the next incident at that site
    and ask the workspace route what it should show."""
    session = _api_session()
    try:
        problem = _problem(session)
        inc = _incident(session, number="INC000020")
        session.commit()
        problem_id, incident_id = problem.id, inc.id
    finally:
        session.close()

    assert client.get(f"/api/v1/incidents/{incident_id}/known-error").json()["known_error"] is None

    client.patch(
        f"/api/v1/problems/{problem_id}",
        json={"workaround": "Force the load onto module 1", "is_known_error": True, "owner_token": "MSP_POWER"},
    )

    body = client.get(f"/api/v1/incidents/{incident_id}/known-error").json()
    assert body["known_error"]["problem_number"] == "PRB000001"
    assert "module 1" in body["note"]


def test_closing_the_problem_through_the_route_stops_the_surfacing(client, on):
    session = _api_session()
    try:
        problem = _problem(session, is_known_error=1, workaround="Force the load onto module 1")
        inc = _incident(session, number="INC000021")
        session.commit()
        problem_id, incident_id = problem.id, inc.id
    finally:
        session.close()

    assert client.get(f"/api/v1/incidents/{incident_id}/known-error").json()["known_error"] is not None

    response = client.patch(
        f"/api/v1/problems/{problem_id}",
        json={"closed": True, "closure_summary": "Rectifier shelf replaced"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "CLOSED"
    assert client.get(f"/api/v1/incidents/{incident_id}/known-error").json()["known_error"] is None


def test_an_owner_token_written_as_a_persons_name_is_refused(client, on):
    """No incident is in scope here, so the check is structural: anything written the way a
    name is written does not get through, and the field keeps meaning "which team owns it"."""
    session = _api_session()
    try:
        problem = _problem(session)
        session.commit()
        problem_id = problem.id
    finally:
        session.close()

    response = client.patch(f"/api/v1/problems/{problem_id}", json={"owner_token": "Kevin Ochieng"})
    assert response.status_code == 422
    assert "role token" in response.json()["detail"]
    assert client.patch(f"/api/v1/problems/{problem_id}", json={"owner_token": "FE_CENTRAL"}).status_code == 200


def test_writing_up_a_known_error_does_not_bump_last_seen(client, on):
    """``last_seen`` is the recurrence window RECURRENCE counts over. Documenting a fault is
    not the site failing again, and bumping it here would quietly extend that window."""
    session = _api_session()
    try:
        problem = _problem(session)
        session.commit()
        problem_id, before = problem.id, problem.last_seen
    finally:
        session.close()

    client.patch(f"/api/v1/problems/{problem_id}", json={"workaround": "Force the load onto module 1"})

    session = _api_session()
    try:
        after = session.get(ProblemRow, problem_id).last_seen
        assert after == before
    finally:
        session.close()


def test_recording_a_known_error_leaves_an_audit_row(client, on):
    """The known-error text ends up in front of every analyst who sees the site again, so who
    wrote it and when is part of the operational record (licence Condition 12.2)."""
    session = _api_session()
    try:
        problem = _problem(session)
        session.commit()
        problem_id = problem.id
    finally:
        session.close()

    client.patch(f"/api/v1/problems/{problem_id}", json={"is_known_error": True, "workaround": "w"})

    session = _api_session()
    try:
        row = session.scalar(select(AuditRow).where(AuditRow.entity_id == problem_id))
        assert row is not None
        assert row.action == "problem.known_error_updated"
        assert row.entity_type == "problem"
    finally:
        session.close()


def test_another_operators_problem_is_a_404_not_a_403(client, on):
    """404 is indistinguishable from "no such id"; a 403 would confirm the row exists in the
    other operator's data, which is the fact the scoping protects."""
    session = _api_session()
    try:
        problem = _problem(session, operator_id="othertel", problem_number="PRB000099")
        session.commit()
        problem_id = problem.id
    finally:
        session.close()

    assert client.patch(f"/api/v1/problems/{problem_id}", json={"workaround": "x"}).status_code == 404
