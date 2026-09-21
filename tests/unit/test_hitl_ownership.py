"""A HITL task owns itself: ``hitl_tasks.operator_id`` (schema_version 8).

Until v8 a task's owner was derived by joining ``incident_id -> incidents.operator_id`` — a
deliberate choice that was right while every gated thing was an incident (the long comment in
``api/deps.py`` keeps the argument). Phase 5 raised cards that are about no incident at all,
so the owner now lives on the task, ``incident_id`` may be NULL, and ``_owned(HitlTaskRow)``
filters on the task's own column.

That moves the risk. With the join, a task could not be written without an owner. With a
column, it can — by any writer that forgets it — and the result is the quietest failure this
system has: the row is stored, ``_owned`` hides it from every operator, and a pending approval
simply never appears in anyone's inbox. Fail-closed, and functionally broken. So the rule is
enforced where no writer can skip it, on the mapped class (``db.models._own_hitl_task``), and
this file pins it from three sides:

1. **the rule** — an incident's operator is filled in; neither incident nor operator is
   refused; a stated operator that contradicts the incident is refused;
2. **the writers that were NOT edited** — the golden-path gate (``agents/hitl.py``), the
   worklog monitor and the handover gate still pass only ``incident_id``, and their tasks come
   out owned, for BOTH operators;
3. **the scope** — each operator sees exactly its own tasks, with and without an incident, and
   over HTTP the other operator's card is a 404 (never a 403, which would confirm it exists) —
   on a database that contains **zero incidents**, which is the case the old join could not
   serve at all.
"""

from __future__ import annotations

import importlib
import sqlite3
from contextlib import contextmanager
from datetime import timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from noc_agents.api import auth
from noc_agents.api.deps import _get_owned, _owned
from noc_agents.config import clear_settings_cache, get_settings
from noc_agents.db.models import (
    HitlTaskOwnershipError,
    HitlTaskRow,
    IncidentBriefRow,
    IncidentRow,
    get_session,
    new_id,
    utcnow,
)
from noc_agents.domain.schemas import EventIngest
from noc_agents.graph.pipeline import process_event
from noc_agents.realtime.hub import hub
from noc_agents.services.worklog_monitor import chase_silent_incidents

SAF_HUB = {  # P2 for safaricom: opens the APPROVE_BROADCAST gate (agents/hitl.py)
    "site_id": "SFC-NBIE-HUB-EMB", "site_name": "Embakasi East Aggregation HUB", "site_type": "HUB",
    "region_code": "NBI_E", "alarm_code": "POWER_GRID_FAIL", "failure_domain": "POWER", "users_affected": 450000,
}
ATL_HUB = {  # the same for airtel, in airtel's own site and region vocabulary
    "site_id": "ATL-NBI-HUB-001", "site_name": "Airtel Nairobi Aggregation HUB", "site_type": "HUB",
    "region_code": "NBI", "alarm_code": "POWER_GRID_FAIL", "failure_domain": "POWER", "users_affected": 450000,
}
OPERATORS = ("safaricom", "airtel")


def _incident(session, operator: str, *, number: str) -> IncidentRow:
    inc = IncidentRow(
        operator_id=operator, incident_number=number, site_id=f"SITE-{number}", region_code="NBI",
        correlation_fingerprint=f"fp-{number}",
    )
    session.add(inc)
    session.flush()
    return inc


def _raw(session, sql: str, params: tuple = ()) -> list[tuple]:
    """Straight at the file with sqlite3 — no ORM, no listener, no identity map."""
    con = sqlite3.connect(session.get_bind().url.database)
    try:
        rows = con.execute(sql, params).fetchall()
        con.commit()
        return rows
    finally:
        con.close()


@contextmanager
def _acting_as(monkeypatch, operator: str):
    """Make ``operator`` the active profile for ``_owned`` — what a process started with
    ``OPERATOR_PROFILE=<operator>`` sees. Both profiles share one database file (spec §8)."""
    monkeypatch.setenv("OPERATOR_PROFILE", operator)
    clear_settings_cache()
    try:
        yield
    finally:
        clear_settings_cache()


# ============================================================================== 1. the rule


@pytest.mark.parametrize("operator", OPERATORS)
def test_a_task_raised_against_an_incident_gets_that_incidents_operator(tmp_db, operator):
    """Every writer that existed before v8 passes ``incident_id`` and nothing else."""
    _settings, session = tmp_db
    inc = _incident(session, operator, number=f"{operator}-1")
    task = HitlTaskRow(incident_id=inc.id, task_type="APPROVE_BROADCAST")
    assert task.operator_id is None  # nothing happens at construction: there is no session yet
    session.add(task)
    session.commit()
    assert task.operator_id == operator
    assert _raw(session, "SELECT operator_id FROM hitl_tasks WHERE id = ?", (task.id,)) == [(operator,)], (
        "in the row, not merely on the object"
    )


def test_the_incident_may_be_pending_in_the_very_same_flush(tmp_db):
    """``tests/unit/test_phase2_schema.py`` adds an incident and its tasks and commits once. The
    unit of work orders INSERTs by relationship(), and these two classes have none, so the task
    usually reaches the database BEFORE its incident: a lookup by SQL alone finds nothing."""
    _settings, session = tmp_db
    task = HitlTaskRow(incident_id="inc-same-flush", task_type="GENERIC")
    session.add(task)  # the task first, to make the point
    session.add(IncidentRow(
        id="inc-same-flush", operator_id="airtel", incident_number="ATL-000777", site_id="ATL-1",
        region_code="NBI", correlation_fingerprint="fp-same-flush",
    ))
    session.commit()
    assert task.operator_id == "airtel"


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_a_task_with_neither_an_incident_nor_an_operator_is_refused(tmp_db, blank):
    """The row this whole file is about. It would be stored, and no operator would ever see it."""
    _settings, session = tmp_db
    session.add(HitlTaskRow(id="t-unowned", task_type="APPROVE_MAINTENANCE_WINDOW", operator_id=blank))
    with pytest.raises(HitlTaskOwnershipError, match="neither incident_id nor operator_id"):
        session.commit()
    session.rollback()
    assert session.get(HitlTaskRow, "t-unowned") is None, "refused means not written"


def test_a_stated_operator_that_contradicts_the_incident_is_refused(tmp_db):
    """The old objection to this column was that a second copy of the owner can drift from the
    first. It is checked against the first at the only moment it is written."""
    _settings, session = tmp_db
    theirs = _incident(session, "airtel", number="ATL-000001")
    session.add(HitlTaskRow(id="t-drift", incident_id=theirs.id, operator_id="safaricom", task_type="GENERIC"))
    with pytest.raises(HitlTaskOwnershipError, match="says operator 'safaricom'.*belongs to 'airtel'"):
        session.commit()
    session.rollback()
    assert session.get(HitlTaskRow, "t-drift") is None

    # Stating it correctly is fine, and changes nothing.
    theirs = _incident(session, "airtel", number="ATL-000002")
    ok = HitlTaskRow(incident_id=theirs.id, operator_id="airtel", task_type="GENERIC")
    session.add(ok)
    session.commit()
    assert ok.operator_id == "airtel"


def test_an_incident_that_does_not_exist_cannot_lend_an_owner(tmp_db):
    _settings, session = tmp_db
    session.add(HitlTaskRow(id="t-dangling", incident_id="no-such-incident", task_type="GENERIC"))
    with pytest.raises(HitlTaskOwnershipError, match="does not exist.*owner cannot be derived"):
        session.commit()
    session.rollback()
    assert session.get(HitlTaskRow, "t-dangling") is None

    # With the owner stated outright the row IS ownable (a dangling incident_id beside a stated
    # owner is no worse than it was before v8, foreign keys being unenforced), and it is stored
    # as stated, trimmed -- an owner with stray whitespace would match no operator's clause.
    kept = HitlTaskRow(id="t-stated", incident_id="no-such-incident", operator_id=" airtel ", task_type="GENERIC")
    session.add(kept)
    session.commit()
    assert kept.operator_id == "airtel"


def test_a_task_that_is_not_about_an_incident_is_owned_directly(tmp_db):
    """What v8 is FOR: a card about a maintenance window, a scorecard line, a vendor notice."""
    _settings, session = tmp_db
    assert session.scalar(select(IncidentRow.id)) is None, "this database has no incident at all"
    card = HitlTaskRow(
        operator_id="safaricom", task_type="APPROVE_MAINTENANCE_WINDOW",
        entity_type="maintenance_window", entity_id="win-1",
    )
    session.add(card)
    session.commit()
    assert (card.incident_id, card.operator_id) == (None, "safaricom")


# ================================================= 2. the writers that were deliberately NOT edited


def test_the_unedited_writers_still_produce_owned_tasks_for_both_operators(tmp_db, monkeypatch):
    """``agents/hitl.py`` is on the golden path and was not touched; ``worklog_monitor`` was not
    touched either. Both construct ``HitlTaskRow(incident_id=inc.id, ...)`` exactly as they did
    at v7. Run the real code under each profile, into one file, and read the owner back."""
    _settings, session = tmp_db
    raised: dict[str, set[str]] = {}
    for operator, event in (("safaricom", SAF_HUB), ("airtel", ATL_HUB)):
        settings = get_settings(operator)
        inc = process_event(session, settings, EventIngest(**event))  # agents/hitl.py -> APPROVE_BROADCAST
        assert inc.operator_id == operator and inc.requires_hitl
        inc.sla_restore_due = utcnow() - timedelta(hours=1)
        session.commit()
        results = chase_silent_incidents(session, settings.operator)  # worklog_monitor -> GENERIC
        assert any(r.task_created for r in results)
        session.commit()
        raised[operator] = {
            t.task_type for t in session.scalars(select(HitlTaskRow).where(HitlTaskRow.incident_id == inc.id))
        }
        owners = set(session.scalars(select(HitlTaskRow.operator_id).where(HitlTaskRow.incident_id == inc.id)))
        assert owners == {operator}
    assert raised == {op: {"APPROVE_BROADCAST", "GENERIC"} for op in OPERATORS}

    # ...and so each inbox query sees exactly its own two, which is the point of the column.
    for operator in OPERATORS:
        with _acting_as(monkeypatch, operator):
            seen = session.scalars(_owned(HitlTaskRow)).all()
            assert {t.operator_id for t in seen} == {operator} and len(seen) == 2


# ============================================================================= 3. the scope


def test_owned_filters_on_the_tasks_own_column_and_no_longer_joins_incidents(tmp_db):
    """The join is what made a task without an incident unreachable: an INNER JOIN on a NULL
    ``incident_id`` matches nothing. ``incident_briefs`` keeps the join, and should — a brief
    always has an incident, so for it the old argument still holds."""
    sql = str(_owned(HitlTaskRow).compile(compile_kwargs={"literal_binds": True}))
    assert "JOIN" not in sql and "hitl_tasks.operator_id = 'safaricom'" in sql
    brief_sql = str(_owned(IncidentBriefRow).compile(compile_kwargs={"literal_binds": True}))
    assert "JOIN incidents" in brief_sql and "incidents.operator_id = 'safaricom'" in brief_sql


def test_each_operator_sees_exactly_its_own_tasks_with_and_without_an_incident(tmp_db, monkeypatch):
    _settings, session = tmp_db
    ids: dict[str, set[str]] = {}
    for operator in OPERATORS:
        inc = _incident(session, operator, number=f"{operator}-9")
        with_incident = HitlTaskRow(id=new_id(), incident_id=inc.id, task_type="APPROVE_BROADCAST")
        without = HitlTaskRow(id=new_id(), operator_id=operator, task_type="APPROVE_SCHEDULE",
                              entity_type="maintenance_task", entity_id=f"mt-{operator}")
        session.add_all([with_incident, without])
        ids[operator] = {with_incident.id, without.id}
    session.commit()

    for operator, other in (OPERATORS, OPERATORS[::-1]):
        with _acting_as(monkeypatch, operator):
            assert {t.id for t in session.scalars(_owned(HitlTaskRow))} == ids[operator]
            for mine in ids[operator]:
                assert _get_owned(session, HitlTaskRow, mine, what="task").operator_id == operator
            for theirs in ids[other]:
                # 404, not 403: a 403 would confirm the id exists in the other operator's data.
                with pytest.raises(HTTPException) as refused:
                    _get_owned(session, HitlTaskRow, theirs, what="task")
                assert refused.value.status_code == 404


def test_a_row_with_no_operator_is_invisible_to_every_operator(tmp_db, monkeypatch):
    """How such a row can exist despite the listener: a pre-v8 release running against a v8 file
    (a rollback) INSERTs without the column, and the migration keeps an orphan unowned. NULL
    must fail CLOSED — ``operator_id = :op`` is never true for NULL — for both operators."""
    _settings, session = tmp_db
    inc = _incident(session, "safaricom", number="INC-V7-WRITER")
    session.commit()
    _raw(
        session,
        "INSERT INTO hitl_tasks (id, incident_id, task_type, proposed_payload_json, status, created_at) "
        "VALUES ('t-from-v7', ?, 'APPROVE_BROADCAST', '{}', 'PENDING', '2026-09-21 08:00:00.000000')",
        (inc.id,),
    )

    assert session.get(HitlTaskRow, "t-from-v7").operator_id is None
    for operator in OPERATORS:
        with _acting_as(monkeypatch, operator):
            assert "t-from-v7" not in {t.id for t in session.scalars(_owned(HitlTaskRow))}
            with pytest.raises(HTTPException) as refused:
                _get_owned(session, HitlTaskRow, "t-from-v7", what="task")
            assert refused.value.status_code == 404

    # The documented way forward after a rollback (db/migrate.py docstring) makes it visible
    # again — to its incident's operator only.
    _raw(
        session,
        "UPDATE hitl_tasks SET operator_id = (SELECT operator_id FROM incidents WHERE incidents.id = hitl_tasks.incident_id) "
        "WHERE operator_id IS NULL AND incident_id IS NOT NULL",
    )
    session.expire_all()
    with _acting_as(monkeypatch, "safaricom"):
        assert "t-from-v7" in {t.id for t in session.scalars(_owned(HitlTaskRow))}
    with _acting_as(monkeypatch, "airtel"):
        assert "t-from-v7" not in {t.id for t in session.scalars(_owned(HitlTaskRow))}


# ============================================== over HTTP, two operators, a file with NO incidents


@contextmanager
def _api(monkeypatch, db, operator: str):
    """The API as a process started with ``OPERATOR_PROFILE=<operator>`` serves it, on ``db``.
    Two of these in a row on one file is the two-tenant deployment of spec §8."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", operator)
    monkeypatch.setenv("MAINTENANCE_ENABLED", "true")

    import noc_agents.config as cfg
    import noc_agents.db.models as models
    import noc_agents.main as main

    cfg.clear_settings_cache()
    models._engine = None
    models.SessionLocal = None
    importlib.reload(main)
    auth.reset_sessions()
    hub._history.clear()
    try:
        with TestClient(main.app) as client:
            yield client
    finally:
        # The same teardown the other route tests use (tests/unit/test_maintenance.py).
        hub._history.clear()
        auth.reset_sessions()
        if models._engine is not None:
            models._engine.dispose()
        models._engine = None
        models.SessionLocal = None
        cfg.clear_settings_cache()
        importlib.reload(main)


def _raise_window_card(client, site: str) -> tuple[str, str]:
    r = client.post(
        "/api/v1/maintenance/windows",
        json={"scope": "SITE", "scope_ref": site, "starts_at": "2026-11-11T21:00:00Z", "ends_at": "2026-11-12T02:00:00Z"},
    )
    assert r.status_code == 200, r.text
    window_id = r.json()["id"]
    r = client.post(f"/api/v1/maintenance/windows/{window_id}/request-approval", json={})
    # Until v8 this was a 503 on an empty database: "no incident to anchor ... on".
    assert r.status_code == 200, r.text
    return window_id, r.json()["hitl_task_id"]


def _incident_count(db) -> int:
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
    finally:
        con.close()


def test_a_maintenance_card_on_a_database_with_no_incidents_belongs_to_its_operator_alone(tmp_path, monkeypatch):
    db = tmp_path / "two_operators_no_incidents.db"

    with _api(monkeypatch, db, "safaricom") as saf:
        saf_window, saf_card = _raise_window_card(saf, "SFC-MTK-HUB-THK")
        assert _incident_count(db) == 0

    with _api(monkeypatch, db, "airtel") as atl:
        atl_window, atl_card = _raise_window_card(atl, "ATL-NBI-HUB-001")
        assert _incident_count(db) == 0

        # airtel's inbox: its own card, and not a trace of safaricom's.
        pending = {t["id"]: t for t in atl.get("/api/v1/hitl/pending").json()}
        assert set(pending) == {atl_card}
        assert pending[atl_card]["incident_id"] is None and pending[atl_card]["incident_number"] is None
        # Every decision route answers 404 for the other operator's card — not 403, and not 200.
        for verb, body in (("claim", {"resolved_by": "Their Analyst"}),
                           ("approve", {"resolved_by": "Their Manager"}),
                           ("reject", {"resolved_by": "Their Manager", "reason": "not ours"})):
            r = atl.post(f"/api/v1/hitl/{saf_card}/{verb}", json=body)
            assert r.status_code == 404, f"airtel {verb} on a safaricom card -> {r.status_code}: {r.text}"
        # ...and airtel cannot see or schedule safaricom's window either.
        assert atl.post(f"/api/v1/maintenance/windows/{saf_window}/schedule", json={}).status_code == 404

    with _api(monkeypatch, db, "safaricom") as saf:
        pending = {t["id"]: t for t in saf.get("/api/v1/hitl/pending").json()}
        assert set(pending) == {saf_card}
        assert pending[saf_card]["task_type"] == "APPROVE_MAINTENANCE_WINDOW"
        for verb, body in (("claim", {"resolved_by": "Peter Kamau"}),
                           ("approve", {"resolved_by": "Grace Wanjiru"}),
                           ("reject", {"resolved_by": "Grace Wanjiru", "reason": "not ours"})):
            r = saf.post(f"/api/v1/hitl/{atl_card}/{verb}", json=body)
            assert r.status_code == 404, f"safaricom {verb} on an airtel card -> {r.status_code}: {r.text}"

        # Its own card it can decide, on the ordinary HITL surface, and the window goes ahead.
        assert saf.post(f"/api/v1/hitl/{saf_card}/approve", json={"resolved_by": "Grace Wanjiru"}).status_code == 200
        r = saf.post(f"/api/v1/maintenance/windows/{saf_window}/schedule", json={})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "SCHEDULED" and r.json()["approved_by"] == "Grace Wanjiru"

    # None of the six refused decisions touched the other operator's card, and at no point did
    # either lane have to invent an incident to hang its approval on.
    con = sqlite3.connect(db)
    try:
        rows = dict((r[0], r[1:]) for r in con.execute("SELECT id, operator_id, incident_id, status FROM hitl_tasks"))
    finally:
        con.close()
    assert rows == {saf_card: ("safaricom", None, "APPROVED"), atl_card: ("airtel", None, "PENDING")}
    assert _incident_count(db) == 0


def test_the_handover_gate_was_left_anchored_and_its_task_is_owned_all_the_same(tmp_path, monkeypatch):
    """``services/handover.py`` still anchors its task to the top open incident — deliberately
    unchanged (its own tests pin the fail-closed answer with nothing open, and ``main.py`` only
    releases the held mail through the task's incident). It passes ``incident_id`` and no
    operator, like every pre-v8 writer, so the listener is what owns it."""
    db = tmp_path / "handover.db"
    with _api(monkeypatch, db, "safaricom") as saf:
        inc = saf.post("/api/v1/events", json=SAF_HUB).json()["incident"]
        ho = saf.post("/api/v1/shifts/handover").json()
        task_id = ho["hitl"]["task_id"]
        assert task_id, ho
        assert task_id in {t["id"] for t in saf.get("/api/v1/hitl/pending").json()}
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT operator_id, incident_id, task_type FROM hitl_tasks WHERE id = ?", (task_id,)).fetchone() == (
            "safaricom", inc["id"], "APPROVE_HANDOVER",
        )
    finally:
        con.close()
