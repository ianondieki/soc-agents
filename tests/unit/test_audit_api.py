"""``GET /api/v1/audit`` carries ``run_id`` and ``node`` per row (the Audit trail page).

What is pinned:

* a lifecycle step writes its audit payload as JSON with ``node``, ``run_id`` and ``output``
  (it used to be a Python ``str()`` repr nothing could parse);
* every listed row has both keys; a step row's ``run_id`` is its run, so the intake steps
  written before the ticket existed (``entity_id == ""``) share it with the ticket's steps;
* rows written before the change still list: a repr payload gives its ``node`` (read by
  pattern, never evaluated) and ``run_id: None``; a payload with neither key, a non-object
  payload and unparseable text give ``None`` for both; a JSON payload cut at the 2000-char cap
  still yields both from its prefix;
* the stored rationale is returned exactly as written.
"""

from __future__ import annotations

import importlib
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from noc_agents.db.models import AgentRunRow, AuditRow, get_session, new_id, utcnow
from noc_agents.realtime.hub import hub

LIFECYCLE = {
    "INGEST", "CORRELATE", "ENRICH", "SEVERITY", "TICKET", "ASSIGN",
    "HITL", "BROADCAST", "EXEC_BRIEF", "LEDGER", "RECURRENCE", "MONITOR",
}

BTS_EVENT = {  # P4 at L2_GUARDED: the whole twelve-step run, no gate
    "site_id": "SFC-MTK-BTS-MCH04",
    "site_name": "Machakos Town BTS",
    "site_type": "BTS",
    "region_code": "MTK",
    "alarm_code": "SITE_DOWN",
    "failure_domain": "POWER",
    "users_affected": 3200,
}


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'audit.db').as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
    monkeypatch.setenv("LIVE_AGENT_DELAY_MS", "0")
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path / "ledgers"))
    import noc_agents.config as cfg
    import noc_agents.db.models as models
    import noc_agents.main as main

    cfg.clear_settings_cache()
    models._engine = None
    models.SessionLocal = None
    importlib.reload(main)
    hub._history.clear()
    with TestClient(main.app) as client:
        yield client
    hub._history.clear()
    cfg.clear_settings_cache()


def _add_rows(*rows: AuditRow) -> list[str]:
    """Stores the rows and returns their ids (read before the commit expires them)."""
    ids = [row.id for row in rows]
    session = get_session()
    try:
        for row in rows:
            session.add(row)
        session.commit()
    finally:
        session.close()
    return ids


def _audit_row(ts_offset_s: int, payload_json: str, *, action: str = "step.succeeded", rationale: str = "") -> AuditRow:
    return AuditRow(
        id=new_id(),
        ts=utcnow() + timedelta(seconds=ts_offset_s),
        operator_id="safaricom",
        actor="EnrichmentAgent",
        action=action,
        entity_type="incident",
        entity_id="",
        rationale=rationale,
        payload_json=payload_json,
    )


def test_a_lifecycle_run_lists_its_run_id_and_node_on_every_step(api):
    r = api.post("/api/v1/events", json=BTS_EVENT)
    assert r.status_code == 200, r.text
    incident_id = r.json()["incident"]["id"]

    listed = api.get("/api/v1/audit", params={"limit": 500}).json()
    assert listed, "the run wrote no audit rows"
    for row in listed:
        assert "run_id" in row and "node" in row, row

    steps = [row for row in listed if row["action"].startswith("step.")]
    assert {row["node"] for row in steps} >= LIFECYCLE, sorted(row["node"] for row in steps)
    run_ids = {row["run_id"] for row in steps}
    assert len(run_ids) == 1 and None not in run_ids, run_ids

    # The point of the field: intake steps (no ticket yet) and the ticket's steps are one run.
    intake = [row for row in steps if row["entity_id"] == ""]
    ticketed = [row for row in steps if row["entity_id"] == incident_id]
    assert {row["node"] for row in intake} >= {"INGEST", "CORRELATE", "ENRICH", "SEVERITY"}
    assert intake and ticketed and {row["run_id"] for row in intake} == {row["run_id"] for row in ticketed}

    session = get_session()
    try:
        run = session.scalars(select(AgentRunRow).where(AgentRunRow.incident_id == incident_id)).first()
        stored = list(session.scalars(select(AuditRow).where(AuditRow.action.like("step.%"))))
    finally:
        session.close()
    assert run is not None and run_ids == {run.id}
    for row in stored:
        payload = json.loads(row.payload_json)  # JSON now, not a Python repr
        assert set(payload) == {"node", "run_id", "output"}, payload
        assert payload["run_id"] == run.id and payload["node"] in LIFECYCLE
        assert isinstance(payload["output"], str)


def test_older_and_foreign_payloads_still_list(api):
    legacy = _audit_row(1, str({"node": "ENRICH", "output": "users_est=80000, region=NBI_E"}))
    no_keys = _audit_row(2, json.dumps({"task_id": "t-1", "priority": "P2"}), action="hitl.escalation.nudged")
    not_object = _audit_row(3, json.dumps(["ENRICH"]), action="scorecard.published")
    garbage = _audit_row(4, "not json at all", action="outbox.retried")
    cut = _audit_row(5, json.dumps({"node": "TICKET", "run_id": "run-cut-1", "output": "x" * 3000})[:2000])
    linked = _audit_row(6, json.dumps({"task_id": "t-2", "incident_id": "inc-77"}), action="hitl.escalated")
    legacy_id, no_keys_id, not_object_id, garbage_id, cut_id, linked_id = _add_rows(
        legacy, no_keys, not_object, garbage, cut, linked
    )

    listed = {row["id"]: row for row in api.get("/api/v1/audit", params={"limit": 50}).json()}

    assert (listed[legacy_id]["node"], listed[legacy_id]["run_id"]) == ("ENRICH", None)
    for row_id in (no_keys_id, not_object_id, garbage_id):
        assert (listed[row_id]["node"], listed[row_id]["run_id"]) == (None, None), listed[row_id]
    assert (listed[cut_id]["node"], listed[cut_id]["run_id"]) == ("TICKET", "run-cut-1")
    # An approval-ladder row names its ticket in the payload; the page links it through this.
    assert listed[linked_id]["incident_id"] == "inc-77"
    assert listed[no_keys_id]["incident_id"] is None


def test_limit_is_bounded(api):
    assert api.get("/api/v1/audit", params={"limit": 0}).status_code == 422
    assert api.get("/api/v1/audit", params={"limit": 5000}).status_code == 422
    assert api.get("/api/v1/audit", params={"limit": 1000}).status_code == 200


def test_the_rationale_is_returned_as_stored(api):
    rationale = "CMDB/mock enrich: Nairobi East; FE on-call=FE-NBI-E-01; pool=['EGYPRO_FIBRE', 'FIELD_ENGINEER']"
    row = _audit_row(1, json.dumps({"node": "ENRICH", "run_id": "run-1", "output": ""}), rationale=rationale)
    (row_id,) = _add_rows(row)

    listed = {r["id"]: r for r in api.get("/api/v1/audit", params={"limit": 50}).json()}
    assert listed[row_id]["rationale"] == rationale
    assert (listed[row_id]["node"], listed[row_id]["run_id"]) == ("ENRICH", "run-1")
