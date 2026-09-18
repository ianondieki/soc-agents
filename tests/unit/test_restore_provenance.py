"""Spec §7.0.8 — restore provenance, and defect #34 (recurrence signature vs count).

Two defects, one wave, so one file.

**Restore provenance.** ``restored_at`` is the input to every SLA number the NOC
reports, and until now nothing recorded how it was set. A supervisor who stood
on the site and a regex that matched the word "RESTORED" in a vendor's SMS wrote
the identical row. These tests pin all three producers that exist
(``MARK_RESTORED``, ``VENDOR_NOTE_INFERRED``, ``SUPERVISOR``), pin that the
fourth (``ALARM_CLEAR``) is declared but has no caller, and pin that provenance
is never written without a timestamp or a timestamp without provenance.

**Defect #34.** The occurrence count was counted over ``site + failure_domain``
while the signature also carried ``alarm_code``. A chronic site whose one real
fault arrives under two alarm codes therefore opened two problem records that
each claimed the combined count, and a known-error record had no single PRB to
attach to. The signature is now ``site|domain`` — the same two columns the count
is counted over.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from noc_agents.agents.recurrence import problem_signature
from noc_agents.api import auth
from noc_agents.api.serializers import incident_out
from noc_agents.db.models import IncidentRow, ProblemRow
from noc_agents.domain.schemas import EventIngest
from noc_agents.graph.pipeline import process_event
from noc_agents.realtime.hub import hub
from noc_agents.services import lifecycle
from noc_agents.services.lifecycle import (
    RESTORE_SOURCE_ALARM_CLEAR,
    RESTORE_SOURCE_MARK,
    RESTORE_SOURCE_NOTE,
    RESTORE_SOURCE_SUPERVISOR,
    RESTORE_SOURCES,
    apply_work_note_side_effects,
    record_restore,
    restore_incident,
)


def _incident(session, status: str = "IN_PROGRESS") -> IncidentRow:
    inc = IncidentRow(
        operator_id="safaricom",
        incident_number=f"INC-{status}",
        status=status,
        site_id="SFC-MTK-HUB-THK",
        region_code="MTK",
        correlation_fingerprint="fp",
    )
    session.add(inc)
    session.flush()
    return inc


# --------------------------------------------------------------------------
# The vocabulary
# --------------------------------------------------------------------------


def test_the_four_sources_are_exactly_the_spec_list():
    assert RESTORE_SOURCES == (
        "MARK_RESTORED",
        "VENDOR_NOTE_INFERRED",
        "SUPERVISOR",
        "ALARM_CLEAR",
    )


def test_an_unknown_source_is_refused(tmp_db):
    """The column is a closed vocabulary; a typo must not become a fifth value."""
    _settings, session = tmp_db
    inc = _incident(session)
    with pytest.raises(ValueError):
        record_restore(inc, source="GUESSED", by="someone")
    assert inc.restored_source is None
    assert inc.restored_at is None


def test_alarm_clear_is_declared_but_unproduced():
    """Reserved for the NMS clear event. Declaring it is not inventing a caller."""
    assert RESTORE_SOURCE_ALARM_CLEAR in RESTORE_SOURCES
    src = (lifecycle.__file__ or "")
    assert src.endswith("lifecycle.py")
    body = open(src, encoding="utf-8").read()
    # It appears as the constant and in RESTORE_SOURCES — never as a source= argument.
    assert "source=RESTORE_SOURCE_ALARM_CLEAR" not in body


# --------------------------------------------------------------------------
# Producer 1 + 2: the work-note path
# --------------------------------------------------------------------------


def test_mark_restored_flag_records_mark_restored(tmp_db):
    _settings, session = tmp_db
    inc = _incident(session, "ASSIGNED")
    apply_work_note_side_effects(
        session, inc, author_role="NOC", author="J. Otieno", body="closing loop", mark_restored=True
    )
    assert inc.status == "RESTORED"
    assert inc.restored_source == RESTORE_SOURCE_MARK
    assert inc.restored_by == "J. Otieno"
    assert inc.restored_at is not None


def test_regex_inference_is_labelled_as_an_inference(tmp_db):
    """The note only *says* restored — that is a guess and the row now says so."""
    _settings, session = tmp_db
    inc = _incident(session)
    apply_work_note_side_effects(
        session, inc, author_role="MSP", author="Egypro NOC", body="Service RESTORED at 0212"
    )
    assert inc.status == "RESTORED"
    assert inc.restored_source == RESTORE_SOURCE_NOTE
    assert inc.restored_by == "Egypro NOC"


def test_flag_wins_over_regex(tmp_db):
    """Both fire on the same note: the human's flag is the stronger claim."""
    _settings, session = tmp_db
    inc = _incident(session)
    apply_work_note_side_effects(
        session, inc, author_role="MSP", author="Egypro NOC", body="service restored", mark_restored=True
    )
    assert inc.restored_source == RESTORE_SOURCE_MARK


def test_author_falls_back_to_the_role_when_unknown(tmp_db):
    """An internal caller that knows only a role still leaves an attribution."""
    _settings, session = tmp_db
    inc = _incident(session)
    apply_work_note_side_effects(session, inc, author_role="MSP", body="Service RESTORED")
    assert inc.restored_by == "MSP"


def test_negated_note_writes_no_provenance(tmp_db):
    _settings, session = tmp_db
    inc = _incident(session)
    apply_work_note_side_effects(session, inc, author_role="MSP", author="Egypro", body="power NOT restored yet")
    assert inc.status == "IN_PROGRESS"
    assert inc.restored_at is None
    assert inc.restored_source is None
    assert inc.restored_by is None


@pytest.mark.parametrize("status", ["CLOSED", "CANCELLED"])
def test_terminal_incident_gets_no_provenance(tmp_db, status):
    _settings, session = tmp_db
    inc = _incident(session, status)
    apply_work_note_side_effects(session, inc, author_role="MSP", author="Egypro", body="Service RESTORED")
    assert inc.status == status
    assert inc.restored_at is None
    assert inc.restored_source is None


def test_timestamp_and_source_are_never_written_apart(tmp_db):
    """The defect was a timestamp with no provenance; the pair moves together."""
    _settings, session = tmp_db
    inc = _incident(session)
    apply_work_note_side_effects(session, inc, author_role="MSP", author="Egypro", body="service up")
    assert (inc.restored_at is None) == (inc.restored_source is None)
    # A second, stronger claim re-stamps BOTH — restored_at has always been
    # overwritten here, so provenance must follow it rather than describe a
    # timestamp it no longer belongs to.
    restore_incident(session, inc, restored_by="Supervisor A", note="confirmed on site")
    assert inc.restored_source == RESTORE_SOURCE_SUPERVISOR
    assert inc.restored_by == "Supervisor A"


# --------------------------------------------------------------------------
# Producer 3: the supervisor route
# --------------------------------------------------------------------------

HUB_EVENT = {
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
    """A live API on its own SQLite file (same reload pattern as the auth tests)."""
    db = tmp_path / "restore.db"
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


def _open_incident(client: TestClient) -> str:
    r = client.post("/api/v1/events", json=HUB_EVENT)
    assert r.status_code == 200, r.text
    return r.json()["incident"]["id"]


def test_route_records_supervisor_and_exposes_it(client):
    inc_id = _open_incident(client)
    r = client.post(f"/api/v1/incidents/{inc_id}/restore", json={"note": "Genset refuelled, site up"})
    assert r.status_code == 200, r.text
    inc = r.json()["incident"]
    assert inc["status"] == "RESTORED"
    assert inc["restored_source"] == "SUPERVISOR"
    assert inc["restored_by"]  # the acting principal, not a client-chosen name
    assert inc["restored_at"].endswith("Z")  # still §7.0.6-stamped
    # and it survives a re-read, i.e. it was committed, not just serialized
    again = client.get(f"/api/v1/incidents/{inc_id}").json()
    assert again["restored_source"] == "SUPERVISOR"
    assert again["restored_by"] == inc["restored_by"]


def test_route_honours_an_observed_restore_time(client):
    """02:41 keystroke, 02:12 restore — MTTR must use the restore, not the typing."""
    inc_id = _open_incident(client)
    observed = datetime.now(timezone.utc) - timedelta(minutes=29)
    r = client.post(
        f"/api/v1/incidents/{inc_id}/restore",
        json={"note": "Field confirmed at 0212", "restored_at": observed.isoformat()},
    )
    assert r.status_code == 200, r.text
    wire = r.json()["incident"]["restored_at"]
    assert wire.startswith(observed.strftime("%Y-%m-%dT%H:%M"))


def test_route_writes_a_work_note_so_the_timeline_shows_it(client):
    inc_id = _open_incident(client)
    client.post(f"/api/v1/incidents/{inc_id}/restore", json={"note": "Genset refuelled"})
    items = client.get(f"/api/v1/incidents/{inc_id}/timeline").json()
    notes = [i for i in items if i["kind"] == "note"]
    assert any(i["detail"] == "Genset refuelled" for i in notes)


def test_route_rejects_an_empty_note(client):
    inc_id = _open_incident(client)
    assert client.post(f"/api/v1/incidents/{inc_id}/restore", json={"note": "   "}).status_code == 400


def test_route_rejects_a_future_restore_time(client):
    inc_id = _open_incident(client)
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    r = client.post(
        f"/api/v1/incidents/{inc_id}/restore",
        json={"note": "early", "restored_at": future.isoformat()},
    )
    assert r.status_code == 400


def test_route_404s_on_an_unknown_incident(client):
    assert client.post("/api/v1/incidents/nope/restore", json={"note": "x"}).status_code == 404


def test_route_refuses_a_closed_incident(client):
    inc_id = _open_incident(client)
    assert client.post(f"/api/v1/incidents/{inc_id}/close", json={"closed_by": "NOC"}).status_code == 200
    r = client.post(f"/api/v1/incidents/{inc_id}/restore", json={"note": "too late"})
    assert r.status_code == 409


def test_note_route_attributes_the_author_not_the_role(client):
    inc_id = _open_incident(client)
    r = client.post(
        f"/api/v1/incidents/{inc_id}/notes",
        json={"author": "Egypro FE Mwangi", "author_role": "MSP", "body": "Service RESTORED"},
    )
    assert r.status_code == 200, r.text
    inc = client.get(f"/api/v1/incidents/{inc_id}").json()
    assert inc["restored_source"] == "VENDOR_NOTE_INFERRED"
    assert inc["restored_by"] == "Egypro FE Mwangi"


def test_serializer_reports_none_for_an_unrestored_incident(tmp_db):
    _settings, session = tmp_db
    out = incident_out(_incident(session)).model_dump()
    assert out["restored_source"] is None
    assert out["restored_by"] is None
    assert out["restored_at"] is None


# --------------------------------------------------------------------------
# Defect #34 — signature and count must be over the same columns
# --------------------------------------------------------------------------


def test_signature_is_site_and_domain_only():
    assert problem_signature("SFC-RFT-HUB-ELD", "POWER") == "SFC-RFT-HUB-ELD|POWER"


def test_one_problem_per_site_and_domain_whatever_the_alarm_code(tmp_db):
    """Four repeats under four alarm codes are ONE chronic power problem.

    Before the fix the count was over site+domain while the signature carried
    alarm_code: the 3rd event opened PRB-a claiming 3, the 4th opened PRB-b
    claiming 4, and neither record was the site's problem.
    """
    _settings, session = tmp_db
    base = EventIngest(
        site_id="SFC-RFT-HUB-ELD",
        site_name="Eldoret Rift HUB",
        site_type="HUB",
        region_code="RFT",
        alarm_code="GENSET_FAIL",
        failure_domain="POWER",
        users_affected=160000,
    )
    for i in range(4):
        process_event(session, _settings, base.model_copy(update={"alarm_code": f"GENSET_FAIL_{i}"}))

    problems = session.scalars(select(ProblemRow)).all()
    assert len(problems) == 1, [p.signature for p in problems]
    problem = problems[0]
    assert problem.signature == "SFC-RFT-HUB-ELD|POWER"

    # The count on the record equals the count the agent counted: every incident
    # at that site+domain inside the lookback window.
    incidents = session.scalars(
        select(IncidentRow).where(
            IncidentRow.site_id == "SFC-RFT-HUB-ELD",
            IncidentRow.failure_domain == "POWER",
        )
    ).all()
    assert problem.occurrence_count == len(incidents)
    # and every incident past the threshold points at that one record
    assert len(problem.linked_incident_ids) == problem.occurrence_count - 2


def test_a_different_failure_domain_is_a_different_problem(tmp_db):
    """``site|domain`` still separates a power fault from a transmission fault."""
    _settings, session = tmp_db
    base = EventIngest(
        site_id="SFC-WNY-HUB-KSM",
        site_name="Kisumu Western-Nyanza HUB",
        site_type="HUB",
        region_code="WNY",
        alarm_code="POWER_OUT",
        failure_domain="POWER",
        users_affected=190000,
    )
    # Distinct alarm codes so correlation opens separate incidents rather than
    # folding the repeats into one — three real repeats per domain.
    for i in range(3):
        process_event(session, _settings, base.model_copy(update={"alarm_code": f"POWER_OUT_{i}"}))
    for i in range(3):
        process_event(
            session,
            _settings,
            base.model_copy(update={"failure_domain": "TRANSMISSION", "alarm_code": f"LOS_{i}"}),
        )

    sigs = sorted(p.signature for p in session.scalars(select(ProblemRow)).all())
    assert sigs == ["SFC-WNY-HUB-KSM|POWER", "SFC-WNY-HUB-KSM|TRANSMISSION"]
