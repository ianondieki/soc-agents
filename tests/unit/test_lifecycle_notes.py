"""Stage C6: a note only restores a ticket when it says so without negation, and never a closed one."""

from __future__ import annotations

import pytest

from noc_agents.db.models import IncidentRow
from noc_agents.services.lifecycle import apply_work_note_side_effects, note_declares_restored

RESTORED_NOTES = [
    "Service RESTORED",
    "No alarms now service restored",  # bare NO must not suppress
    "MSP says no truck needed power restored",
    "RCA pending - power restored",  # PENDING must not suppress
    "not sure why, but service restored",  # NOT far from the match
    "Genset on, no issue, power restored",
    "service up on genset",
    "partially restored",  # product decision: still restores today
]
NOT_RESTORED_NOTES = [
    "power NOT RESTORED yet",
    "site not yet restored",
    "still not restored",
    "isn't restored",
    "hasn't been restored",
    "has not been restored",
    "power has not yet been restored",  # two filler words between NOT and the match
    "not yet fully restored",
    "has not been fully restored",
    "service has not yet been restored to normal",
    "is still not restored",
    "never restored",
    "wasn't restored after the splice",
    "unrestored",  # no word boundary between N and R
    "service not up yet",  # contains no SERVICE UP, same as before
    "Joint located; splicing started",
]


@pytest.mark.parametrize("body", RESTORED_NOTES)
def test_affirmative_notes_declare_restored(body):
    assert note_declares_restored(body) is True


@pytest.mark.parametrize("body", NOT_RESTORED_NOTES)
def test_negated_notes_do_not_declare_restored(body):
    assert note_declares_restored(body) is False


def _incident(session, status: str) -> IncidentRow:
    inc = IncidentRow(
        operator_id="safaricom",
        incident_number=f"INC{status[:6]}",
        status=status,
        site_id="SFC-MTK-HUB-THK",
        region_code="MTK",
        correlation_fingerprint="fp",
    )
    session.add(inc)
    session.flush()
    return inc


def test_negated_note_keeps_status(tmp_db):
    _settings, session = tmp_db
    inc = _incident(session, "IN_PROGRESS")
    apply_work_note_side_effects(session, inc, author_role="MSP", body="power NOT restored yet")
    assert inc.status == "IN_PROGRESS"
    assert inc.restored_at is None


def test_affirmative_note_restores(tmp_db):
    _settings, session = tmp_db
    inc = _incident(session, "IN_PROGRESS")
    apply_work_note_side_effects(session, inc, author_role="MSP", body="No alarms now service restored")
    assert inc.status == "RESTORED"
    assert inc.msp_percent_complete == 100


@pytest.mark.parametrize("status", ["CLOSED", "CANCELLED"])
def test_terminal_incident_never_moves(tmp_db, status):
    _settings, session = tmp_db
    inc = _incident(session, status)
    apply_work_note_side_effects(session, inc, author_role="MSP", body="Service RESTORED")
    assert inc.status == status
    apply_work_note_side_effects(session, inc, author_role="MSP", body="late note", mark_restored=True)
    assert inc.status == status
    assert inc.restored_at is None


def test_mark_restored_flag_still_restores_open_ticket(tmp_db):
    _settings, session = tmp_db
    inc = _incident(session, "ASSIGNED")
    apply_work_note_side_effects(session, inc, author_role="NOC", body="closing loop", mark_restored=True)
    assert inc.status == "RESTORED"
