"""CORRELATE folds the same fault into a ticket that is still open, however old it is.

A second storm over sites still down used to open a second ticket each (with its own
broadcast card), because a repeat folded only into tickets created in the last 15 minutes.
Now the window applies only to a ticket already restored: a site flapping straight after it
came back still folds in; one that fails again later opens a new ticket (and Recurrence
counts it).
"""

from __future__ import annotations

import importlib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from noc_agents.db.models import AgentRunRow, AgentRunStepRow, IncidentRow, get_session, utcnow
from noc_agents.realtime.hub import hub

EVENT = {
    "site_id": "SFC-NBIE-HUB-EMB",
    "site_name": "Embakasi East Aggregation HUB",
    "site_type": "HUB",
    "region_code": "NBI_E",
    "alarm_code": "POWER_GRID_FAIL",
    "failure_domain": "POWER",
    "users_affected": 450000,
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'correlate.db').as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
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


def _age(incident_id: str, minutes: int) -> None:
    session = get_session()
    try:
        session.get(IncidentRow, incident_id).created_at = utcnow() - timedelta(minutes=minutes)
        session.commit()
    finally:
        session.close()


def _open_count() -> int:
    session = get_session()
    try:
        return len(session.scalars(select(IncidentRow).where(IncidentRow.status.not_in(["RESTORED", "CLOSED", "CANCELLED"]))).all())
    finally:
        session.close()


def _last_correlate_rationale() -> str:
    session = get_session()
    try:
        step = session.scalar(
            select(AgentRunStepRow)
            .join(AgentRunRow, AgentRunRow.id == AgentRunStepRow.run_id)
            .where(AgentRunStepRow.node_name == "CORRELATE")
            .order_by(AgentRunRow.started_at.desc())
        )
        return step.rationale if step else ""
    finally:
        session.close()


def test_a_repeat_long_after_the_window_folds_into_the_ticket_still_open(client):
    first = client.post("/api/v1/events", json=EVENT).json()["incident"]
    _age(first["id"], 180)
    again = client.post("/api/v1/events", json=EVENT).json()["incident"]
    assert again["id"] == first["id"]
    assert _open_count() == 1
    assert "still open" in _last_correlate_rationale()


def test_a_repeat_inside_the_window_keeps_its_pinned_wording(client):
    first = client.post("/api/v1/events", json=EVENT).json()["incident"]
    again = client.post("/api/v1/events", json=EVENT).json()["incident"]
    assert again["id"] == first["id"]
    assert _last_correlate_rationale() == "Duplicate within 15m window — idempotent merge"


def test_a_restored_site_flapping_inside_the_window_folds_in(client):
    first = client.post("/api/v1/events", json=EVENT).json()["incident"]
    client.post(f"/api/v1/incidents/{first['id']}/restore", json={"note": "back"})
    again = client.post("/api/v1/events", json=EVENT).json()["incident"]
    assert again["id"] == first["id"]


def test_a_restored_site_failing_again_later_opens_a_new_ticket(client):
    first = client.post("/api/v1/events", json=EVENT).json()["incident"]
    client.post(f"/api/v1/incidents/{first['id']}/restore", json={"note": "back"})
    _age(first["id"], 60)
    again = client.post("/api/v1/events", json=EVENT).json()["incident"]
    assert again["id"] != first["id"]
    assert _open_count() == 1
