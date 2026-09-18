"""Operator isolation (spec §8, Phase 1): safaricom data must never reach an airtel query.

How ``operator_id`` is threaded through today, read before anything was asserted here:

* **Config** — ``get_settings(profile)`` loads ``config/operators/<profile>.yaml``; the whole
  lifecycle takes ``cfg.operator_id`` from there. One process serves one profile
  (``OPERATOR_PROFILE``), but every profile shares one ``DATABASE_URL``, so the rows of both
  operators live in one file and isolation is a *query* property, not a deployment property.
* **Schema** — ``incidents``, ``problems``, ``agent_runs``, ``audit_events``, ``shift_ledger``,
  ``outbox`` and ``llm_calls`` all carry ``operator_id``. ``work_notes``, ``broadcasts``,
  ``incident_briefs``, ``agent_run_steps`` and ``hitl_tasks`` do NOT; they are reached through
  their parent (``incident_id`` / ``run_id``) — except ``hitl_tasks``, which the API reads
  directly (see the failing section below).
* **Domain layer** — every ``select()`` in ``agents/`` and ``services/`` that touches an
  operator-scoped table filters on ``cfg.operator_id``: correlate, recurrence, handover and
  worklog_monitor all do. This layer is clean.
* **API layer** — ``main.py`` filters on ``_settings().operator.operator_id`` for
  ``/incidents``, ``/problems``, ``/shifts/ledger`` and part of ``/metrics/summary``, and
  does NOT filter for ``/runs``, ``/audit``, ``/hitl/pending``, two of the metric counters,
  or any of the by-id routes.

The first section pins the properties that HOLD. The second section is a set of tests that
FAIL on purpose: each one documents a query through which one operator's rows reach the
other's screen. They are written against the *property* ("no airtel row in a safaricom
answer"), never against a particular fix, because how to close each hole — filter, 404, 403,
or add ``hitl_tasks.operator_id`` — is the owner's call, not this file's.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from noc_agents.config import get_settings
from noc_agents.db.models import (
    AgentRunRow,
    AuditRow,
    HitlTaskRow,
    IncidentRow,
    OutboxRow,
    ProblemRow,
    ShiftLedgerRow,
    get_session,
)
from noc_agents.domain.schemas import EventIngest
from noc_agents.graph.pipeline import process_event
from noc_agents.orchestrator.outbox import enqueue
from noc_agents.realtime.hub import hub

SAF_HUB = {  # P2 for safaricom: opens an APPROVE_BROADCAST gate
    "site_id": "SFC-NBIE-HUB-EMB",
    "site_name": "Embakasi East Aggregation HUB",
    "site_type": "HUB",
    "region_code": "NBI_E",
    "alarm_code": "POWER_GRID_FAIL",
    "failure_domain": "POWER",
    "users_affected": 450000,
}
SAF_BTS = {
    "site_id": "SFC-MTK-BTS-MCH04",
    "site_name": "Machakos Town BTS",
    "site_type": "BTS",
    "region_code": "MTK",
    "alarm_code": "SITE_DOWN",
    "failure_domain": "POWER",
    "users_affected": 3200,
}
ATL_HUB = {  # P2 for airtel: same shape, airtel's own region vocabulary
    "site_id": "ATL-NBI-HUB-001",
    "site_name": "Airtel Nairobi Aggregation HUB",
    "site_type": "HUB",
    "region_code": "NBI",
    "alarm_code": "POWER_GRID_FAIL",
    "failure_domain": "POWER",
    "users_affected": 450000,
}
ATL_BTS = {
    "site_id": "ATL-CKA-BTS-014",
    "site_name": "Nyeri Town BTS",
    "site_type": "BTS",
    "region_code": "CKA",
    "alarm_code": "SITE_DOWN",
    "failure_domain": "POWER",
    "users_affected": 3200,
}


class Tenants:
    """The two operators' rows in one database, plus a client bound to the safaricom profile."""

    def __init__(self, client: TestClient, ledger_dir) -> None:
        self.client = client
        self.ledger_dir = ledger_dir
        self.saf: list[dict] = []
        self.atl: list[dict] = []

    @property
    def saf_numbers(self) -> set[str]:
        return {i["incident_number"] for i in self.saf}

    @property
    def atl_numbers(self) -> set[str]:
        return {i["incident_number"] for i in self.atl}

    def ingest_safaricom(self, event: dict) -> dict:
        """Through the API, which is what the running process does."""
        r = self.client.post("/api/v1/events", json=event)
        assert r.status_code == 200, r.text
        inc = r.json()["incident"]
        assert inc["operator_id"] == "safaricom"
        self.saf.append(inc)
        return inc

    def ingest_airtel(self, event: dict) -> dict:
        """Straight into the same file under the airtel profile — the second operator's
        process, writing to the shared database while the safaricom API is up."""
        session = get_session()
        try:
            inc = process_event(session, get_settings("airtel"), EventIngest(**event))
            assert inc.operator_id == "airtel"
            out = {
                "id": inc.id,
                "incident_number": inc.incident_number,
                "operator_id": inc.operator_id,
                "priority": inc.priority,
                "site_id": inc.site_id,
            }
        finally:
            session.close()
        self.atl.append(out)
        return out


@pytest.fixture()
def tenants(tmp_path, monkeypatch):
    db = tmp_path / "isolation.db"
    ledgers = tmp_path / "ledgers"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
    monkeypatch.setenv("LEDGER_DIR", str(ledgers))
    monkeypatch.setenv("LIVE_AGENT_DELAY_MS", "0")

    import noc_agents.config as cfg
    import noc_agents.db.models as models
    import noc_agents.main as main

    cfg.clear_settings_cache()
    models._engine = None
    models.SessionLocal = None
    importlib.reload(main)
    hub._history.clear()
    with TestClient(main.app) as c:
        t = Tenants(c, ledgers)
        t.ingest_safaricom(SAF_HUB)
        t.ingest_safaricom(SAF_BTS)
        t.ingest_airtel(ATL_HUB)
        t.ingest_airtel(ATL_BTS)
        yield t
    hub._history.clear()
    cfg.clear_settings_cache()


def _read(fn):
    session = get_session()
    try:
        return fn(session)
    finally:
        session.close()


def _leak(rows: list[str], foreign: set[str]) -> list[str]:
    return sorted(set(rows) & foreign)


# =================================================================================
# Section 1 — the isolation that HOLDS
# =================================================================================


def test_the_two_operators_really_are_both_in_one_database(tenants):
    """The precondition: this is one file with both operators' rows in it, which is exactly
    the situation the isolation rules exist for."""
    by_op = _read(
        lambda s: {
            op: sorted(
                n
                for (n,) in s.execute(select(IncidentRow.incident_number).where(IncidentRow.operator_id == op)).all()
            )
            for op in ("safaricom", "airtel")
        }
    )
    assert by_op["safaricom"] == sorted(tenants.saf_numbers)
    assert by_op["airtel"] == sorted(tenants.atl_numbers)
    assert by_op["safaricom"] and by_op["airtel"]
    # The numbering styles differ, so the two operators' ticket numbers cannot be confused.
    assert all(n.startswith("INC") for n in by_op["safaricom"])
    assert all(n.startswith("ATL-") for n in by_op["airtel"])


def test_incident_list_is_operator_scoped(tenants):
    rows = tenants.client.get("/api/v1/incidents").json()
    assert {r["operator_id"] for r in rows} == {"safaricom"}
    assert _leak([r["incident_number"] for r in rows], tenants.atl_numbers) == []
    # And the filtered variants cannot be used to reach across either.
    for query in ("?priority=P2", "?status=AWAITING_VENDOR", "?q=ATL", "?q=HUB", "?region=NBI"):
        rows = tenants.client.get(f"/api/v1/incidents{query}").json()
        assert _leak([r["incident_number"] for r in rows], tenants.atl_numbers) == [], query


def test_correlate_never_merges_one_operators_alarm_into_the_others(tenants):
    """The same site, alarm and domain for both operators must stay two tickets: CORRELATE
    filters on ``cfg.operator_id`` before the fingerprint."""
    shared = {
        "site_id": "SHARED-COLO-BTS-01",
        "site_name": "Shared colocation BTS",
        "site_type": "BTS",
        "region_code": "MTK",
        "alarm_code": "SITE_DOWN",
        "failure_domain": "POWER",
        "users_affected": 3200,
    }
    saf = tenants.ingest_safaricom(shared)
    atl = tenants.ingest_airtel({**shared, "region_code": "CKA"})
    assert saf["incident_number"] != atl["incident_number"]

    rows = _read(
        lambda s: sorted(
            (r.operator_id, r.incident_number)
            for r in s.scalars(select(IncidentRow).where(IncidentRow.site_id == "SHARED-COLO-BTS-01"))
        )
    )
    assert len(rows) == 2, rows
    assert {op for op, _n in rows} == {"safaricom", "airtel"}

    # A second identical safaricom alarm merges into the SAFARICOM ticket, not the airtel one.
    again = tenants.ingest_safaricom(shared)
    assert again["incident_number"] == saf["incident_number"]


def test_problem_records_are_operator_scoped(tenants):
    """Three failures at one site open a ProblemRow. Each operator opens its own, and
    ``GET /api/v1/problems`` shows only the active profile's."""
    def repeats(ingest, site: str, region: str) -> None:
        for alarm in ("SITE_DOWN", "MAINS_FAIL", "RECTIFIER_FAULT"):
            ingest(
                {
                    "site_id": site,
                    "site_name": f"{site} site",
                    "site_type": "BTS",
                    "region_code": region,
                    "alarm_code": alarm,
                    "failure_domain": "POWER",
                    "users_affected": 4000,
                }
            )

    repeats(tenants.ingest_safaricom, "SFC-MTK-BTS-REP01", "MTK")
    repeats(tenants.ingest_airtel, "ATL-CKA-BTS-REP01", "CKA")

    stored = _read(lambda s: sorted((p.operator_id, p.site_id) for p in s.scalars(select(ProblemRow))))
    assert ("safaricom", "SFC-MTK-BTS-REP01") in stored, stored
    assert ("airtel", "ATL-CKA-BTS-REP01") in stored, stored

    listed = tenants.client.get("/api/v1/problems").json()
    assert listed, "the safaricom problem record did not surface at all"
    assert {p["site_id"] for p in listed} == {"SFC-MTK-BTS-REP01"}


def test_shift_ledger_rows_and_workbooks_are_operator_scoped(tenants):
    """Both the DB ledger query and the .xlsx the dispatcher appends stay on their own side."""
    listed = tenants.client.get("/api/v1/shifts/ledger").json()
    assert _leak([r["incident_number"] for r in listed], tenants.atl_numbers) == []
    assert set(r["incident_number"] for r in listed) == tenants.saf_numbers

    stored = _read(lambda s: {(r.operator_id, r.incident_number) for r in s.scalars(select(ShiftLedgerRow))})
    assert {n for op, n in stored if op == "airtel"} == tenants.atl_numbers
    assert {n for op, n in stored if op == "safaricom"} == tenants.saf_numbers

    # services/ledger.append_excel_row puts every workbook under <LEDGER_DIR>/<operator_id>.
    folders = sorted(p.name for p in tenants.ledger_dir.iterdir() if p.is_dir())
    assert folders == ["airtel", "safaricom"], folders
    for folder in folders:
        assert list((tenants.ledger_dir / folder).glob("ledger_*.xlsx")), f"no workbook for {folder}"


def test_outbox_rows_are_attributed_and_never_unowned(tenants):
    """Every queued side effect carries the operator that produced it, and ``enqueue``
    refuses to write a row it cannot attribute."""
    rows = _read(lambda s: [(r.operator_id, r.kind, r.incident_id) for r in s.scalars(select(OutboxRow))])
    assert rows, "no side effects were queued at all"
    assert {op for op, _k, _i in rows} == {"safaricom", "airtel"}

    incident_owner = _read(lambda s: {i.id: i.operator_id for i in s.scalars(select(IncidentRow))})
    mismatched = [(op, kind, inc) for op, kind, inc in rows if inc and incident_owner.get(inc) != op]
    assert mismatched == [], f"outbox rows attributed to the wrong operator: {mismatched}"

    # The EXCEL_ROW payload is what chooses the ledger folder, so it must agree too.
    excel = _read(
        lambda s: [
            (r.operator_id, __import__("json").loads(r.payload_json)["operator_id"])
            for r in s.scalars(select(OutboxRow).where(OutboxRow.kind == "EXCEL_ROW"))
        ]
    )
    assert excel and all(row_op == payload_op for row_op, payload_op in excel), excel

    session = get_session()
    try:
        with pytest.raises(ValueError, match="operator_id is required"):
            enqueue(session, kind="EMAIL", idempotency_key="EMAIL:unowned", payload={"subject": "x"})
    finally:
        session.rollback()
        session.close()


def test_handover_and_open_incident_metrics_are_operator_scoped(tenants):
    """The two supervisor-facing summaries that ARE scoped today."""
    handover = tenants.client.post("/api/v1/shifts/handover").json()
    blob = str(handover)
    assert "SAFARICOM" in handover["subject"]
    assert _leak([n for n in tenants.atl_numbers if n in blob], tenants.atl_numbers) == [], handover["subject"]

    metrics = tenants.client.get("/api/v1/metrics/summary").json()
    assert metrics["operator_id"] == "safaricom"
    open_saf = _read(
        lambda s: len(
            list(
                s.scalars(
                    select(IncidentRow).where(
                        IncidentRow.operator_id == "safaricom",
                        IncidentRow.status.not_in(["CLOSED", "CANCELLED"]),
                    )
                )
            )
        )
    )
    assert metrics["open_total"] == open_saf
    assert sum(metrics["by_priority"].values()) == open_saf
    assert set(metrics["by_region"]) <= {"NBI_E", "MTK", "CST", "RFT", "WNY", "NBI_W"}
    assert "NBI" not in metrics["by_region"] and "CKA" not in metrics["by_region"], metrics["by_region"]


# =================================================================================
# Section 2 — the isolation that DOES NOT hold. These tests fail on purpose.
#
# Every assertion below states the property ("no airtel row in a safaricom answer"),
# not a fix. Closing each hole is a product decision:
#   * /runs and /audit can simply filter on operator_id — both columns already exist;
#   * /hitl/pending has no operator_id on hitl_tasks at all, so it needs either a join to
#     incidents or a new column (schema change, hence a decision, not a guess);
#   * the by-id routes must choose between 404 (pretend it is not there) and 403.
# =================================================================================


def test_runs_listing_leaks_the_other_operators_runs(tenants):
    """DEFECT: ``GET /api/v1/runs`` (main.py ``list_runs``) never filters on operator_id,
    although ``AgentRunRow.operator_id`` exists and the serializer returns it. A safaricom
    analyst's Agent Activity panel lists airtel lifecycle runs, and
    ``GET /api/v1/runs/{id}`` will then open one."""
    listed = tenants.client.get("/api/v1/runs").json()
    foreign = [r for r in listed if r["operator_id"] != "safaricom"]
    assert foreign == [], (
        f"{len(foreign)} of {len(listed)} runs on the safaricom Agent Activity panel belong to "
        f"another operator: {[ (r['operator_id'], r['id']) for r in foreign ]}"
    )


def test_audit_trail_leaks_the_other_operators_entries(tenants):
    """DEFECT: ``GET /api/v1/audit`` (main.py ``list_audit``) selects every AuditRow ordered
    by ts, with no operator filter, although ``AuditRow.operator_id`` exists. The audit log
    is the one surface a regulator reads; it must not show another licensee's actions."""
    listed = tenants.client.get("/api/v1/audit", params={"limit": 500}).json()
    owners = _read(lambda s: {a.id: a.operator_id for a in s.scalars(select(AuditRow))})
    foreign = sorted({owners.get(a["id"], "?") for a in listed} - {"safaricom"})
    assert foreign == [], (
        f"the safaricom audit trail contains rows owned by {foreign} "
        f"({sum(1 for a in listed if owners.get(a['id']) != 'safaricom')} of {len(listed)} rows)"
    )


def test_hitl_queue_leaks_the_other_operators_approval_tasks(tenants):
    """DEFECT (most serious): ``GET /api/v1/hitl/pending`` selects every open HitlTaskRow.

    ``hitl_tasks`` carries no ``operator_id`` — the only route to the owner is
    ``incident_id -> incidents.operator_id``, and the endpoint already loads that incident to
    fill in the number, so the information is right there and unused. The consequence is not
    cosmetic: the task the safaricom supervisor sees is an APPROVE_BROADCAST gate, and
    approving it releases the OTHER operator's customer-facing message."""
    pending = tenants.client.get("/api/v1/hitl/pending").json()
    foreign = [t for t in pending if t["incident_number"] in tenants.atl_numbers]
    assert foreign == [], (
        "the safaricom HITL inbox is offering approval of another operator's broadcast: "
        f"{[(t['incident_number'], t['task_type']) for t in foreign]}"
    )


def test_a_supervisor_cannot_decide_the_other_operators_hitl_task(tenants):
    """DEFECT: the decision routes resolve the task by id with no operator check, so the
    approval above is not merely offered — it goes through, and the other operator's held
    broadcast is released by a supervisor who does not work there."""
    # SETUP NOTE: this originally found the airtel task by scanning GET /hitl/pending on the
    # SAFARICOM client. That only works while the queue leaks — i.e. it depended on the very
    # defect that test_hitl_queue_leaks_the_other_operators_approval_tasks asserts must be
    # closed, making the two tests mutually exclusive: no implementation could satisfy both.
    # The id now comes straight from the database, the way this module's other checks do.
    # The assertions below are unchanged: a foreign approve must be refused, and the task
    # must still be PENDING afterwards.
    atl_task_id = _read(
        lambda s: s.scalar(
            select(HitlTaskRow.id)
            .join(IncidentRow, IncidentRow.id == HitlTaskRow.incident_id)
            .where(IncidentRow.operator_id == "airtel", HitlTaskRow.status == "PENDING")
        )
    )
    assert atl_task_id is not None, "expected an airtel APPROVE_BROADCAST task to exist for this check"

    r = tenants.client.post(f"/api/v1/hitl/{atl_task_id}/approve", json={"resolved_by": "Safaricom Supervisor"})
    assert r.status_code in (403, 404), (
        f"a safaricom supervisor approved airtel task {atl_task_id} and got {r.status_code}: {r.text}"
    )
    status = _read(lambda s: s.get(HitlTaskRow, atl_task_id).status)
    assert status == "PENDING", f"the airtel task was moved to {status} by another operator's supervisor"


def test_metric_counters_are_counted_across_operators(tenants):
    """DEFECT: ``GET /api/v1/metrics/summary`` scopes ``open_total``, ``by_priority``,
    ``by_region`` and ``problems_open`` to the operator, but ``hitl_pending`` and
    ``agents_running`` are global counts. The response carries ``operator_id: safaricom``,
    so the wallboard states a number about safaricom that is not about safaricom."""
    metrics = tenants.client.get("/api/v1/metrics/summary").json()
    assert metrics["operator_id"] == "safaricom"

    saf_open_tasks, saf_running = _read(
        lambda s: (
            len(
                [
                    t
                    for t in s.scalars(select(HitlTaskRow).where(HitlTaskRow.status.in_(["PENDING", "CLAIMED"])))
                    if s.get(IncidentRow, t.incident_id).operator_id == "safaricom"
                ]
            ),
            len(
                list(
                    s.scalars(
                        select(AgentRunRow).where(
                            AgentRunRow.operator_id == "safaricom",
                            AgentRunRow.status.in_(["RUNNING", "WAITING_HITL"]),
                        )
                    )
                )
            ),
        )
    )
    wrong = {
        name: (reported, expected)
        for name, reported, expected in (
            ("hitl_pending", metrics["hitl_pending"], saf_open_tasks),
            ("agents_running", metrics["agents_running"], saf_running),
        )
        if reported != expected
    }
    assert wrong == {}, (
        "counters on a response labelled operator_id=safaricom are counted across every "
        f"operator in the database — {', '.join(f'{k} reported {r}, safaricom has {e}' for k, (r, e) in wrong.items())}"
    )


def test_the_other_operators_incident_is_not_readable_or_writable_by_id(tenants):
    """DEFECT: every by-id route (`GET /incidents/{id}`, `/timeline`, `/workflow`,
    `/briefs/{id}`, `POST /notes`, `/close`, `/reassign`) resolves the row with
    ``session.get(IncidentRow, id)`` and never compares its ``operator_id`` with the active
    profile. So the airtel ticket can be read in full — and closed — from the safaricom API.

    Whether the right answer is 404 or 403 is the owner's call; this asserts only that one
    of them must happen, never a 200.
    """
    atl = tenants.atl[0]
    failures: list[str] = []

    reads = {
        f"GET /incidents/{{id}}": tenants.client.get(f"/api/v1/incidents/{atl['id']}"),
        f"GET /incidents/{{id}}/timeline": tenants.client.get(f"/api/v1/incidents/{atl['id']}/timeline"),
        f"GET /briefs/{{id}}": tenants.client.get(f"/api/v1/briefs/{atl['id']}"),
    }
    for name, resp in reads.items():
        if resp.status_code not in (403, 404):
            failures.append(f"{name} -> {resp.status_code} (readable)")

    note = tenants.client.post(
        f"/api/v1/incidents/{atl['id']}/notes",
        json={"author": "Safaricom NOC", "author_role": "NOC", "body": "cross-operator note"},
    )
    if note.status_code not in (403, 404):
        failures.append(f"POST /incidents/{{id}}/notes -> {note.status_code} (a note was written)")

    close = tenants.client.post(
        f"/api/v1/incidents/{atl['id']}/close",
        json={"closed_by": "Safaricom NOC", "resolution_code": "CLOSED_NORMAL", "resolution_summary": "not mine"},
    )
    if close.status_code not in (403, 404):
        failures.append(f"POST /incidents/{{id}}/close -> {close.status_code} (the ticket was CLOSED)")

    final = _read(lambda s: s.get(IncidentRow, atl["id"]).status)
    assert failures == [], (
        f"airtel incident {atl['incident_number']} is reachable through the safaricom API: "
        + "; ".join(failures)
        + f" — it now reads {final}"
    )
