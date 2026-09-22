"""Planned-maintenance routes (spec §7.5.2; ``MAINTENANCE_ENABLED=false``) — Phase 5 Lane 5A.

    GET  /api/v1/maintenance/plans                         list plans
    POST /api/v1/maintenance/plans                         create one            (planning+)
    GET  /api/v1/maintenance/tasks?status=&region=&due_before=
    POST /api/v1/maintenance/tasks/{id}/complete           record the work done  (operations)
    GET  /api/v1/maintenance/windows                       list windows
    POST /api/v1/maintenance/windows                       create one (always PROPOSED)
    POST /api/v1/maintenance/windows/{id}/cancel           METHOD:CANCEL side    (supervisors)

plus seven routes §7.5.2 does not list, each of which exists because the shipped data model is
otherwise unreachable — the same kind of reasoned deviation ``routers/pir.py`` documents for
``PATCH /pir/{id}/actions/{action_id}``:

    GET  /api/v1/maintenance/windows/{id}                   one window, live rain verdict, clashes
    POST /api/v1/maintenance/tasks/{id}/request-approval    raise APPROVE_SCHEDULE
    POST /api/v1/maintenance/tasks/{id}/schedule            PROPOSED -> SCHEDULED, behind that gate
    POST /api/v1/maintenance/windows/{id}/request-approval  raise APPROVE_MAINTENANCE_WINDOW
    POST /api/v1/maintenance/windows/{id}/schedule          PROPOSED -> SCHEDULED, behind THAT gate
    POST /api/v1/maintenance/windows/{id}/notice-sent       stamp customer_notice_sent_at (sends nothing)
    POST /api/v1/maintenance/windows/{id}/tasks/{task_id}   book a task into a window
    GET  /api/v1/maintenance/incidents/{id}/stop-clock-proposal

§7.5.2 names both HITL card types but gives no route that raises either, and §7.5.3 describes
a window moving to SCHEDULED and a task moving to SCHEDULED without saying what performs
either move. Read literally, a window could be created and never approved, which would make
the whole lane inert: the ``APPROVE_MAINTENANCE_WINDOW`` gate would be a card nobody can
raise, and ``maintenance_windows.customer_notice_sent_at`` and ``maintenance_tasks.window_id``
would be columns nothing can ever write. These add no column, no status and no vocabulary —
they are the smallest thing that makes the shipped schema usable.

A ``/reschedule`` route is deliberately **not** offered yet; ``services.maintenance`` has the
function (and the approval-revocation rule that goes with it), but moving a window is a
conversation with engineers and customers before it is a button.

**The flag is a 404, not a 403.** With ``MAINTENANCE_ENABLED`` unset the lane is supposed to
be invisible: "the system behaves exactly as it does today" means today's system, which has
no ``/maintenance`` surface at all. A 403 would announce that the feature exists and the
caller is merely not allowed it, which is a different — and false — statement. The same
choice ``routers/pir.py`` and ``routers/clocks.py`` make.

**Operator scoping (§8).** ``maintenance_plans`` and ``maintenance_windows`` carry
``operator_id``, so ``_owned``/``_get_owned`` filter them directly and answer 404 (never 403)
for another operator's row. ``maintenance_tasks`` carries none: every read of it goes through
``services.maintenance.owned_tasks``, which joins the plan and puts the operator clause in the
WHERE. There is no ``_owned(MaintenanceTaskRow)`` and there must not be — ``api.deps._owned``
would fall back to a column that does not exist.

**Approving happens on the existing HITL surface**, ``POST /api/v1/hitl/{task_id}/approve``.
This module does not duplicate it: raiser≠approver, the supervisor role check and the audit
trail are already there, and a second approve route for one lane is how a gate ends up with
two sets of rules. What this module does is refuse to act until that approval exists and
verifies (``services.maintenance.window_approval_of``).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from noc_agents.api import auth
from noc_agents.api.auth import require_role
from noc_agents.api.deps import (
    INCIDENT_READERS,
    OPERATIONS,
    READERS,
    SUPERVISORS,
    _actor,
    _get_owned,
    _owned,
    _settings,
)
from noc_agents.db.models import AuditRow, IncidentRow, get_session, utcnow
from noc_agents.db.models_maintenance import (
    TASK_PROPOSED,
    TASK_SCHEDULED,
    WINDOW_PROPOSED,
    WINDOW_SCHEDULED,
    MaintenancePlanRow,
    MaintenanceTaskRow,
    MaintenanceWindowRow,
)
from noc_agents.services import maintenance as svc
from noc_agents.services import sites as site_catalogue

UTC = timezone.utc

router = APIRouter(prefix="/api/v1", tags=["maintenance"])

#: Who may write a maintenance policy or move a window. Planning owns the programme and
#: supervisors own the night; ``deps.py`` has no tuple for that pair because no earlier lane
#: needed one, so it is named here rather than by editing a shared file.
PLANNERS: tuple[str, ...] = ("planning",) + SUPERVISORS


def require_maintenance_enabled() -> None:
    """404 the whole lane while ``MAINTENANCE_ENABLED`` is off (the default)."""
    if not svc.maintenance_enabled():
        raise HTTPException(
            404,
            "planned maintenance is not enabled on this deployment "
            f"({svc.MAINTENANCE_ENABLED_ENV}=false)",
        )


_ENABLED = Depends(require_maintenance_enabled)


# ------------------------------------------------------------------------------- bodies


class PlanIn(BaseModel):
    """Exactly one of ``site_id`` / ``site_class``; ``standard_ref`` is mandatory (§7.5.1)."""

    task_type: str
    standard_ref: str
    site_id: str | None = None
    site_class: str | None = None
    interval_days: int | None = None
    interval_hours: int | None = None
    consumption_driven: bool = False
    owner_vendor_id: str | None = None
    active: bool = True


class WindowIn(BaseModel):
    """``starts_at``/``ends_at`` are instants. Send them with an offset or as UTC —
    a naive value is taken as UTC, which is the storage contract (§7.0.6)."""

    scope: str
    scope_ref: str
    starts_at: datetime
    ends_at: datetime
    rrule: str | None = None
    organizer: str | None = None
    attendees_ref: str | None = None
    ca_approval_ref: str | None = None
    incident_id: str | None = None


class CompleteIn(BaseModel):
    outcome: str
    completed_by: str | None = None
    evidence_note: str | None = None
    completed_at: datetime | None = None


class ReasonIn(BaseModel):
    reason: str


class ScheduleWindowIn(BaseModel):
    """``override_rain`` is the named-human escape from a *storm* verdict, and it needs a
    reason. It cannot be set by any agent: this body only ever arrives on a request."""

    override_rain: bool = False
    override_reason: str | None = None


class NoticeIn(BaseModel):
    """Recording that the customer notice went out. The notice itself is a broadcast and
    leaves through §6's own approval path; this only stamps when it did."""

    sent_at: datetime | None = None


# ------------------------------------------------------------------------------ helpers


def _audit(session, *, actor: str, action: str, entity_type: str, entity_id: str, rationale: str, payload: dict) -> None:
    session.add(
        AuditRow(
            operator_id=_settings().operator.operator_id,
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            rationale=rationale,
            payload_json=json.dumps(payload, default=str),
        )
    )


def _owned_task(session, task_id: str) -> tuple[MaintenanceTaskRow, MaintenancePlanRow]:
    """One task and its plan, scoped to the active operator, or 404.

    The operator clause is the plan join inside ``owned_tasks`` — a WHERE, not a check after
    the read — so another operator's task id is indistinguishable from a nonexistent one.
    """
    task = session.scalar(svc.owned_tasks(session, _settings().operator.operator_id).where(MaintenanceTaskRow.id == task_id))
    if task is None:
        raise HTTPException(404, "maintenance task not found")
    plan = _get_owned(session, MaintenancePlanRow, task.plan_id, what="maintenance plan")
    return task, plan


def _cfg():
    return _settings().operator


def _task_detail(session, task: MaintenanceTaskRow, plan: MaintenancePlanRow | None) -> dict[str, Any]:
    body = svc.task_out(task, plan=plan)
    site = site_catalogue.lookup_site(task.site_id)
    body["region_code"] = site.region_code if site else None
    body["site_name"] = site.site_name if site else None
    return body


# -------------------------------------------------------------------------------- reads


@router.get("/maintenance/plans", dependencies=[_ENABLED, Depends(require_role(*READERS))])
def list_plans(active: bool | None = None, task_type: str | None = None, limit: int = 200) -> list[dict]:
    session = get_session()
    try:
        stmt = _owned(MaintenancePlanRow)
        if active is not None:
            stmt = stmt.where(MaintenancePlanRow.active == (1 if active else 0))
        if task_type:
            stmt = stmt.where(MaintenancePlanRow.task_type == task_type.strip().upper())
        rows = session.scalars(stmt.order_by(MaintenancePlanRow.created_at.desc()).limit(limit)).all()
        return [svc.plan_out(p) for p in rows]
    finally:
        session.close()


@router.get("/maintenance/tasks", dependencies=[_ENABLED, Depends(require_role(*READERS))])
def list_tasks(
    status: str | None = None,
    region: str | None = None,
    due_before: datetime | None = None,
    limit: int = 200,
) -> list[dict]:
    """§7.5.2's filtered task list. ``region`` is resolved through the site catalogue.

    ``region`` cannot be a SQL predicate: ``maintenance_tasks`` stores a site id and the
    region lives in the catalogue file, not in the database. So the region filter is applied
    in Python over the already operator-scoped rows — the *scoping* stays in the WHERE clause,
    which is the property that matters (§8); only the cosmetic filter is applied after.
    """
    session = get_session()
    try:
        operator_id = _settings().operator.operator_id
        stmt = svc.owned_tasks(session, operator_id)
        if status:
            stmt = stmt.where(MaintenanceTaskRow.status == status.strip().upper())
        if due_before is not None:
            # Naive UTC is the storage contract (§7.0.6): an aware bound is converted, never
            # compared against naive column values (SQLite would raise, or worse, compare text).
            bound = due_before if due_before.tzinfo is None else due_before.astimezone(UTC).replace(tzinfo=None)
            stmt = stmt.where(MaintenanceTaskRow.due_at < bound)
        rows = session.scalars(stmt.order_by(MaintenanceTaskRow.due_at.asc()).limit(limit)).all()
        plans = {p.id: p for p in session.scalars(_owned(MaintenancePlanRow)).all()}
        want = (region or "").strip().upper()
        out = []
        for task in rows:
            site = site_catalogue.lookup_site(task.site_id)
            if want and (not site or (site.region_code or "").upper() != want):
                continue
            out.append(_task_detail(session, task, plans.get(task.plan_id)))
        return out
    finally:
        session.close()


@router.get("/maintenance/windows", dependencies=[_ENABLED, Depends(require_role(*READERS))])
def list_windows(status: str | None = None, scope: str | None = None, limit: int = 200) -> list[dict]:
    session = get_session()
    try:
        stmt = _owned(MaintenanceWindowRow)
        if status:
            stmt = stmt.where(MaintenanceWindowRow.status == status.strip().upper())
        if scope:
            stmt = stmt.where(MaintenanceWindowRow.scope == scope.strip().upper())
        rows = session.scalars(stmt.order_by(MaintenanceWindowRow.starts_at.desc()).limit(limit)).all()
        out = []
        for window in rows:
            body = svc.window_out(window)
            body["tasks"] = [svc.task_out(t) for t in svc.tasks_for_window(session, window)]
            out.append(body)
        return out
    finally:
        session.close()


@router.get("/maintenance/windows/{window_id}", dependencies=[_ENABLED, Depends(require_role(*READERS))])
def get_window(window_id: str) -> dict:
    """One window, with its tasks, the live rain verdict and any SCHEDULED clash.

    The rain verdict is recomputed on read rather than served from ``rain_season_flag``: the
    stored flag is what the guard concluded when the card was raised, and the question a human
    opening this page is asking is about tonight. Both are returned, labelled.
    """
    session = get_session()
    try:
        window = _get_owned(session, MaintenanceWindowRow, window_id, what="maintenance window")
        body = svc.window_out(window)
        body["tasks"] = [svc.task_out(t) for t in svc.tasks_for_window(session, window)]
        body["rain_guard"] = svc.rain_guard(session, window, _cfg()).as_json()
        body["ics"] = svc.window_ics_fields(session, window, _cfg())
        body["overlaps"] = [
            {"id": w.id, "status": w.status, "scope": w.scope, "scope_ref": w.scope_ref}
            for w in svc.overlapping_windows(
                session,
                window.operator_id,
                scope=window.scope,
                scope_ref=window.scope_ref,
                starts_at=window.starts_at,
                ends_at=window.ends_at,
                statuses=(WINDOW_PROPOSED, WINDOW_SCHEDULED),
                exclude_id=window.id,
            )
        ]
        return body
    finally:
        session.close()


@router.get(
    "/maintenance/incidents/{incident_id}/stop-clock-proposal",
    # An incident-scoped read beside /incidents/{id}/clock: §9.3 row 1, INCIDENT_READERS.
    dependencies=[_ENABLED, Depends(require_role(*INCIDENT_READERS))],
)
def stop_clock_proposal(incident_id: str) -> dict:
    """The ``PLANNED_MAINTENANCE`` stop-clock **proposal** for one incident, or ``null``.

    Read-only by construction and by intent. There is no POST beside it: accepting a proposal
    is ``POST /api/v1/incidents/{id}/clock``, the existing route in ``routers/clocks.py``,
    which demands an operations role, a reason and a named actor, and records how late the
    operator was in opening it. A stop clock deducts minutes from a vendor's SLA figure, which
    is a commercial act — this lane proposes, a person decides. See
    ``services.maintenance.stop_clock_proposal``.
    """
    session = get_session()
    try:
        inc = _get_owned(session, IncidentRow, incident_id, what="incident")
        return {"proposal": svc.stop_clock_proposal(session, inc)}
    finally:
        session.close()


# ------------------------------------------------------------------------------- writes


@router.post("/maintenance/plans", dependencies=[_ENABLED])
def create_plan(body: PlanIn, principal: auth.Principal = Depends(require_role(*PLANNERS))) -> dict:
    actor = _actor(principal, None)
    session = get_session()
    try:
        try:
            plan = svc.create_plan(session, _cfg(), body.model_dump(), actor=actor)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        session.commit()
        return svc.plan_out(plan)
    finally:
        session.close()


@router.post("/maintenance/tasks/{task_id}/complete", dependencies=[_ENABLED])
def complete_task(task_id: str, body: CompleteIn, principal: auth.Principal = Depends(require_role(*OPERATIONS))) -> dict:
    """Record that the work was done. ``outcome`` is mandatory — see the service for why.

    The completion is what the *next* due date is computed from, so it is the one field the
    whole plan→task cycle depends on being true.
    """
    who = _actor(principal, body.completed_by)
    session = get_session()
    try:
        task, plan = _owned_task(session, task_id)
        try:
            svc.complete_task(
                session,
                task,
                completed_by=who,
                outcome=body.outcome,
                evidence_note=body.evidence_note,
                completed_at=body.completed_at,
            )
        except svc.MaintenanceStateError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        session.commit()
        return _task_detail(session, task, plan)
    finally:
        session.close()


@router.post("/maintenance/tasks/{task_id}/request-approval", dependencies=[_ENABLED])
def request_schedule_approval(task_id: str, principal: auth.Principal = Depends(require_role(*PLANNERS))) -> dict:
    """Raise the ``APPROVE_SCHEDULE`` card for one task. Queues nothing (see the service)."""
    actor = _actor(principal, None)
    session = get_session()
    try:
        task, plan = _owned_task(session, task_id)
        try:
            card = svc.request_schedule_approval(session, task, plan, _cfg(), actor=actor)
        except svc.MaintenanceStateError as exc:
            raise HTTPException(409, str(exc)) from exc
        except svc.NoAnchorIncident as exc:
            # 503, not 500: the request was valid and the system is temporarily unable to
            # raise a card. The message says exactly why, because "try again later" is useless
            # advice for a condition that only an incident (or a schema change) resolves.
            raise HTTPException(503, str(exc)) from exc
        session.commit()
        return {"ok": True, "hitl_task_id": card.id, "task": svc.task_out(task, plan=plan)}
    finally:
        session.close()


@router.post("/maintenance/tasks/{task_id}/schedule", dependencies=[_ENABLED])
def schedule_task(task_id: str, principal: auth.Principal = Depends(require_role(*PLANNERS))) -> dict:
    """PROPOSED → SCHEDULED, only behind an APPROVED ``APPROVE_SCHEDULE`` card."""
    actor = _actor(principal, None)
    session = get_session()
    try:
        task, plan = _owned_task(session, task_id)
        try:
            svc.mark_scheduled(session, task, actor=actor)
        except svc.ScheduleNotApproved as exc:
            raise HTTPException(403, str(exc)) from exc
        except svc.MaintenanceStateError as exc:
            raise HTTPException(409, str(exc)) from exc
        session.commit()
        return _task_detail(session, task, plan)
    finally:
        session.close()


@router.post("/maintenance/windows", dependencies=[_ENABLED])
def create_window(body: WindowIn, principal: auth.Principal = Depends(require_role(*PLANNERS))) -> dict:
    """Create a window. Always PROPOSED, whatever the caller asks for — there is no route from
    "created" to "customers off air" that does not pass the approval gate."""
    actor = _actor(principal, None)
    session = get_session()
    try:
        try:
            window = svc.create_window(session, _cfg(), body.model_dump(), actor=actor)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        session.commit()
        out = svc.window_out(window)
        out["rain_guard"] = svc.rain_guard(session, window, _cfg()).as_json()
        return out
    finally:
        session.close()


@router.post("/maintenance/windows/{window_id}/request-approval", dependencies=[_ENABLED])
def request_window_approval(window_id: str, principal: auth.Principal = Depends(require_role(*PLANNERS))) -> dict:
    """Raise the ``APPROVE_MAINTENANCE_WINDOW`` card. The rain verdict goes on the card."""
    actor = _actor(principal, None)
    session = get_session()
    try:
        window = _get_owned(session, MaintenanceWindowRow, window_id, what="maintenance window")
        try:
            card = svc.request_window_approval(session, window, _cfg(), actor=actor)
        except svc.MaintenanceStateError as exc:
            raise HTTPException(409, str(exc)) from exc
        except svc.NoAnchorIncident as exc:
            raise HTTPException(503, str(exc)) from exc
        payload = card.proposed_payload
        session.commit()
        return {"ok": True, "hitl_task_id": card.id, "rain_guard": payload.get("rain_guard")}
    finally:
        session.close()


@router.post("/maintenance/windows/{window_id}/schedule", dependencies=[_ENABLED])
def schedule_window(
    window_id: str,
    body: ScheduleWindowIn,
    principal: auth.Principal = Depends(require_role(*SUPERVISORS)),
) -> dict:
    """PROPOSED → SCHEDULED: the moment this window may take live customers off air.

    Every refusal below is a different question and gets its own status code, because "403"
    with no distinction between "nobody approved this", "the Authority has not written back"
    and "there is a storm coming" is an error message nobody can act on:

    * 403 — no valid ``APPROVE_MAINTENANCE_WINDOW`` approval. **An approved
      ``APPROVE_SCHEDULE`` on every task in the window does not count**; that card signs off
      the programme, not the night.
    * 403 — a REGION/NETWORK window with no ``ca_approval_ref`` (licence Condition 9.1).
    * 409 — a fresh forecast says storm. Overridable with ``override_rain`` + a reason, which
      is audited. "No forecast" does not refuse — see ``services.maintenance.rain_guard``.
    * 409 — another SCHEDULED window already covers an intersecting scope and period.
    """
    actor = _actor(principal, None)
    session = get_session()
    try:
        window = _get_owned(session, MaintenanceWindowRow, window_id, what="maintenance window")
        try:
            svc.schedule_window(
                session,
                window,
                _cfg(),
                actor=actor,
                override_rain=body.override_rain,
                override_reason=body.override_reason or "",
            )
        except (svc.WindowNotApproved, svc.CaApprovalRequired) as exc:
            raise HTTPException(403, str(exc)) from exc
        except (svc.RainGuardBlocked, svc.WindowOverlapError, svc.MaintenanceStateError) as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        session.commit()
        return svc.window_out(window)
    finally:
        session.close()


@router.post("/maintenance/windows/{window_id}/cancel", dependencies=[_ENABLED])
def cancel_window(window_id: str, body: ReasonIn, principal: auth.Principal = Depends(require_role(*SUPERVISORS))) -> dict:
    """Cancel the window and everything booked into it. ``SEQUENCE`` advances so the ICS lane's
    ``METHOD:CANCEL`` is not entitled to be ignored by the recipients' calendars."""
    actor = _actor(principal, None)
    session = get_session()
    try:
        window = _get_owned(session, MaintenanceWindowRow, window_id, what="maintenance window")
        try:
            svc.cancel_window(session, window, actor=actor, reason=body.reason)
        except svc.MaintenanceStateError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        session.commit()
        return svc.window_out(window)
    finally:
        session.close()


@router.post("/maintenance/windows/{window_id}/notice-sent", dependencies=[_ENABLED])
def record_notice_sent(
    window_id: str,
    body: NoticeIn,
    principal: auth.Principal = Depends(require_role(*SUPERVISORS)),
) -> dict:
    """Stamp ``customer_notice_sent_at``. Sends nothing.

    The customer notice is a broadcast: it is drafted, validated and approved on §6's own path
    and leaves through the outbox. This route records the fact that it went, so the window
    approver can see it on the card and the ``notice_days`` policy is checkable — it does not
    and must not become a second way to message customers.
    """
    actor = _actor(principal, None)
    session = get_session()
    try:
        window = _get_owned(session, MaintenanceWindowRow, window_id, what="maintenance window")
        window.customer_notice_sent_at = body.sent_at or utcnow()
        _audit(
            session,
            actor=actor,
            action="maintenance.customer_notice_recorded",
            entity_type="maintenance_window",
            entity_id=window.id,
            rationale="customer notice recorded as sent; this route transmits nothing",
            payload={"customer_notice_sent_at": window.customer_notice_sent_at},
        )
        session.commit()
        return svc.window_out(window)
    finally:
        session.close()


@router.post("/maintenance/windows/{window_id}/tasks/{task_id}", dependencies=[_ENABLED])
def book_task_into_window(
    window_id: str,
    task_id: str,
    principal: auth.Principal = Depends(require_role(*PLANNERS)),
) -> dict:
    """Book an existing task into a window. Refused once the window is SCHEDULED.

    Adding work to a window a human has already approved changes what they approved: the
    approver signed off a specific list of jobs, a specific duration and a specific customer
    impact. So a SCHEDULED window is closed to new work (409) — book it into another window,
    or move this one, which revokes the approval on purpose.
    """
    actor = _actor(principal, None)
    session = get_session()
    try:
        window = _get_owned(session, MaintenanceWindowRow, window_id, what="maintenance window")
        task, plan = _owned_task(session, task_id)
        if window.status != WINDOW_PROPOSED:
            raise HTTPException(409, f"window is {window.status}; only a PROPOSED window accepts new tasks")
        if task.status not in (TASK_PROPOSED, TASK_SCHEDULED):
            raise HTTPException(409, f"task is {task.status} and cannot be booked into a window")
        task.window_id = window.id
        _audit(
            session,
            actor=actor,
            action="maintenance.task_booked",
            entity_type="maintenance_task",
            entity_id=task.id,
            rationale=f"booked into window {window.uid}",
            payload={"window_id": window.id, "site_id": task.site_id},
        )
        session.commit()
        return _task_detail(session, task, plan)
    finally:
        session.close()
