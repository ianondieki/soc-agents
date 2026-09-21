"""Post-incident review and problem-management routes (spec §7.7.2, ``PIR_ENABLED=false``).

Thin transport over ``services/pir.py``: this module parses bodies, applies RBAC, turns the
service's verdicts into status codes and serialises rows. Every rule worth arguing about —
the blameless validator, the trigger matrix, the publish gates — lives in the service, where
it can be tested without a client and reused by the scheduled job.

**One route is not in §7.7.2's list**: ``PATCH /pir/{id}/actions/{action_id}``. The reasoning
is at the route itself — in short, §7.7.1 ships ``status`` with three terminal values and a
``closed_at`` column, and an API that can reach none of them would mean an action item can be
created and never closed. It is a reasoned deviation, not an unread line of the spec.

**The flag is a 404, not a 403.** With ``PIR_ENABLED`` unset the whole lane is supposed to be
invisible: §7.7 ships it off, and "the system behaves exactly as it does today" means today's
system, which has no ``/pir`` surface at all. A 403 would announce that the feature exists and
that the caller is merely not allowed it, which is a different (and false) statement.

**Operator scoping.** ``post_incident_reviews`` carries ``operator_id``, so ``_owned`` filters
it directly. ``pir_action_items`` does not: every route reaches an action item by resolving its
parent review through ``_get_owned`` first — which applies the operator clause and answers 404,
never 403, for another operator's row — and only then filters on ``pir_id``. The child query is
therefore bounded by a WHERE clause on the parent, not by a check after the read. The same
applies to the ``problem_id`` an action item may carry: the problem is re-resolved through
``_get_owned`` before it is stored, so a caller cannot staple another operator's PRB to a
review by guessing its id.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from noc_agents.api import auth
from noc_agents.api.auth import require_role
from noc_agents.api.deps import OPERATIONS, READERS, SUPERVISORS, _actor, _get_owned, _owned, _settings
from noc_agents.db.models import AuditRow, IncidentRow, ProblemRow, get_session, utcnow
from noc_agents.db.models_pir import PirActionItemRow, PostIncidentReviewRow
from noc_agents.services import pir as pir_service

router = APIRouter(prefix="/api/v1", tags=["pir"])


def require_pir_enabled() -> None:
    """404 the whole lane while ``PIR_ENABLED`` is off (the default). See the module docstring."""
    if not pir_service.pir_enabled():
        raise HTTPException(404, "post-incident reviews are not enabled on this deployment (PIR_ENABLED=false)")


_ENABLED = Depends(require_pir_enabled)

#: §9.3's PIR row, read side: noc_analyst edits, shift_supervisor and duty_manager publish,
#: management, planning and legal read, admin does everything -- and msp_coordinator and
#: field_engineer have "—". Not READERS, which is the incident surface's tuple and admits
#: both vendor roles while leaving legal out: a review's root causes, its went-poorly list and
#: its vendor-attributed action items are exactly what an external MSP role should not read,
#: and exactly what legal is in the row to read. (The known-error route at the bottom is an
#: incident-workspace read of the problem record, not a review, and keeps READERS.)
PIR_READERS: tuple[str, ...] = OPERATIONS + ("management", "planning", "legal")


# ------------------------------------------------------------------------------- bodies


class PirPatchIn(BaseModel):
    """Editable review fields. Only the keys actually present in the request are applied.

    ``status`` accepts DRAFT / IN_REVIEW / NOT_REQUIRED. PUBLISHED is not settable here: it
    carries a reviewer, a timestamp and two hard preconditions, so it has its own route.
    """

    summary: str | None = None
    detection_method: str | None = None
    detected_at: datetime | None = None
    trigger: str | None = None
    root_causes: str | None = None
    contributing_factors: str | None = None
    went_well: str | None = None
    went_poorly: str | None = None
    got_lucky: str | None = None
    status: str | None = None
    revenue_note: str | None = None


class ActionIn(BaseModel):
    type: str
    priority: str
    description: str
    owner_token: str
    due_date: date
    problem_id: str | None = None
    tracking_ref: str | None = None
    status: str | None = None


class ActionPatchIn(BaseModel):
    """Editable action-item fields. Only the keys present in the request are applied.

    ``status`` is the reason this body exists (see ``patch_action``); the rest are here
    because the same validation already had to run, and an action item whose due date can
    never slip is an action item people stop updating and start keeping in a spreadsheet.
    """

    status: str | None = None
    type: str | None = None
    priority: str | None = None
    description: str | None = None
    owner_token: str | None = None
    due_date: date | None = None
    problem_id: str | None = None
    tracking_ref: str | None = None


class PublishIn(BaseModel):
    """``reviewer`` is the human signing the review; with auth on, the principal wins."""

    reviewer: str | None = None
    rationale: str | None = None


class ProblemPatchIn(BaseModel):
    """The §7.7.1 known-error fields on an existing problem record."""

    root_cause: str | None = None
    workaround: str | None = None
    is_known_error: bool | None = None
    permanent_fix_plan: str | None = None
    owner_token: str | None = None
    target_date: datetime | None = None
    status: str | None = None
    closure_summary: str | None = None
    closed: bool | None = None


# ------------------------------------------------------------------------------ helpers

_OWNER_TOKEN_MESSAGE = "owner_token must be a role token (RNIO / FE / MSP_POWER), not a person's name"


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


def _incident_for(session, pir: PostIncidentReviewRow) -> IncidentRow:
    """The review's incident, re-resolved through the operator clause.

    Not ``session.get``: the incident id on the review is trusted, but reading it through
    ``_get_owned`` keeps every incident read in this module on the one scoped path, so a
    future edit cannot quietly introduce the first unscoped one.
    """
    return _get_owned(session, IncidentRow, pir.incident_id, what="incident")


def _reject_named_people(session, pir: PostIncidentReviewRow, values: dict[str, Any]) -> None:
    """422 when a blameless-validated field names a person (§7.7.3).

    Applied to the values being written, not to the stored row, so the rejection happens
    before the text exists anywhere. The message is the spec's exact wording and carries the
    alternative; the name that triggered it is never echoed back.
    """
    fields = [f for f in ("root_causes", "contributing_factors") if f in values]
    if not fields:
        return
    names = pir_service.person_names_for_incident(session, _incident_for(session, pir))
    if any(pir_service.blameless_violation(values[f], names) for f in fields):
        raise HTTPException(422, pir_service.BLAMELESS_MESSAGE)


def _validated_action_values(session, pir: PostIncidentReviewRow, values: dict[str, Any]) -> dict[str, Any]:
    """Validate the action-item fields present in ``values``; return them ready to write.

    Shared by create and update so the two cannot drift: a vocabulary enforced on POST and
    not on PATCH is a vocabulary enforced nowhere, because the second request is the easy
    way round the first. ``problem_id`` is re-resolved through ``_get_owned`` here, so a
    caller cannot staple another operator's PRB to a review by guessing its id on either
    route.
    """
    out = dict(values)
    # PATCH sends only the keys the caller set, so an explicit null is a real request to
    # clear the column — and on these six that is a NOT NULL violation at commit, i.e. a 500
    # for what is plainly a bad request. Refuse it here, where the answer can say which field
    # and why. (Unreachable from POST: pydantic makes them required there.)
    for field in ("type", "priority", "description", "owner_token", "due_date", "status"):
        if field in out and (out[field] is None or out[field] == ""):
            raise HTTPException(422, f"{field} cannot be cleared; every action item must keep one")
    if "type" in out and out["type"] not in pir_service.ACTION_TYPES:
        raise HTTPException(422, f"type must be one of {list(pir_service.ACTION_TYPES)}")
    if "priority" in out and out["priority"] not in pir_service.ACTION_PRIORITIES:
        raise HTTPException(422, f"priority must be one of {list(pir_service.ACTION_PRIORITIES)}")
    if "status" in out:
        out["status"] = str(out["status"] or "").upper()
        if out["status"] not in pir_service.ACTION_STATUSES:
            raise HTTPException(422, f"status must be one of {list(pir_service.ACTION_STATUSES)}")
    if "owner_token" in out:
        # §7.7.6: role tokens, not names. Two checks, because each catches what the other
        # misses — the shape check refuses anything written like a name, and the name check
        # refuses an all-caps token that happens to be one of this incident's actual people.
        owner = (out["owner_token"] or "").strip()
        names = pir_service.person_names_for_incident(session, _incident_for(session, pir))
        if not pir_service.is_role_token(owner) or pir_service.blameless_violation(owner, names):
            raise HTTPException(422, _OWNER_TOKEN_MESSAGE)
        out["owner_token"] = owner
    if out.get("problem_id"):
        out["problem_id"] = _get_owned(session, ProblemRow, out["problem_id"], what="problem").id
    return out


def _owned_action(session, pir: PostIncidentReviewRow, action_id: str) -> PirActionItemRow:
    """One action item of ``pir``, or 404.

    Scoped through the parent, which is the whole scoping story for this table: ``pir`` was
    resolved with ``_get_owned`` (operator clause applied, 404 and never 403), and the
    ``pir_id`` predicate below is what stops an id from another review — another operator's
    or merely another incident's — resolving here. There is no ``_owned(PirActionItemRow)``
    to reach for and there should not be; see the docstring on ``PirActionItemRow``.
    """
    action = session.scalar(
        select(PirActionItemRow).where(PirActionItemRow.id == action_id, PirActionItemRow.pir_id == pir.id)
    )
    if action is None:
        raise HTTPException(404, "action item not found")
    return action


def _detail(session, pir: PostIncidentReviewRow) -> dict[str, Any]:
    body = pir_service.pir_out(pir)
    body["actions"] = [pir_service.action_out(a) for a in pir_service.action_items(session, pir)]
    return body


# -------------------------------------------------------------------------------- reads


@router.get("/pir", dependencies=[_ENABLED, Depends(require_role(*PIR_READERS))])
def list_pir(status: str | None = None, limit: int = 100) -> list[dict]:
    """Reviews for this operator, newest first. ``?status=DRAFT`` feeds the awaiting-review counter."""
    session = get_session()
    try:
        stmt = _owned(PostIncidentReviewRow)
        if status:
            stmt = stmt.where(PostIncidentReviewRow.status == status.upper())
        rows = session.scalars(stmt.order_by(PostIncidentReviewRow.created_at.desc()).limit(limit)).all()
        # The list view carries the summary fields only; the timeline of a chatty P1 is
        # hundreds of entries and no list needs it.
        return [
            {
                k: v
                for k, v in pir_service.pir_out(row).items()
                if k not in ("timeline", "went_well", "went_poorly", "got_lucky")
            }
            for row in rows
        ]
    finally:
        session.close()


# Declared BEFORE "/pir/{pir_id}": FastAPI resolves in registration order, so the parametric
# route would otherwise swallow this one and answer 404 for a review called "awaiting-review".
@router.get("/pir/awaiting-review", dependencies=[_ENABLED, Depends(require_role(*PIR_READERS))])
def awaiting_review() -> dict:
    """The Wallboard's "PIRs awaiting review" counter (§7.7.7).

    Its own route rather than a field on ``/metrics/summary``: that payload belongs to
    ``main.py``, which this lane does not edit. Folding the count into ``MetricsSummary`` is
    one call to ``services.pir.awaiting_review_count`` whenever that file is next opened.
    """
    session = get_session()
    try:
        operator_id = _settings().operator.operator_id
        return {"awaiting_review": pir_service.awaiting_review_count(session, operator_id=operator_id)}
    finally:
        session.close()


@router.get("/pir/{pir_id}", dependencies=[_ENABLED, Depends(require_role(*PIR_READERS))])
def get_pir(pir_id: str) -> dict:
    session = get_session()
    try:
        pir = _get_owned(session, PostIncidentReviewRow, pir_id, what="PIR")
        return _detail(session, pir)
    finally:
        session.close()


@router.get("/pir/{pir_id}/actions", dependencies=[_ENABLED, Depends(require_role(*PIR_READERS))])
def list_actions(pir_id: str) -> list[dict]:
    session = get_session()
    try:
        pir = _get_owned(session, PostIncidentReviewRow, pir_id, what="PIR")
        return [pir_service.action_out(a) for a in pir_service.action_items(session, pir)]
    finally:
        session.close()


# ------------------------------------------------------------------------------- writes


@router.patch("/pir/{pir_id}", dependencies=[_ENABLED])
def patch_pir(pir_id: str, body: PirPatchIn, principal: auth.Principal = Depends(require_role(*OPERATIONS))) -> dict:
    """Edit the narrative. The blameless validator gates ``root_causes``/``contributing_factors``."""
    values = body.model_dump(exclude_unset=True)
    session = get_session()
    try:
        pir = _get_owned(session, PostIncidentReviewRow, pir_id, what="PIR")
        if pir.status == pir_service.PUBLISHED:
            # A published review is the record of what a named human signed. Editing it in
            # place would rewrite history under their name; the correction belongs in a new
            # note or a follow-up action, which is why there is no unpublish route.
            raise HTTPException(409, "a PUBLISHED review is immutable; add an action item instead")
        status = values.pop("status", None)
        if status is not None:
            status = str(status).upper()
            if status == pir_service.PUBLISHED:
                raise HTTPException(422, "publish through POST /api/v1/pir/{id}/publish, which records the reviewer")
            if status not in pir_service.PIR_STATUSES:
                raise HTTPException(422, f"status must be one of {list(pir_service.PIR_STATUSES)}")
        revenue_note = values.pop("revenue_note", None)
        _reject_named_people(session, pir, values)
        for field, value in values.items():
            setattr(pir, field, value)
        if status is not None:
            pir.status = status
        if revenue_note is not None:
            # The one impact field a human fills in: the rest are computed from the incident
            # and must not be typed over, or the review stops matching the ticket it reviews.
            impact = json.loads(pir.impact_json or "{}")
            impact["revenue_note"] = revenue_note
            pir.impact_json = json.dumps(impact)
        pir.updated_at = utcnow()
        session.commit()
        return _detail(session, pir)
    finally:
        session.close()


@router.post("/pir/{pir_id}/actions", dependencies=[_ENABLED])
def add_action(pir_id: str, body: ActionIn, principal: auth.Principal = Depends(require_role(*OPERATIONS))) -> dict:
    """Add one typed action item with exactly one owner and a due date.

    §7.7.6: ``owner_token`` is a role token, never a person. An action outlives whoever is
    on shift today, and a name in this field is the same blame the narrative fields are
    validated against, one column over.
    """
    session = get_session()
    try:
        pir = _get_owned(session, PostIncidentReviewRow, pir_id, what="PIR")
        values = body.model_dump()
        values["status"] = values.get("status") or "OPEN"
        values = _validated_action_values(session, pir, values)
        action = PirActionItemRow(pir_id=pir.id, **values)
        session.add(action)
        pir.updated_at = utcnow()
        session.commit()
        return pir_service.action_out(action)
    finally:
        session.close()


@router.patch("/pir/{pir_id}/actions/{action_id}", dependencies=[_ENABLED])
def patch_action(
    pir_id: str,
    action_id: str,
    body: ActionPatchIn,
    principal: auth.Principal = Depends(require_role(*OPERATIONS)),
) -> dict:
    """Move an action item along, or correct it.

    **§7.7.2 does not list this route.** It exists anyway, and the reason is in the DDL that
    section ships: ``pir_action_items.status`` has the vocabulary
    ``OPEN | IN_PROGRESS | DONE | WONT_DO`` and the table carries ``closed_at``. Three
    terminal states and a closure timestamp with no way to reach any of them is an omission
    in the API list, not a deliberate restriction — read literally it would mean an action
    item can be created and never closed, which makes the whole lane inert in practice:
    nobody completes a post-incident review whose actions stay OPEN for ever, and the
    "≥ 1 P0/P1 action" publish gate would be a promise the product cannot keep. So the route
    is the smallest thing that makes the shipped schema usable, gated exactly like every
    other write here, and it adds no column, no status and no vocabulary of its own.

    ``closed_at`` is maintained by ``services.pir.transition_action``: stamped when the item
    becomes terminal, cleared when it leaves the terminal set, untouched otherwise — so an
    edit to the description cannot silently re-date a closure.
    """
    values = body.model_dump(exclude_unset=True)
    session = get_session()
    try:
        pir = _get_owned(session, PostIncidentReviewRow, pir_id, what="PIR")
        action = _owned_action(session, pir, action_id)
        values = _validated_action_values(session, pir, values)
        status = values.pop("status", None)
        for field, value in values.items():
            setattr(action, field, value)
        if status is not None:
            pir_service.transition_action(action, status)
        pir.updated_at = utcnow()
        session.commit()
        return pir_service.action_out(action)
    finally:
        session.close()


@router.post("/pir/{pir_id}/publish", dependencies=[_ENABLED])
def publish_pir(pir_id: str, body: PublishIn, principal: auth.Principal = Depends(require_role(*SUPERVISORS))) -> dict:
    """Publish the review. 422 with every unmet precondition listed (§7.7.2)."""
    reviewer = _actor(principal, body.reviewer)
    session = get_session()
    try:
        pir = _get_owned(session, PostIncidentReviewRow, pir_id, what="PIR")
        if pir.status == pir_service.PUBLISHED:
            raise HTTPException(409, "this review is already published")
        blockers = pir_service.publish_blockers(session, pir, reviewer=reviewer)
        if blockers:
            raise HTTPException(422, "; ".join(blockers))
        now = utcnow()
        pir.status = pir_service.PUBLISHED
        pir.reviewer = reviewer
        pir.reviewed_at = now
        pir.published_at = now
        pir.updated_at = now
        _audit(
            session,
            actor=reviewer,
            action="pir.published",
            entity_type="post_incident_review",
            entity_id=pir.id,
            rationale=(body.rationale or "").strip(),
            payload={"incident_id": pir.incident_id, "ai_assisted": int(pir.ai_assisted or 0)},
        )
        session.commit()
        return _detail(session, pir)
    finally:
        session.close()


@router.post("/pir/{pir_id}/draft/llm", dependencies=[_ENABLED])
def draft_llm(pir_id: str, principal: auth.Principal = Depends(require_role(*OPERATIONS))) -> dict:
    """Queue the optional model draft (§7.7.2: assist; DRAFT text only).

    The request itself calls nothing: it writes a redacted ``LLM_CALL`` row to the outbox
    and returns. Nothing about the review changes except ``ai_assisted``, and a named human
    still has to publish.
    """
    session = get_session()
    try:
        pir = _get_owned(session, PostIncidentReviewRow, pir_id, what="PIR")
        if pir.status == pir_service.PUBLISHED:
            raise HTTPException(409, "a PUBLISHED review is immutable")
        inc = _incident_for(session, pir)
        row, queued_now = pir_service.queue_llm_draft(session, pir, inc)
        session.commit()
        return {
            "ok": True,
            "queued": queued_now,
            "already_queued": not queued_now,
            "outbox_id": row.id,
            "ai_assisted": int(pir.ai_assisted or 0),
        }
    finally:
        session.close()


@router.post("/incidents/{incident_id}/pir", dependencies=[_ENABLED])
def open_pir_for_incident(incident_id: str, principal: auth.Principal = Depends(require_role(*OPERATIONS))) -> dict:
    """Manually open a review (``opened_reason=MANUAL``). Idempotent: an existing one is returned.

    A CANCELLED incident gets ``NOT_REQUIRED`` rather than DRAFT — the service decides that,
    so the manual and automatic paths cannot disagree about it.
    """
    session = get_session()
    try:
        inc = _get_owned(session, IncidentRow, incident_id, what="incident")
        pir, created = pir_service.open_pir(session, inc, reason=pir_service.REASON_MANUAL)
        session.commit()
        body = _detail(session, pir)
        body["created"] = created
        return body
    finally:
        session.close()


@router.patch("/problems/{problem_id}", dependencies=[_ENABLED])
def patch_problem(
    problem_id: str, body: ProblemPatchIn, principal: auth.Principal = Depends(require_role(*OPERATIONS))
) -> dict:
    """Record the known-error fields on a problem record (§7.7.1).

    This is what makes the next matching incident useful: ``is_known_error=1`` plus a
    workaround is what ``services.pir.known_error_for_incident`` surfaces on the next
    incident with the same ``site|domain`` signature.
    """
    values = body.model_dump(exclude_unset=True)
    actor = _actor(principal, None)
    session = get_session()
    try:
        problem = _get_owned(session, ProblemRow, problem_id, what="problem")
        owner = values.get("owner_token")
        if owner is not None and not pir_service.is_role_token(owner):
            raise HTTPException(422, _OWNER_TOKEN_MESSAGE)
        became_known_error = bool(values.get("is_known_error")) and not int(problem.is_known_error or 0)
        closed = values.pop("closed", None)
        if "is_known_error" in values:
            values["is_known_error"] = 1 if values["is_known_error"] else 0
        for field, value in values.items():
            setattr(problem, field, value)
        now = utcnow()
        if became_known_error and problem.known_error_since is None:
            # Stamped by the system, not typed by the caller: "since when has this been a
            # known error" is the number every recurrence argument turns on, and a
            # hand-entered one is the first thing that gets back-dated.
            problem.known_error_since = now
        if closed is not None:
            problem.closed_at = now if closed else None
            if closed:
                problem.status = "CLOSED"
        # ``last_seen`` is NOT touched: writing up a known error is not the site failing
        # again, and bumping it here would quietly reset the recurrence window RECURRENCE
        # counts over.
        _audit(
            session,
            actor=actor,
            action="problem.known_error_updated",
            entity_type="problem",
            entity_id=problem.id,
            rationale=(problem.closure_summary or "")[:500],
            payload={"fields": sorted(values), "is_known_error": int(problem.is_known_error or 0)},
        )
        session.commit()
        return {
            "id": problem.id,
            "problem_number": problem.problem_number,
            "signature": problem.signature,
            "status": problem.status,
            "root_cause": problem.root_cause,
            "workaround": problem.workaround,
            "is_known_error": int(problem.is_known_error or 0),
            "known_error_since": problem.known_error_since,
            "permanent_fix_plan": problem.permanent_fix_plan,
            "owner_token": problem.owner_token,
            "target_date": problem.target_date,
            "closed_at": problem.closed_at,
            "closure_summary": problem.closure_summary,
        }
    finally:
        session.close()


@router.get("/incidents/{incident_id}/known-error", dependencies=[_ENABLED, Depends(require_role(*READERS))])
def known_error_for(incident_id: str) -> dict:
    """The open known error matching this incident's signature, for the workspace panel.

    The same read ENRICH/RECURRENCE make (§7.7.3). Exposed as a route as well so the
    Incident Workspace can show it without waiting for the next lifecycle run, and so the
    surfacing behaviour is observable from the outside.
    """
    session = get_session()
    try:
        inc = _get_owned(session, IncidentRow, incident_id, what="incident")
        known_error = pir_service.known_error_for_incident(session, inc)
        if known_error is None:
            return {"known_error": None}
        return {"known_error": known_error, "note": pir_service.known_error_note(known_error)}
    finally:
        session.close()
