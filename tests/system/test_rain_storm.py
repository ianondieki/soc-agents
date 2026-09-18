from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "rain.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
    monkeypatch.setenv("LIVE_AGENT_DELAY_MS", "0")
    import importlib
    import noc_agents.config as cfg
    import noc_agents.db.models as models
    import noc_agents.main as main

    cfg.clear_settings_cache()
    models._engine = None
    models.SessionLocal = None
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c


def test_rain_storm_events_template(client):
    r = client.get("/api/v1/demo/rain-storm/events")
    assert r.status_code == 200
    body = r.json()
    assert len(body["events"]) >= 8
    regions = {e["region_code"] for e in body["events"]}
    assert "RFT" in regions and "MTK" in regions and "NBI_E" in regions
    # parents before useful cascade
    assert any(e.get("parent_hub_id") for e in body["events"])


def test_rain_storm_bulk_creates_hubs(client):
    r = client.post("/api/v1/demo/rain-storm")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 5
    # After cascade, open majors should include region HUBs
    inc = client.get("/api/v1/incidents").json()
    sites = {i["site_id"] for i in inc}
    assert "SFC-RFT-HUB-NKR" in sites or any("RFT" in i["region_code"] for i in inc)
    assert any(i["incident_number"].startswith("INC") for i in inc)
