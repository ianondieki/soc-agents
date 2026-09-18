"""Realtime after commit (spec §7.0.4), the lifecycle half: ``close_incident`` / ``reassign_incident``.

``RunTracker`` was moved onto :func:`buffer_event` when §7.0.4 landed, but the two
supervisor-driven lifecycle writes were missed and kept calling ``hub.publish_sync`` from
inside the caller's open transaction. Both are called by routes that commit *after* they
return (``main.close_inc`` and ``main.reassign_inc``), so a failure anywhere between the
call and that commit rolled the ticket back while the UI had already been told the ticket
was closed, or handed to a new owner — a closed ticket on the wall board that is still open
in the database is exactly the lie §7.0.4 exists to prevent.

Pinned here, per site:

* inside the transaction the event is parked on the session and NOTHING has reached the hub;
* a rollback discards it — the UI is never told about a row the database did not keep;
* a commit still publishes it exactly once, with its payload intact and the row durable at
  the moment of the announce. The fix must not become "the event is dropped".

``worklog_monitor.chase_silent_incidents`` is deliberately absent: it publishes after its own
``session.commit()``, so it is already correct, and buffering there would strand its events on
a session whose transaction has already ended (``POST /api/v1/monitor/tick`` then closes it).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from noc_agents.db.models import IncidentRow, WorkNoteRow, get_session
from noc_agents.realtime.commit_hook import pending_events
from noc_agents.realtime.hub import hub
from noc_agents.services.lifecycle import close_incident, reassign_incident


@pytest.fixture()
def clean_hub():
    hub._history.clear()
    yield hub
    hub._history.clear()


def _incident(session, status: str = "IN_PROGRESS") -> IncidentRow:
    inc = IncidentRow(
        operator_id="safaricom",
        incident_number="INC-2026-000777",
        status=status,
        site_id="SFC-MTK-HUB-THK",
        region_code="MTK",
        correlation_fingerprint="fp-lifecycle-events",
        assignee_type="NOC",
        assignee_name="NOC Queue",
    )
    session.add(inc)
    session.commit()  # the incident itself is durable before the test's transaction of interest
    return inc


def _of_type(event_type: str) -> list[dict]:
    return [e for e in hub._history if e["type"] == event_type]


def _in_new_session(read):
    other = get_session()
    try:
        return read(other)
    finally:
        other.close()


# --- close: the rollback case (the defect) ---------------------------------------------------


def test_rolled_back_close_publishes_no_incident_closed_event(tmp_db, clean_hub):
    """The defect: with ``hub.publish_sync`` the event was on the wire before the rollback."""
    _settings, session = tmp_db
    inc = _incident(session)

    close_incident(session, inc, closed_by="Supervisor", resolution_code="CLOSED_NORMAL", resolution_summary="OK")

    # Still inside the transaction: parked on the session, nothing has left the process.
    assert [e.type for e in pending_events(session)] == ["incident.closed"]
    assert _of_type("incident.closed") == []

    session.rollback()

    assert pending_events(session) == []
    assert _of_type("incident.closed") == []
    # Discarded, not deferred: a later, unrelated commit on the same session publishes nothing.
    session.add(WorkNoteRow(incident_id=inc.id, author="NOC", author_role="NOC", body="later", source="ui"))
    session.commit()
    assert _of_type("incident.closed") == []
    # And the ticket the UI was not told about is genuinely still open.
    assert _in_new_session(lambda s: s.get(IncidentRow, inc.id).status) == "IN_PROGRESS"


def test_committed_close_publishes_incident_closed_exactly_once_and_durable(tmp_db, clean_hub):
    """The other half of the fix: buffering must defer the event, never drop it."""
    _settings, session = tmp_db
    inc = _incident(session)

    close_incident(session, inc, closed_by="Supervisor", resolution_code="CLOSED_NORMAL", resolution_summary="OK")
    session.commit()

    (event,) = _of_type("incident.closed")
    assert event["incident_id"] == inc.id
    assert event["operator_id"] == "safaricom"
    assert event["payload"] == {"incident_number": "INC-2026-000777", "resolution_code": "CLOSED_NORMAL"}
    assert pending_events(session) == []
    # Durable at announce: another session can read the CLOSED row the UI was just told about.
    assert _in_new_session(lambda s: s.get(IncidentRow, inc.id).status) == "CLOSED"


# --- reassign: the same two cases ------------------------------------------------------------


def _reassign(session, inc: IncidentRow) -> None:
    reassign_incident(
        session,
        inc,
        assignee_type="MSP",
        assignee_name="Adrian Kenya",
        msp_name="Adrian Kenya",
        fe_name=None,
        by="Supervisor",
        reason="Fibre team closer to site",
    )


def test_rolled_back_reassign_publishes_no_incident_reassigned_event(tmp_db, clean_hub):
    _settings, session = tmp_db
    inc = _incident(session)

    _reassign(session, inc)

    assert [e.type for e in pending_events(session)] == ["incident.reassigned"]
    assert _of_type("incident.reassigned") == []

    session.rollback()

    assert pending_events(session) == []
    assert _of_type("incident.reassigned") == []
    # The owner the UI was not told about is still the owner, and the note was rolled back too.
    assert _in_new_session(lambda s: s.get(IncidentRow, inc.id).assignee_name) == "NOC Queue"
    assert _in_new_session(lambda s: s.scalars(select(WorkNoteRow)).all()) == []


def test_committed_reassign_publishes_incident_reassigned_exactly_once(tmp_db, clean_hub):
    _settings, session = tmp_db
    inc = _incident(session)

    _reassign(session, inc)
    session.commit()

    (event,) = _of_type("incident.reassigned")
    assert event["incident_id"] == inc.id
    assert event["payload"] == {
        "incident_number": "INC-2026-000777",
        "from": "NOC Queue",
        "to": "Adrian Kenya",
        "reason": "Fibre team closer to site",
    }
    assert pending_events(session) == []
    assert _in_new_session(lambda s: s.get(IncidentRow, inc.id).assignee_name) == "Adrian Kenya"
