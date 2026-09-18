"""Stop-clock events (SCCs) on an incident: open, close, reverse, and the arithmetic the
scorecard deducts with (spec §7.6.1-§7.6.2) -- Phase 4 Lane 4A, step 1.

The commercial crux of §7.6 is in the bottom half of this file, and it is pure: no
session, no clock, just intervals. Three properties are pinned by ``test_clock_events.py``
and must survive any refactor here:

* **Overlapping events never double-deduct.** Two SCCs covering the same hour deduct one
  hour. The deduction is the length of the UNION of the effective intervals, not the sum.
* **An event still open at the period end is bounded by the period end.** A stop clock
  someone forgot to close does not deduct minutes from next month.
* **A reversed event deducts nothing.** It stays in the table -- the claim was made -- but
  contributes no interval.

The top half is the write path. Two rules from §7.6.3/§7.6.6 are enforced HERE and not
only by ``require_role`` on the route, because ``require_role`` is inert while
``AUTH_DISABLED=true`` (the demo default) and the spec is explicit that an SCC opened by
a vendor role is a 403 regardless: only NOC/supervisor roles may open (``opened_role``),
and only supervisors may close or reverse. ``opened_at`` is always the server clock: it
is the operator-side discipline counter (``opened_at - started_at``), and a client that
could supply it could also erase its own lateness.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from noc_agents.api.deps import OPERATIONS, SUPERVISORS
from noc_agents.config import OperatorConfig
from noc_agents.db.models import AuditRow, IncidentRow, WorkNoteRow, utcnow
from noc_agents.db.models_vendors import SCC_CODES, ClockEventRow
from noc_agents.realtime.commit_hook import buffer_event
from noc_agents.realtime.hub import RealtimeEvent
from noc_agents.services.clock import to_utc, z_utc
from noc_agents.services.vendors import attach_vendor

__all__ = [
    "CLOSER_ROLES",
    "ClockPermissionError",
    "ClockStateError",
    "Interval",
    "LATE_OPENING_THRESHOLD_MIN",
    "OPENER_ROLES",
    "SCC_CODES",
    "clock_event_out",
    "close_clock_event",
    "deducted_minutes",
    "deducted_minutes_for_incident",
    "deducted_timedelta",
    "effective_intervals",
    "incident_window",
    "late_openings",
    "list_clock_events",
    "open_clock_event",
    "opening_delay_minutes",
    "reverse_clock_event",
    "scc_breakdown",
]

#: Who may OPEN an SCC (§7.6.3 "noc_analyst may open"): the operations floor.
OPENER_ROLES: tuple[str, ...] = OPERATIONS
#: Who may CLOSE or REVERSE one (§7.6.3 "shift_supervisor+ closes/reverses").
CLOSER_ROLES: tuple[str, ...] = SUPERVISORS

#: §7.6.2 operator discipline counter: an SCC recorded more than this many minutes after
#: its own ``started_at`` counts against the OPERATOR's data hygiene on the card. The
#: policy value also lives in config/sla_terms.yaml (scorecards.late_scc_opening_minutes);
#: this is the code default the scorecard lane may override from there.
LATE_OPENING_THRESHOLD_MIN = 60

#: Tolerated clock skew when a client supplies a timestamp "now-ish" (same as /restore).
_SKEW = timedelta(minutes=1)


class ClockPermissionError(PermissionError):
    """The acting role may not do this to a stop clock (route: 403)."""


class ClockStateError(RuntimeError):
    """The event is not in a state that allows this transition (route: 409)."""


# ----------------------------------------------------------------------- write path


def _naive_utc(value: datetime | None) -> datetime | None:
    """Store naive UTC (the DB contract); an offset-aware input is converted, never
    silently written as a local wall clock (§7.0.6, defect #41)."""
    if value is None:
        return None
    return to_utc(value).replace(tzinfo=None)


def _audit(session: Session, inc: IncidentRow, *, actor: str, action: str, ev: ClockEventRow, rationale: str) -> None:
    """One audit row per transition, flushed at once: the session factory is autoflush=False,
    so without the flush a same-transaction reader (the evidence pack, a test) would not see
    the row until something else happened to flush."""
    session.add(
        AuditRow(
            operator_id=inc.operator_id,
            actor=actor,
            action=action,
            entity_type="clock_event",
            entity_id=ev.id,
            rationale=rationale,
            payload_json=str(
                {
                    "incident_id": inc.id,
                    "incident_number": inc.incident_number,
                    "scc_code": ev.scc_code,
                    "started_at": ev.started_at.isoformat(),
                    "ended_at": ev.ended_at.isoformat() if ev.ended_at else None,
                }
            )[:2000],
        )
    )
    session.flush()


def _announce(session: Session, inc: IncidentRow, ev: ClockEventRow, change: str) -> None:
    """Tell the workspace after commit -- never before (§7.0.4). ``incident.updated`` is
    the Appendix C type for "something on the ticket changed"; no new WS type is coined."""
    buffer_event(
        session,
        RealtimeEvent(
            type="incident.updated",
            operator_id=inc.operator_id,
            incident_id=inc.id,
            payload={
                "incident_number": inc.incident_number,
                "change": change,
                "event_id": ev.id,
                "scc_code": ev.scc_code,
            },
        ),
    )


def open_clock_event(
    session: Session,
    inc: IncidentRow,
    *,
    scc_code: str,
    reason: str,
    opened_by: str,
    opened_role: str,
    started_at: datetime | None = None,
    evidence_note_id: str | None = None,
    cfg: OperatorConfig | None = None,
    now: datetime | None = None,
) -> ClockEventRow:
    """Record a Stop Clock Condition on ``inc``. Flushes; the caller commits.

    * ``scc_code`` must be one of ``SCC_CODES`` and ``reason`` non-empty (``ValueError``).
    * ``opened_role`` must be an operations role (``ClockPermissionError``): §7.6.6, an SCC
      opened by a vendor role is refused even when auth is off.
    * ``started_at`` defaults to now and may be earlier (the condition began before anyone
      typed) but never later than now plus a minute of skew (``ValueError``); the gap
      between it and ``opened_at`` (= now, server-side) is the discipline counter.
    * ``evidence_note_id`` must be a work note ON THIS INCIDENT (``ValueError``): the
      evidence for a deduction cannot be borrowed from another ticket.
    * Also makes ``inc.vendor_id`` mean something: a stop clock is vendor-attributable, so
      if the incident has a resolvable ``msp_name`` and no ``vendor_id`` yet, it is set now
      (``services.vendors.attach_vendor``; needs ``cfg`` -- omitted means "skip").
    """
    code = (scc_code or "").strip().upper()
    if code not in SCC_CODES:
        raise ValueError(f"unknown scc_code {scc_code!r}; expected one of {list(SCC_CODES)}")
    text = (reason or "").strip()
    if not text:
        raise ValueError("reason is required")
    role = (opened_role or "").strip()
    if role not in OPENER_ROLES:
        raise ClockPermissionError(f"role {role!r} may not open a stop clock; only {list(OPENER_ROLES)}")
    who = (opened_by or "").strip()
    if not who:
        raise ValueError("opened_by is required")

    at = now or utcnow()
    start = _naive_utc(started_at) or at
    if start > at + _SKEW:
        raise ValueError("started_at cannot be in the future")

    note_id = (evidence_note_id or "").strip() or None
    if note_id is not None:
        note = session.get(WorkNoteRow, note_id)
        if note is None or note.incident_id != inc.id:
            raise ValueError("evidence_note_id is not a work note on this incident")

    if cfg is not None:
        attach_vendor(session, inc, cfg)

    ev = ClockEventRow(
        incident_id=inc.id,
        scc_code=code,
        started_at=start,
        ended_at=None,
        opened_by=who,
        opened_role=role,
        opened_at=at,  # server clock, by construction -- see the module docstring
        reason=text,
        evidence_note_id=note_id,
        created_at=at,
    )
    session.add(ev)
    inc.updated_at = at
    session.flush()
    _audit(session, inc, actor=who, action="clock.opened", ev=ev, rationale=text)
    _announce(session, inc, ev, "clock.opened")
    return ev


def close_clock_event(
    session: Session,
    inc: IncidentRow,
    ev: ClockEventRow,
    *,
    closed_by: str,
    closed_role: str,
    ended_at: datetime | None = None,
    reason: str | None = None,
    now: datetime | None = None,
) -> ClockEventRow:
    """End a running SCC. Supervisors only. ``ended_at`` defaults to now, may be earlier
    (the condition ended before the keystroke -- same argument as ``/restore``), never
    before ``started_at`` and never in the future. 409 (``ClockStateError``) if the event
    is already closed or has been reversed. ``reason`` is optional here (the datum is the
    end time) and goes to the audit row."""
    role = (closed_role or "").strip()
    if role not in CLOSER_ROLES:
        raise ClockPermissionError(f"role {role!r} may not close a stop clock; only {list(CLOSER_ROLES)}")
    if ev.incident_id != inc.id:
        raise ValueError("clock event does not belong to this incident")
    if ev.reversed_at is not None:
        raise ClockStateError("clock event has been reversed")
    if ev.ended_at is not None:
        raise ClockStateError("clock event is already closed")
    at = now or utcnow()
    end = _naive_utc(ended_at) or at
    if end > at + _SKEW:
        raise ValueError("ended_at cannot be in the future")
    if end < ev.started_at:
        raise ValueError("ended_at is before started_at")
    ev.ended_at = end
    inc.updated_at = at
    session.flush()
    _audit(session, inc, actor=(closed_by or "").strip() or role, action="clock.closed", ev=ev, rationale=(reason or "").strip())
    _announce(session, inc, ev, "clock.closed")
    return ev


def reverse_clock_event(
    session: Session,
    inc: IncidentRow,
    ev: ClockEventRow,
    *,
    reversed_by: str,
    reversed_role: str,
    reason: str,
    now: datetime | None = None,
) -> ClockEventRow:
    """Withdraw an SCC. Supervisors only; a non-empty ``reason`` is mandatory (a deduction
    that silently disappears is as indefensible as one that silently appears). The row is
    kept with ``reversed_at/by/reason`` set and deducts nothing from then on; a second
    reversal is a 409. An open event may be reversed without closing it first."""
    role = (reversed_role or "").strip()
    if role not in CLOSER_ROLES:
        raise ClockPermissionError(f"role {role!r} may not reverse a stop clock; only {list(CLOSER_ROLES)}")
    if ev.incident_id != inc.id:
        raise ValueError("clock event does not belong to this incident")
    text = (reason or "").strip()
    if not text:
        raise ValueError("reversal reason is required")
    if ev.reversed_at is not None:
        raise ClockStateError("clock event is already reversed")
    at = now or utcnow()
    ev.reversed_at = at
    ev.reversed_by = (reversed_by or "").strip() or role
    ev.reversal_reason = text
    inc.updated_at = at
    session.flush()
    _audit(session, inc, actor=ev.reversed_by, action="clock.reversed", ev=ev, rationale=text)
    _announce(session, inc, ev, "clock.reversed")
    return ev


def list_clock_events(session: Session, incident_id: str) -> list[ClockEventRow]:
    """Every event on the incident, reversed ones included, oldest condition first.
    Not operator-scoped by itself: callers reach the incident through ``_get_owned``
    first, or are the scorecard job iterating its own operator's incidents."""
    return list(
        session.scalars(
            select(ClockEventRow)
            .where(ClockEventRow.incident_id == incident_id)
            .order_by(ClockEventRow.started_at, ClockEventRow.created_at)
        )
    )


# ------------------------------------------------------------------ arithmetic (pure)


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


def _clip(start: datetime, end: datetime, window_start: datetime, window_end: datetime) -> Interval | None:
    lo = max(start, window_start)
    hi = min(end, window_end)
    return Interval(lo, hi) if hi > lo else None


def effective_intervals(
    events: Iterable[ClockEventRow],
    *,
    window_start: datetime,
    window_end: datetime,
) -> list[Interval]:
    """The de-overlapped, window-clipped intervals that actually deduct.

    * reversed events are skipped entirely;
    * an event with no ``ended_at`` is treated as ending at ``window_end`` (bounded by the
      period, never running into the next one);
    * each interval is clipped to ``[window_start, window_end]``;
    * the survivors are merged into a disjoint union, so the sum of their durations is
      exactly the time the clock was stopped and no minute is counted twice, however many
      SCCs covered it. Touching intervals (one ends where the next starts) merge too,
      which changes nothing numerically and keeps the list minimal.
    """
    if window_end <= window_start:
        return []
    clipped: list[Interval] = []
    for ev in events:
        if ev.reversed_at is not None:
            continue
        end = ev.ended_at if ev.ended_at is not None else window_end
        iv = _clip(ev.started_at, end, window_start, window_end)
        if iv is not None:
            clipped.append(iv)
    clipped.sort(key=lambda iv: (iv.start, iv.end))
    merged: list[Interval] = []
    for iv in clipped:
        if merged and iv.start <= merged[-1].end:
            if iv.end > merged[-1].end:
                merged[-1] = Interval(merged[-1].start, iv.end)
            continue
        merged.append(iv)
    return merged


def deducted_timedelta(events: Iterable[ClockEventRow], *, window_start: datetime, window_end: datetime) -> timedelta:
    """Exact stop-clock time inside the window (the union of ``effective_intervals``)."""
    return sum((iv.duration for iv in effective_intervals(events, window_start=window_start, window_end=window_end)), timedelta())


def deducted_minutes(events: Iterable[ClockEventRow], *, window_start: datetime, window_end: datetime) -> int:
    """Whole minutes to deduct from the vendor's restore time (``scc_minutes_deducted``).

    Truncated, not rounded: a partial minute of stop clock is not credited. The exact
    figure is ``deducted_timedelta`` for any caller that keeps seconds; the two agree on
    every whole-minute fixture, which is what contracts and the golden test use.
    """
    return int(deducted_timedelta(events, window_start=window_start, window_end=window_end).total_seconds() // 60)


def scc_breakdown(events: Iterable[ClockEventRow], *, window_start: datetime, window_end: datetime) -> list[dict]:
    """Per-event lines for an evidence pack (``pack_json.scc_breakdown[]``, §7.6.1).

    Each line shows the event's OWN clipped minutes. Because events may overlap, the sum
    of these lines can exceed ``deducted_minutes`` for the same window -- that is the
    point: the pack shows each claim as made, and the total shows what was deducted once.
    A reversed event is listed with ``minutes=0`` and ``reversed=True`` so its withdrawal
    is visible, not hidden.
    """
    lines: list[dict] = []
    for ev in sorted(events, key=lambda e: (e.started_at, e.created_at)):
        reversed_ = ev.reversed_at is not None
        end = ev.ended_at if ev.ended_at is not None else window_end
        iv = None if reversed_ else _clip(ev.started_at, end, window_start, window_end)
        lines.append(
            {
                "event_id": ev.id,
                "scc_code": ev.scc_code,
                "started_at": ev.started_at,
                "ended_at": ev.ended_at,
                "open": ev.ended_at is None and not reversed_,
                "reversed": reversed_,
                "effective_start": iv.start if iv else None,
                "effective_end": iv.end if iv else None,
                "minutes": int(iv.duration.total_seconds() // 60) if iv else 0,
                "opening_delay_min": opening_delay_minutes(ev),
            }
        )
    return lines


def opening_delay_minutes(ev: ClockEventRow) -> int:
    """§7.6.2 discipline counter for one event: how late OUR NOC recorded it.

    ``opened_at - started_at`` in whole minutes, floored at zero (a client is allowed one
    minute of skew on ``started_at``, which could otherwise read as -1). This number is
    about the operator, never the vendor, and nothing here or downstream may let it
    improve a vendor's figure: it does not touch the deduction at all.
    """
    delta = (ev.opened_at - ev.started_at).total_seconds()
    return max(0, int(delta // 60))


def late_openings(events: Iterable[ClockEventRow], *, threshold_min: int = LATE_OPENING_THRESHOLD_MIN) -> list[ClockEventRow]:
    """Events recorded more than ``threshold_min`` after they started (reversed ones
    included -- a late record is a late record even if later withdrawn)."""
    return [ev for ev in events if opening_delay_minutes(ev) > threshold_min]


# ------------------------------------------------------------------ incident helpers


def incident_window(inc: IncidentRow, *, now: datetime | None = None) -> tuple[datetime, datetime]:
    """The outage window a stop clock can deduct from: ``[failure_time, restored_at]``.

    Falls back to ``outage_start_at`` then ``created_at`` for the start, and to now for an
    incident that is not restored yet -- so the workspace can show a live "deducted so far".
    The scorecard passes its own window (the period), not this one.
    """
    start = inc.failure_time or inc.outage_start_at or inc.created_at or (now or utcnow())
    end = inc.restored_at or (now or utcnow())
    return start, end


def deducted_minutes_for_incident(
    session: Session,
    inc: IncidentRow,
    *,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    now: datetime | None = None,
) -> int:
    """Convenience for the scorecard and the evidence pack: the incident's events, deducted
    over the given window (default: the incident's own outage window)."""
    start, end = incident_window(inc, now=now)
    return deducted_minutes(
        list_clock_events(session, inc.id),
        window_start=window_start or start,
        window_end=window_end or end,
    )


def clock_event_out(ev: ClockEventRow) -> dict:
    """Wire shape of one event; every timestamp Z-stamped (§7.0.6)."""
    return {
        "id": ev.id,
        "incident_id": ev.incident_id,
        "scc_code": ev.scc_code,
        "started_at": z_utc(ev.started_at),
        "ended_at": z_utc(ev.ended_at),
        "open": ev.is_open,
        "opened_by": ev.opened_by,
        "opened_role": ev.opened_role,
        "opened_at": z_utc(ev.opened_at),
        "opening_delay_min": opening_delay_minutes(ev),
        "late": opening_delay_minutes(ev) > LATE_OPENING_THRESHOLD_MIN,
        "reason": ev.reason,
        "evidence_note_id": ev.evidence_note_id,
        "reversed": ev.is_reversed,
        "reversed_at": z_utc(ev.reversed_at),
        "reversed_by": ev.reversed_by,
        "reversal_reason": ev.reversal_reason,
        "created_at": z_utc(ev.created_at),
    }
