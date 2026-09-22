"""Incident-less HITL cards through main.py's HITL routes (schema v8).

Since v8 a maintenance card -- APPROVE_SCHEDULE, APPROVE_MAINTENANCE_WINDOW -- has
``incident_id`` NULL: it is about a programme of work or a night's planned outage, not an
incident. ``hitl_pending``, ``hitl_claim``, ``hitl_approve`` and ``hitl_reject`` each looked the
task's incident up with ``session.get(IncidentRow, t.incident_id)``. With a NULL id SQLAlchemy
2.0.51 returns None but emits ``SAWarning: fully NULL primary key identity cannot load any
object. This condition may raise an error in a future release`` -- and pyproject pins only a
floor (``sqlalchemy>=2.0.36``), so "a future release" is one ``pip install -U`` away from
blanking the HITL inbox for every supervisor.

These tests turn that warning into an error, so the unguarded lookup fails today rather than
on the day the library changes its mind.
"""

from __future__ import annotations

import importlib
import warnings

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SAWarning


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A live API on its own SQLite file (the reload pattern the route tests share)."""
    db = tmp_path / "hitl_no_incident.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")

    import noc_agents.config as cfg
    import noc_agents.db.models as models
    import noc_agents.main as main

    cfg.clear_settings_cache()
    models._engine = None
    models.SessionLocal = None
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c


@pytest.fixture()
def null_identity_is_an_error():
    """Every SAWarning raised while the routes run -- in the worker thread too, because the
    warnings filter list is process-wide -- becomes an exception, and so a 500 / a raise here."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", SAWarning)
        yield


def _maintenance_window_card() -> str:
    """An APPROVE_MAINTENANCE_WINDOW card as the v8 maintenance lane writes it: no incident,
    owned directly through ``operator_id``."""
    from noc_agents.config import get_settings
    from noc_agents.db.models import HitlTaskRow, get_session

    session = get_session()
    try:
        card = HitlTaskRow(
            incident_id=None,
            operator_id=get_settings().operator.operator_id,
            task_type="APPROVE_MAINTENANCE_WINDOW",
            entity_type="maintenance_window",
            entity_id="win-test-1",
            created_by="planning-desk",
            status="PENDING",
        )
        # starts_at_eat as the backend writes it: services/clock.fmt_eat output, which already
        # ends in " EAT" (the card heading in frontend/src/lib/hitlSubject.ts must not add another).
        card.proposed_payload = {
            "window": {"scope": "SITE", "scope_ref": "SFC-CST-HUB-MSA", "starts_at_eat": "2026-09-22 01:00 EAT"}
        }
        session.add(card)
        session.commit()
        return card.id
    finally:
        session.close()


def test_the_inbox_lists_an_incident_less_card(client, null_identity_is_an_error):
    task_id = _maintenance_window_card()
    r = client.get("/api/v1/hitl/pending")
    assert r.status_code == 200, r.text
    card = next(t for t in r.json() if t["id"] == task_id)
    assert card["incident_id"] is None
    assert card["incident_number"] is None and card["priority"] is None and card["site_id"] is None
    assert card["task_type"] == "APPROVE_MAINTENANCE_WINDOW"


def test_an_incident_less_card_can_be_claimed_approved_and_rejected(client, null_identity_is_an_error):
    approved, rejected = _maintenance_window_card(), _maintenance_window_card()
    decision = {"resolved_by": "Supervisor A", "reason": "window checked against the rains"}

    assert client.post(f"/api/v1/hitl/{approved}/claim", json=decision).status_code == 200
    assert client.post(f"/api/v1/hitl/{approved}/approve", json=decision).status_code == 200
    r = client.post(f"/api/v1/hitl/{rejected}/reject", json={**decision, "reason": "clashes with the long rains"})
    assert r.status_code == 200, r.text

    still_pending = {t["id"] for t in client.get("/api/v1/hitl/pending").json()}
    assert approved not in still_pending and rejected not in still_pending
