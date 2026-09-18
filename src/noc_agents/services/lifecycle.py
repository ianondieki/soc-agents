from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from noc_agents.config import OperatorConfig, get_settings
from noc_agents.db.models import IncidentBriefRow, IncidentRow, WorkNoteRow, utcnow
from noc_agents.domain.enums import IncidentStatus
from noc_agents.realtime.hub import RealtimeEvent, hub
from noc_agents.services.composition import compose_brief
from noc_agents.services.hitl import sync_incident_hitl_scalars

TERMINAL_STATUSES = (IncidentStatus.CLOSED.value, IncidentStatus.CANCELLED.value)

# --------------------------------------------------------------------------
# Restore provenance (spec §7.0.8)
# --------------------------------------------------------------------------
# Every SLA number downstream (MTTR, restore-within-SLA, the ledger, the exec
# brief) is computed from ``restored_at``. Until now nothing recorded HOW that
# timestamp arrived, so a supervisor-confirmed field restore and a regex hit on
# a vendor's SMS were indistinguishable once written. These four values are the
# whole vocabulary; ``restored_source`` is never set to anything else.
RESTORE_SOURCE_MARK = "MARK_RESTORED"  # a human ticked "mark restored" on a work note
RESTORE_SOURCE_NOTE = "VENDOR_NOTE_INFERRED"  # inferred by note_declares_restored() — a guess
RESTORE_SOURCE_SUPERVISOR = "SUPERVISOR"  # POST /api/v1/incidents/{id}/restore
RESTORE_SOURCE_ALARM_CLEAR = "ALARM_CLEAR"  # reserved: NMS clear event. NO PRODUCER YET.

#: The closed set the column may hold, in the spec's order.
RESTORE_SOURCES: tuple[str, ...] = (
    RESTORE_SOURCE_MARK,
    RESTORE_SOURCE_NOTE,
    RESTORE_SOURCE_SUPERVISOR,
    RESTORE_SOURCE_ALARM_CLEAR,
)

_RESTORE = re.compile(r"\b(RESTORED|SERVICE UP)\b")
# A negation word before the match, separated only by filler words (YET/BEEN/FULLY/STILL), so
# "not restored", "not yet been restored" and "has not been fully restored" all count as negated.
# Deliberately narrow: bare NO / PENDING / UN would also suppress affirmative notes such as
# "No alarms now service restored" or "RCA pending - power restored".
_NEGATION = re.compile(r"\b(NOT|NEVER|ISN'?T|WASN'?T|HASN'?T)\W+((YET|BEEN|FULLY|STILL)\W+)*$")


# --------------------------------------------------------------------------
# Executive brief refresh (spec §5.3.8, defect #24)
# --------------------------------------------------------------------------
# The EXEC_BRIEF node publishes the brief once, at ingest, and nothing ever
# touched it again: by the time management read "Owner: NOC Queue / What we
# know: …" the ticket had been reassigned, restored and closed. The brief is
# what exists so executives stop phoning the NOC, so a stale one is worse than
# none. Every place that changes what the brief SAYS now refreshes it — a work
# note, a close, a HITL approval (main.hitl_approve).


def upsert_brief(session: Session, inc: IncidentRow, cfg: OperatorConfig | None = None) -> IncidentBriefRow | None:
    """Re-compose this incident's exec brief in place. Returns the row, or None.

    Refresh, never first publication: the row is updated when the incident already has a
    brief and **nothing is inserted when it does not**. Whether an incident that the
    EXEC_BRIEF node never briefed (a merge/cascade short-circuit, a node that failed soft)
    should acquire its first brief because someone typed a note is a product decision, and
    guessing it would also move the ``IncidentBriefRow`` count the §2.1 golden register
    pins. Reported, not guessed — see the wave report.

    ``cfg`` defaults to the active operator profile; every caller here is already scoped to
    it (the routes fetch the incident through ``main._get_owned``).

    Runs inside the caller's transaction and only flushes: the brief commits with whatever
    changed it, so a rolled-back note leaves the old brief standing rather than a brief
    describing a note that never happened.
    """
    row = session.scalar(
        select(IncidentBriefRow)
        .where(IncidentBriefRow.incident_id == inc.id)
        .order_by(IncidentBriefRow.updated_at.desc())
    )
    if row is None:
        return None
    body = compose_brief(cfg or get_settings().operator, inc)
    if row.body != body:  # an identical re-compose is not an update; keep updated_at honest
        row.body = body
        row.updated_at = utcnow()
        session.flush()
    return row


def note_declares_restored(body: str) -> bool:
    """True when the note says the service is restored and that mention is not negated."""
    text = body.upper()
    return any(not _NEGATION.search(text[: m.start()]) for m in _RESTORE.finditer(text))


def record_restore(
    inc: IncidentRow,
    *,
    source: str,
    by: str | None,
    at: datetime | None = None,
    evidence: str = "",
) -> None:
    """Move ``inc`` to RESTORED and stamp *when*, *how* and *who* in one place.

    The three columns are written together and never apart: a ``restored_at``
    without a ``restored_source`` is exactly the ambiguity §7.0.8 exists to end.
    Callers do the status guarding (terminal tickets never reach here).

    ``at`` must already be naive UTC (the storage contract, see services/clock);
    ``None`` means now. Re-restoring overwrites all three, because the previous
    behaviour overwrote ``restored_at`` and provenance must not be allowed to
    describe a timestamp it no longer belongs to.
    """
    if source not in RESTORE_SOURCES:
        raise ValueError(f"unknown restored_source {source!r}; expected one of {list(RESTORE_SOURCES)}")
    inc.status = IncidentStatus.RESTORED.value
    inc.restored_at = at or utcnow()
    inc.restored_source = source
    inc.restored_by = (by or "").strip() or None
    inc.msp_percent_complete = 100
    if not inc.resolution_code:
        inc.resolution_code = "FIELD_RESTORED"
    if not inc.resolution_summary:
        inc.resolution_summary = evidence[:500]
    if not inc.msp_action_taken:
        inc.msp_action_taken = evidence[:500]


def restore_incident(
    session: Session,
    inc: IncidentRow,
    *,
    restored_by: str,
    note: str,
    restored_at: datetime | None = None,
    source: str = RESTORE_SOURCE_SUPERVISOR,
) -> WorkNoteRow:
    """Explicit, attributed restore — the ``SUPERVISOR`` path (spec §7.0.8).

    Unlike the work-note path this never guesses: the caller is a named human
    who is asserting the service is back, optionally at a time they observed
    rather than the moment they got to a keyboard. A work note is written so the
    assertion is visible on the timeline, not only in a column.

    The caller checks ``inc.status in TERMINAL_STATUSES`` first (the route turns
    that into a 409); this function does not resurrect closed tickets.
    """
    record_restore(inc, source=source, by=restored_by, at=restored_at, evidence=note)
    row = WorkNoteRow(
        incident_id=inc.id,
        author=restored_by,
        author_role="NOC",
        body=note,
        source="restore",
    )
    session.add(row)
    inc.updated_at = utcnow()
    session.flush()
    return row


def apply_work_note_side_effects(
    session: Session,
    inc: IncidentRow,
    *,
    author_role: str,
    body: str,
    author: str | None = None,
    mark_restored: bool = False,
    vendor_tt_ref: str | None = None,
    msp_eta_at=None,
    msp_root_cause: str | None = None,
    msp_action_taken: str | None = None,
    msp_percent_complete: int | None = None,
) -> None:
    """When MSP/FE posts notes, move ticket through real NOC statuses.

    ``author`` is the person the note is attributed to; it becomes
    ``restored_by`` when this note restores the ticket. Callers that know only a
    role (the direct-call tests, any internal caller) leave it ``None`` and the
    role is stored instead, so the column is never silently empty.
    """
    role = author_role.upper()
    if vendor_tt_ref:
        inc.vendor_tt_ref = vendor_tt_ref
    if msp_eta_at is not None:
        inc.msp_eta_at = msp_eta_at
    if msp_root_cause:
        inc.msp_root_cause = msp_root_cause
    if msp_action_taken:
        inc.msp_action_taken = msp_action_taken
    if msp_percent_complete is not None:
        inc.msp_percent_complete = max(0, min(100, int(msp_percent_complete)))

    if role in ("MSP", "FE", "RNIO") and inc.status in (
        IncidentStatus.ASSIGNED.value,
        IncidentStatus.AWAITING_VENDOR.value,
        IncidentStatus.TICKETED.value,
    ):
        inc.status = IncidentStatus.IN_PROGRESS.value
        if not inc.first_vendor_note_at:
            inc.first_vendor_note_at = utcnow()
        if not inc.acknowledged_at:
            inc.acknowledged_at = utcnow()

    terminal = inc.status in TERMINAL_STATUSES
    if not terminal and (mark_restored or note_declares_restored(body)):
        # The flag is a human saying so; the regex is an inference from free text.
        # Which one fired is the difference between a fact and a guess, so it is
        # recorded rather than reconstructed later from the note body.
        record_restore(
            inc,
            source=RESTORE_SOURCE_MARK if mark_restored else RESTORE_SOURCE_NOTE,
            by=author or author_role,
            evidence=body,
        )

    inc.updated_at = utcnow()
    session.flush()
    upsert_brief(session, inc)  # defect #24: the note may have changed owner/status/cause


def close_incident(
    session: Session,
    inc: IncidentRow,
    *,
    closed_by: str,
    resolution_code: str = "CLOSED_NORMAL",
    resolution_summary: str = "",
) -> None:
    inc.status = IncidentStatus.CLOSED.value
    inc.closed_at = utcnow()
    inc.resolution_code = resolution_code
    if resolution_summary:
        inc.resolution_summary = resolution_summary
    elif not inc.resolution_summary:
        inc.resolution_summary = f"Closed by {closed_by}"
    if not inc.restored_at:
        inc.restored_at = inc.closed_at
    session.add(
        WorkNoteRow(
            incident_id=inc.id,
            author=closed_by,
            author_role="NOC",
            body=f"Ticket closed ({resolution_code}): {inc.resolution_summary}",
            source="ui",
        )
    )
    sync_incident_hitl_scalars(session, inc)  # open tasks are not cancelled on close (deferred); keep scalars honest
    session.flush()
    upsert_brief(session, inc)  # defect #24: a closed ticket must not brief as "investigating"
    hub.publish_sync(
        RealtimeEvent(
            type="incident.closed",
            operator_id=inc.operator_id,
            incident_id=inc.id,
            payload={"incident_number": inc.incident_number, "resolution_code": resolution_code},
        )
    )


def reassign_incident(
    session: Session,
    inc: IncidentRow,
    *,
    assignee_type: str,
    assignee_name: str,
    msp_name: str | None,
    fe_name: str | None,
    by: str,
    reason: str,
) -> None:
    old = inc.assignee_name
    inc.assignee_type = assignee_type
    inc.assignee_name = assignee_name
    inc.msp_name = msp_name
    if fe_name:
        inc.fe_name = fe_name
    if assignee_type == "MSP":
        inc.status = IncidentStatus.AWAITING_VENDOR.value
    else:
        inc.status = IncidentStatus.ASSIGNED.value
    inc.updated_at = utcnow()
    session.add(
        WorkNoteRow(
            incident_id=inc.id,
            author=by,
            author_role="NOC",
            body=f"Reassigned {old} → {assignee_name} ({assignee_type}). Reason: {reason}",
            source="reassign",
        )
    )
    session.flush()
    hub.publish_sync(
        RealtimeEvent(
            type="incident.reassigned",
            operator_id=inc.operator_id,
            incident_id=inc.id,
            payload={
                "incident_number": inc.incident_number,
                "from": old,
                "to": assignee_name,
                "reason": reason,
            },
        )
    )
