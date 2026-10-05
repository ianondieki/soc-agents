"""The close-the-loop demo (docs/CLOSE_THE_LOOP.md section 6): after the rain storm, the support seed
leaves complaints on every open storm incident (one number twice) and a Rongai burst that opens one
surge card; restoring the storm's P3 tells its customers at once, restoring a P2 raises the card.
Each test gets its own database, because the storm and the seed are the whole point."""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from noc_agents.api import auth
from noc_agents.realtime.hub import hub
from noc_agents.support.ratelimit import complaint_limiter

BASE = "/api/v1/support"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'demo.db').as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
    monkeypatch.setenv("AUTH_DISABLED", "true")
    monkeypatch.setenv("NOC_ENV", "demo")
    monkeypatch.setenv("SUPPORT_DESK_ENABLED", "true")

    import noc_agents.config as cfg
    import noc_agents.db.models as models
    import noc_agents.main as main

    cfg.clear_settings_cache()
    models._engine = None
    models.SessionLocal = None
    importlib.reload(main)
    auth.reset_sessions()
    hub._history.clear()
    complaint_limiter.reset()
    with TestClient(main.app) as c:
        yield c
    hub._history.clear()
    complaint_limiter.reset()
    models._engine = None
    models.SessionLocal = None
    cfg.clear_settings_cache()


def _surge_cards(client):
    return [t for t in client.get("/api/v1/hitl/pending").json() if t["task_type"] == "CONFIRM_POSSIBLE_OUTAGE"]


def test_the_storm_demo_closes_the_loop(client):
    assert client.post("/api/v1/demo/rain-storm").status_code == 200
    created = client.post(f"{BASE}/demo/seed").json()["created"]
    assert created >= 13 + 3
    # Seeding again inside two minutes creates nothing (the desk's dedupe) and no second card.
    assert client.post(f"{BASE}/demo/seed").json()["created"] == 0
    [card] = _surge_cards(client)
    assert (card["proposed_payload"]["place"], card["proposed_payload"]["numbers"]) == ("Rongai", 3)
    incidents = {i["incident_number"]: i for i in client.get("/api/v1/incidents").json()}
    outages = {o["incident_number"]: o for o in client.get(f"{BASE}/outages").json()}
    open_storm = [i for i in incidents.values() if i["status"] not in ("RESTORED", "CLOSED", "CANCELLED")]
    assert open_storm and all(outages[i["incident_number"]]["customers"] >= 2 for i in open_storm)
    assert client.get(f"{BASE}/loop").json()["repeat_contacts"] >= 1  # one number complained twice
    p3 = next(i for i in open_storm if i["priority"] == "P3")
    p2 = next(i for i in open_storm if i["priority"] == "P2")
    assert client.post(f"/api/v1/incidents/{p3['id']}/restore", json={"note": "Ring node back"}).status_code == 200
    panel = client.get(f"{BASE}/incidents/{p3['id']}/customers").json()
    assert panel["notice"]["state"] == "sent" and panel["told"] == panel["customers"] >= 2
    first = client.get(f"{BASE}/complaints/{panel['complaints'][0]['id']}").json()
    assert first["messages"][-1]["body"].startswith(("Service is back in ", "Huduma imerejea "))
    assert client.post(f"/api/v1/incidents/{p2['id']}/restore", json={"note": "HUB back"}).status_code == 200
    cards = [t for t in client.get("/api/v1/hitl/pending").json()
             if t["task_type"] == "APPROVE_CUSTOMER_UPDATE" and t["incident_id"] == p2["id"]]
    assert len(cards) == 1 and cards[0]["proposed_payload"]["recipients"] == outages[p2["incident_number"]]["customers"]
    assert len(_surge_cards(client)) == 1


def test_on_a_quiet_network_the_seed_only_adds_the_rongai_burst(client):
    client.post(f"{BASE}/demo/seed")
    assert client.get(f"{BASE}/outages").json() == []  # nothing open, so nothing linked
    [card] = _surge_cards(client)
    assert card["proposed_payload"]["place"] == "Rongai"
    surges = client.get(f"{BASE}/surges").json()
    assert [s["place"] for s in surges] == ["Rongai"]  # the lone Nakuru and Kayole complaints are not a surge
