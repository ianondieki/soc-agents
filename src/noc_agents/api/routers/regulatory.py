"""Regulatory clock and evidence-pack routes (spec §7.6.3) — Phase 4 Lane 4A.

Six routes, and the shape of them is the point:

* three **reads** (``GET /incidents/{id}/regulatory``, ``GET /incidents/{id}/evidence-pack``)
  that a workspace can call on every load;
* three **writes** that each take the notice exactly one step further (open → draft →
  request approval), and
* exactly one route that can put a regulator notice into the outbox
  (``POST /regulatory/{id}/send``), which delegates the decision to
  ``services.regulatory.release_notice`` and adds nothing of its own.

The approval itself deliberately does **not** live here. It goes through the existing
``POST /api/v1/hitl/{task_id}/approve`` in ``main.py``, which for a task type it does not
recognise records the decision and releases nothing — so approving the card is a pure
"a named human said yes", and the send is a separate, separately-authorised act by a
supervisor who must also account for any lateness (DPA 2019 s.43). Two acts, two rows in
``audit_events``, and no single button that both approves and transmits to a regulator.

``REGULATORY_ENABLED`` defaults to **false**. Reads then answer 200 with ``enabled: false``
and nothing in them — the same shape the memory panel uses, and for the same reason: an
empty countdown because the feature is off must not look like an incident with no obligation.
Writes answer **503**: the feature is not available on this deployment, which is a different
thing from "you may not" (403) and from "no such incident" (404).

Operator scoping is in the WHERE clause on every read (``_owned`` / ``_get_owned``), and an
id belonging to another operator is a 404, never a 403 — see ``api/deps.py``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from noc_agents.api import auth
from noc_agents.api.auth import require_role
from noc_agents.api.deps import AUDIT_READERS, READERS, SUPERVISORS, _actor, _get_owned, _settings
from noc_agents.db.models import IncidentRow, get_session
from noc_agents.db.models_regulatory import EvidencePackRow, RegulatoryNotificationRow
from noc_agents.realtime.commit_hook import buffer_event
from noc_agents.realtime.hub import RealtimeEvent
from noc_agents.services.clock import z_utc
from noc_agents.services.evidence import get_or_build_pack
from noc_agents.services.regulatory import (
    REGULATORY_KINDS,
    REGULATORY_TASK_TYPE,
    ClockStartUnknown,
    NoticeNotApproved,
    NoticeStateError,
    countdown,
    draft_for,
    evaluate_significance,
    incident_notifications,
    open_notification,
    regulatory_enabled,
    release_notice,
    request_approval,
)

router = APIRouter(prefix="/api/v1", tags=["regulatory"])

#: Who may read an evidence pack. The audit readers (the regulator-facing surface, §7.0.5)
#: plus the supervisors who have to draft and send the notice the pack backs — asking a duty
#: manager to send a notice whose evidence they may not open would be an odd rule to defend.
EVIDENCE_READERS: tuple[str, ...] = tuple(dict.fromkeys(SUPERVISORS + AUDIT_READERS))

_DISABLED = "regulatory notifications are disabled on this deployment (REGULATORY_ENABLED=false)"


def _require_enabled() -> None:
    """503 while the lane is off. Not 404 (the route exists) and not 403 (the caller is fine)."""
    if not regulatory_enabled():
        raise HTTPException(503, _DISABLED)


# ------------------------------------------------------------------------------ request bodies


class OpenNoticeBody(BaseModel):
    """``POST /incidents/{id}/regulatory``. ``kind`` defaults to the CA 24-hour clock."""

    kind: str = "CA_OUTAGE_24H"
    requested_by: str | None = None


class ApprovalRequestBody(BaseModel):
    requested_by: str | None = None


class SendNoticeBody(BaseModel):
    """``POST /regulatory/{id}/send``.

    ``reason_for_delay`` is optional in the schema and **mandatory in fact** once the deadline
    has passed (DPA 2019 s.43; §9.2). It is not a required field here because the common case
    is an on-time send with nothing to explain, and a required-but-usually-empty field is the
    kind that gets filled with "n/a". ``release_notice`` refuses the late send instead.
    """

    sent_by: str | None = None
    reason_for_delay: str | None = Field(default=None, max_length=2000)
    external_ref: str | None = Field(default=None, max_length=256)


# ----------------------------------------------------------------------------- serialisation


def _notice_out(notice: RegulatoryNotificationRow) -> dict[str, Any]:
    """One notification as the workspace reads it, countdown included.

    Every timestamp is stamped with ``Z`` (``z_utc``) and the countdown carries the EAT
    spelling beside it: the clock is statutory, the reader is in Nairobi, and a naive ISO
    string would be read as EAT and make a 24-hour deadline look three hours further away.
    """
    return {
        "id": notice.id,
        "kind": notice.kind,
        "status": notice.status,
        "incident_id": notice.incident_id,
        "clock_started_at": z_utc(notice.clock_started_at),
        "due_at": z_utc(notice.due_at),
        "approved_by": notice.approved_by,
        "approved_at": z_utc(notice.approved_at),
        "sent_at": z_utc(notice.sent_at),
        "external_ref": notice.external_ref,
        "hitl_task_id": notice.hitl_task_id,
        "evidence_pack_id": notice.evidence_pack_id,
        "significance": notice.significance,
        "draft": notice.draft_alert,
        "countdown": countdown(notice),
        "created_at": z_utc(notice.created_at),
    }


def _pack_out(row: EvidencePackRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "incident_id": row.incident_id,
        "generated_at": z_utc(row.generated_at),
        "generated_by": row.generated_by,
        "sha256": row.sha256,
        "pack": row.pack,
    }


# ------------------------------------------------------------------------------------- reads


@router.get("/incidents/{incident_id}/regulatory", dependencies=[Depends(require_role(*READERS))])
def get_incident_regulatory(incident_id: str) -> dict[str, Any]:
    """The incident's regulatory clocks and their countdowns — the workspace panel (§5.3.20).

    Read-only and inert: calling it opens no clock and raises no card. Opening a clock is a
    decision (§5.3.20 autonomy A2), and a decision must not be a side effect of rendering a
    page. ``POST`` to the same path is how a supervisor takes it.
    """
    enabled = regulatory_enabled()
    session = get_session()
    try:
        inc = _get_owned(session, IncidentRow, incident_id, what="incident")
        notices = incident_notifications(session, inc.id) if enabled else []
        return {
            "incident_id": inc.id,
            "incident_number": inc.incident_number,
            "enabled": enabled,
            "notifications": [_notice_out(n) for n in notices],
            # The verdict is shown even with nothing opened yet, so a supervisor can see WHY
            # the rule would or would not bite before deciding to open a clock. Evaluating is
            # a pure read (services/regulatory.evaluate_significance writes nothing).
            "significance": evaluate_significance(inc, _settings().operator, session=session).as_json()
            if enabled
            else None,
            "kinds": list(REGULATORY_KINDS),
            # Same honesty rule as the memory panel's ``degraded``: an empty list because the
            # flag is off must not read as "this outage has no regulatory obligation".
            "degraded": not enabled,
        }
    finally:
        session.close()


@router.get("/incidents/{incident_id}/evidence-pack", dependencies=[Depends(require_role(*EVIDENCE_READERS))])
def get_evidence_pack(incident_id: str, refresh: bool = Query(False)) -> dict[str, Any]:
    """Generate or return the incident's evidence pack (§7.6.3); the hash is stable.

    Available with the lane's flag off, on purpose: an evidence pack is a read-only statement
    of facts that already exist in the database, it notifies nobody, and being able to produce
    one for a vendor dispute should not depend on whether regulatory notifications are armed.

    ``refresh=true`` rebuilds from the current rows and appends a new row **only if the
    content actually changed** — so repeated refreshes of an unchanged incident return the
    same id and the same sha256 (``services/evidence.get_or_build_pack``). The table is
    append-only: an older pack is never rewritten, because its whole value is that the bytes
    behind its hash cannot have moved.
    """
    session = get_session()
    try:
        inc = _get_owned(session, IncidentRow, incident_id, what="incident")
        actor = "api:evidence-pack"
        row, created = get_or_build_pack(session, inc, generated_by=actor, refresh=refresh)
        session.commit()
        return {**_pack_out(row), "created": created}
    finally:
        session.close()


# ------------------------------------------------------------------------------------ writes


@router.post("/incidents/{incident_id}/regulatory", dependencies=[Depends(require_role(*SUPERVISORS))])
def open_incident_regulatory(
    incident_id: str,
    body: OpenNoticeBody,
    principal: auth.Principal = Depends(require_role(*SUPERVISORS)),
) -> dict[str, Any]:
    """Evaluate significance and open the clock (or record NOT_REQUIRED). Idempotent.

    A P4 that matches no rule produces a ``NOT_REQUIRED`` row rather than nothing (§7.6.8):
    "we considered it and it is not notifiable" has to be distinguishable from "nobody
    looked". Re-posting returns the existing row untouched — the clock is never restarted.
    """
    _require_enabled()
    actor = _actor(principal, body.requested_by)
    session = get_session()
    try:
        inc = _get_owned(session, IncidentRow, incident_id, what="incident")
        try:
            notice = open_notification(session, inc, _settings().operator, kind=body.kind, actor=actor)
        except ClockStartUnknown as exc:
            # 422, not 400: the request is well-formed, the incident is not ready. The clock
            # starts at the failure, so the fix is to record failure_time — never to let this
            # route invent a deadline from row-creation time.
            raise HTTPException(422, str(exc)) from exc
        except ValueError as exc:  # unknown kind, or a kind with no configured deadline
            raise HTTPException(400, str(exc)) from exc
        session.commit()
        return _notice_out(notice)
    finally:
        session.close()


@router.post("/regulatory/{notification_id}/draft", dependencies=[Depends(require_role(*SUPERVISORS))])
def redraft_notice(notification_id: str) -> dict[str, Any]:
    """Regenerate the draft text from the incident as it stands now (§7.6.3).

    Allowed only while the notice is open. A SENT notice keeps the wording that was actually
    released — rewriting it would destroy the only record of what the regulator was told —
    and a NOT_REQUIRED notice has nothing to draft.
    """
    _require_enabled()
    session = get_session()
    try:
        notice = _get_owned(session, RegulatoryNotificationRow, notification_id, what="notification")
        if not notice.is_open:
            raise HTTPException(409, f"notification is {notice.status}; only an open notice can be redrafted")
        inc = _get_owned(session, IncidentRow, notice.incident_id, what="incident")
        draft_for(session, notice, inc, _settings().operator)
        session.commit()
        return _notice_out(notice)
    finally:
        session.close()


@router.post("/regulatory/{notification_id}/request-approval", dependencies=[Depends(require_role(*SUPERVISORS))])
def request_notice_approval(
    notification_id: str,
    body: ApprovalRequestBody,
    principal: auth.Principal = Depends(require_role(*SUPERVISORS)),
) -> dict[str, Any]:
    """Raise ``APPROVE_REGULATORY_NOTICE`` and attach the evidence pack. Queues nothing.

    No outbox row exists for this notice until :func:`send_notice` runs, so there is nothing
    for any generic release path to promote (see ``services/regulatory`` rule 2).
    """
    _require_enabled()
    actor = _actor(principal, body.requested_by)
    session = get_session()
    try:
        notice = _get_owned(session, RegulatoryNotificationRow, notification_id, what="notification")
        inc = _get_owned(session, IncidentRow, notice.incident_id, what="incident")
        try:
            task = request_approval(session, notice, inc, _settings().operator, actor=actor)
        except NoticeStateError as exc:
            raise HTTPException(409, str(exc)) from exc
        buffer_event(
            session,
            RealtimeEvent(
                type="hitl.created",
                operator_id=inc.operator_id,
                incident_id=inc.id,
                payload={
                    "task_id": task.id,
                    "task_type": REGULATORY_TASK_TYPE,
                    "notification_id": notice.id,
                    "kind": notice.kind,
                    "incident_number": inc.incident_number,
                    "due_at": z_utc(notice.due_at).isoformat(),
                },
            ),
        )
        session.commit()  # the buffered event leaves the process here, never before
        return {"task_id": task.id, "notification": _notice_out(notice)}
    finally:
        session.close()


@router.post("/regulatory/{notification_id}/send", dependencies=[Depends(require_role(*SUPERVISORS))])
def send_notice(
    notification_id: str,
    body: SendNoticeBody,
    principal: auth.Principal = Depends(require_role(*SUPERVISORS)),
) -> dict[str, Any]:
    """Release an APPROVED notice to the outbox. The only route that can (M10).

    It decides nothing itself: ``release_notice`` re-reads the ``APPROVE_REGULATORY_NOTICE``
    task from the database and refuses unless a named human approved *this* notice. A missing
    or invalid approval is a **403**, not a 409 — the caller is being told they are not
    permitted to send, which is exactly what has happened.

    Nothing is transmitted here. The row is PENDING in the outbox and the dispatcher takes it
    on its own pass, so the mail cannot leave inside this request's transaction.
    """
    _require_enabled()
    actor = _actor(principal, body.sent_by)
    session = get_session()
    try:
        notice = _get_owned(session, RegulatoryNotificationRow, notification_id, what="notification")
        inc = _get_owned(session, IncidentRow, notice.incident_id, what="incident")
        try:
            row = release_notice(
                session,
                notice,
                inc,
                actor=actor,
                reason_for_delay=body.reason_for_delay,
                external_ref=body.external_ref,
            )
        except NoticeNotApproved as exc:
            raise HTTPException(403, str(exc)) from exc
        except NoticeStateError as exc:
            raise HTTPException(409, str(exc)) from exc
        session.commit()
        return {
            "ok": True,
            "outbox_id": row.id,
            "outbox_status": row.status,
            "notification": _notice_out(notice),
        }
    finally:
        session.close()
