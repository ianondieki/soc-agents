from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "sys.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
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


def test_health_and_six_regions(client):
    r = client.get("/health")
    assert r.status_code == 200
    p = client.get("/api/v1/profile").json()
    assert p["operator_id"] == "safaricom"
    for code in ("NBI_E", "NBI_W", "MTK", "CST", "RFT", "WNY"):
        assert code in p["regions"], f"missing region {code}"
    assert p["regions"]["NBI_E"]["label"]


def test_system_ingest_inc9_msp_lifecycle(client):
    r = client.post(
        "/api/v1/events",
        json={
            "site_id": "SFC-NBIE-HUB-EMB",
            "site_name": "Embakasi East Aggregation HUB",
            "site_type": "HUB",
            "region_code": "NBI_E",
            "alarm_code": "POWER_GRID_FAIL",
            "failure_domain": "POWER",
            "users_affected": 450000,
            "access_notes": "Genset fail",
        },
    )
    assert r.status_code == 200
    inc = r.json()["incident"]
    assert inc["incident_number"].startswith("INC")
    assert len(inc["incident_number"]) == 9
    assert inc["priority"] == "P2"
    assert inc["responsible_msp"] == "EGYPRO"
    assert inc["escalated_at"]
    assert inc["expected_resolution_at"]
    assert inc["failure_time"]

    m = client.get("/api/v1/metrics/summary").json()
    assert m["open_total"] >= 1

    wf = client.get(f"/api/v1/incidents/{inc['id']}/workflow").json()
    assert any(n["status"] == "succeeded" for n in wf["nodes"])

    hitl = client.get("/api/v1/hitl/pending").json()
    assert len(hitl) >= 1
    task_id = hitl[0]["id"]
    assert client.post(f"/api/v1/hitl/{task_id}/claim", json={"resolved_by": "Supervisor A"}).status_code == 200
    assert client.post(f"/api/v1/hitl/{task_id}/approve", json={"resolved_by": "Supervisor A"}).status_code == 200

    note = client.post(
        f"/api/v1/incidents/{inc['id']}/notes",
        json={
            "author": "Egypro tech",
            "author_role": "MSP",
            "body": "On site, genset started",
            "vendor_tt_ref": "EGY-KE-1001",
            "msp_root_cause": "Grid failure; genset fuel low",
            "msp_action_taken": "Refuelled and started DG",
            "msp_percent_complete": 60,
        },
    )
    assert note.status_code == 200
    assert note.json()["status"] == "IN_PROGRESS"

    detail = client.get(f"/api/v1/incidents/{inc['id']}").json()
    assert detail["msp_percent_complete"] == 60
    assert detail["msp_root_cause"]
    assert detail["vendor_tt_ref"] == "EGY-KE-1001"

    # radio path Coast → Huawei
    rad = client.post(
        "/api/v1/events",
        json={
            "site_id": "SFC-CST-ENB-NYL12",
            "site_type": "ENODEB",
            "region_code": "CST",
            "alarm_code": "RADIO_CELL_DOWN",
            "failure_domain": "RADIO",
            "users_affected": 18000,
        },
    ).json()["incident"]
    assert rad["responsible_msp"] == "HUAWEI_RADIO" or rad["msp_name"] == "HUAWEI_RADIO"
    assert rad["radio_oem"] == "HUAWEI"

    ho = client.post("/api/v1/shifts/handover").json()
    assert ho["watch_count"] >= 1
