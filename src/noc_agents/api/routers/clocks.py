"""Stop-clock routes (spec §7.6.3) -- the workspace control behind the "stop clock" button:

    GET  /api/v1/incidents/{id}/clock                      events + minutes deducted so far
    POST /api/v1/incidents/{id}/clock                      open   {scc_code, reason, started_at?, evidence_note_id?}
    POST /api/v1/incidents/{id}/clock/{event_id}/close     close  {ended_at?, reason?}
    POST /api/v1/incidents/{id}/clock/{event_id}/reverse   reverse {reason}   (reason mandatory)

Behind ``SCORECARDS_ENABLED`` (default OFF): 404 on every route while the flag is off, so
the surface is exactly what it was before this lane existed.

Roles (§7.6.3): ``noc_analyst``+ opens, ``shift_supervisor``+ closes/reverses. Two layers
enforce it. ``require_role`` bites once ``AUTH_DISABLED=false``. The service layer checks
the acting role again on every call because ``require_role`` is inert in the demo default
and §7.6.6 says an SCC opened by a vendor role is a 403 regardless -- so the role switcher
set to ``msp_coordinator`` is refused here even with auth off.

Every incident and event is fetched through ``_get_owned``: another operator's incident,
or an event that belongs to another incident, is a 404 -- never a 403 (§8).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from noc_agents.api import auth
from noc_agents.api.auth import require_role
from noc_agents.api.deps import INCIDENT_READERS, OPERATIONS, SUPERVISORS, _actor, _get_owned, _settings
from noc_agents.db.models import IncidentRow, get_session
from noc_agents.db.models_vendors import ClockEventRow
from noc_agents.services.clock import z_utc
from noc_agents.services.clock_events import (
    ClockPermissionError,
    ClockStateError,
    clock_event_out,
    close_clock_event,
    deducted_minutes,
    incident_window,
    late_openings,
    list_clock_events,
    open_clock_event,
    reverse_clock_event,
)
from noc_agents.services.vendors import FLAG, lane_enabled

router = APIRouter(prefix="/api/v1", tags=["clocks"])


def require_lane() -> None:
    """Dependency: 404 while ``SCORECARDS_ENABLED`` is off."""
    if not lane_enabled():
        raise HTTPException(404, f"Not found ({FLAG} is off)")


class ClockOpenIn(BaseModel):
    """``started_at`` may be earlier than now (the condition began before anyone typed);
    the server records ``opened_at`` itself. ``opened_by`` is honoured only while auth is
    off (``_actor``), exactly like the other write routes."""

    scc_code: str
    reason: str
    started_at: datetime | None = None
    evidence_note_id: str | None = None
    opened_by: str | None = None


class ClockCloseIn(BaseModel):
    ended_at: datetime | None = None
    reason: str | None = None
    closed_by: str | None = None


class ClockReverseIn(BaseModel):
    reason: str
    reversed_by: str | None = None


def _event_on(session, inc: IncidentRow, event_id: str) -> ClockEventRow:
    """The event, if the active operator owns it AND it is on this incident; else 404."""
    ev = _get_owned(session, ClockEventRow, event_id, what="clock event")
    if ev.incident_id != inc.id:
        raise HTTPException(404, "clock event not found")
    return ev


def _clock_view(session, inc: IncidentRow) -> dict:
    events = list_clock_events(session, inc.id)
    start, end = incident_window(inc)
    return {
        "incident_id": inc.id,
        "incident_number": inc.incident_number,
        "vendor_id": inc.vendor_id,
        "window_start": z_utc(start),
        "window_end": z_utc(end),
        "restored": inc.restored_at is not None,
        # Minutes the vendor is NOT charged for inside this incident's own outage window
        # so far (live until restored). The scorecard recomputes over ITS period window.
        "deducted_minutes": deducted_minutes(events, window_start=start, window_end=end),
        # Operator-side discipline (§7.6.2): how many of our records were late.
        "late_openings": len(late_openings(events)),
        "events": [clock_event_out(ev) for ev in events],
    }


# §9.3 row 1 read (incident surface): INCIDENT_READERS -- legal R, the vendor roles "notes only".
@router.get("/incidents/{incident_id}/clock", dependencies=[Depends(require_lane), Depends(require_role(*INCIDENT_READERS))])
def get_clock(incident_id: str) -> dict:
    session = get_session()
    try:
        inc = _get_owned(session, IncidentRow, incident_id, what="incident")
        return _clock_view(session, inc)
    finally:
        session.close()


@router.post("/incidents/{incident_id}/clock", dependencies=[Depends(require_lane)])
def open_clock(
    incident_id: str,
    body: ClockOpenIn,
    principal: auth.Principal = Depends(require_role(*OPERATIONS)),
) -> dict:
    s = _settings()
    session = get_session()
    try:
        inc = _get_owned(session, IncidentRow, incident_id, what="incident")
        try:
            ev = open_clock_event(
                session,
                inc,
                scc_code=body.scc_code,
                reason=body.reason,
                opened_by=_actor(principal, body.opened_by),
                opened_role=principal.role,
                started_at=body.started_at,
                evidence_note_id=body.evidence_note_id,
                cfg=s.operator,
            )
        except ClockPermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        session.commit()
        return {"ok": True, "event": clock_event_out(ev), "clock": _clock_view(session, inc)}
    finally:
        session.close()


@router.post("/incidents/{incident_id}/clock/{event_id}/close", dependencies=[Depends(require_lane)])
def close_clock(
    incident_id: str,
    event_id: str,
    body: ClockCloseIn,
    principal: auth.Principal = Depends(require_role(*SUPERVISORS)),
) -> dict:
    session = get_session()
    try:
        inc = _get_owned(session, IncidentRow, incident_id, what="incident")
        ev = _event_on(session, inc, event_id)
        try:
            close_clock_event(
                session,
                inc,
                ev,
                closed_by=_actor(principal, body.closed_by),
                closed_role=principal.role,
                ended_at=body.ended_at,
                reason=body.reason,
            )
        except ClockPermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ClockStateError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        session.commit()
        return {"ok": True, "event": clock_event_out(ev), "clock": _clock_view(session, inc)}
    finally:
        session.close()


@router.post("/incidents/{incident_id}/clock/{event_id}/reverse", dependencies=[Depends(require_lane)])
def reverse_clock(
    incident_id: str,
    event_id: str,
    body: ClockReverseIn,
    principal: auth.Principal = Depends(require_role(*SUPERVISORS)),
) -> dict:
    session = get_session()
    try:
        inc = _get_owned(session, IncidentRow, incident_id, what="incident")
        ev = _event_on(session, inc, event_id)
        try:
            reverse_clock_event(
                session,
                inc,
                ev,
                reversed_by=_actor(principal, body.reversed_by),
                reversed_role=principal.role,
                reason=body.reason,
            )
        except ClockPermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ClockStateError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        session.commit()
        return {"ok": True, "event": clock_event_out(ev), "clock": _clock_view(session, inc)}
    finally:
        session.close()
