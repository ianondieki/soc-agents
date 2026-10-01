"""Vendor scorecard routes (spec §7.6.3) -- Phase 4 Lane 4A, step 2: computation and reads.

    GET  /api/v1/scorecards?vendor=&period=&status=      cards the caller may see, newest period first
    GET  /api/v1/scorecards/{id}                         one card with its lines (raw / normalised /
                                                         excluded / SCC minutes / formula / yaml_path)
    POST /api/v1/scorecards/compute?period=&vendor=      compute or recompute (shift_supervisor+)
    POST /api/v1/scorecards/{id}/shadow-review           {rationale}  a named human inspected it (duty_manager+)
    POST /api/v1/scorecards/{id}/publish                 {reason}     release it; starts the dispute window (duty_manager+)
    POST /api/v1/scorecards/{id}/finalise                {reason?}    after the window, no OPEN dispute (duty_manager+)

NOT here yet, on purpose: ``POST /scorecards/lines/{line_id}/dispute``,
``POST /scorecards/{id}/notice/draft`` and the two ``.xlsx`` packs. ``_owned_line`` below is
the door the dispute route will come through. Since schema_version 8 a ``hitl_tasks`` row
carries its own ``operator_id`` and ``incident_id`` is nullable, so a DISPUTE_SCORECARD_LINE or
APPROVE_VENDOR_NOTICE card is owned directly by the operator (``operator_id=card.operator_id``,
``entity_type="vendor_scorecard_line"`` / ``"vendor_scorecard"``) -- no anchor incident.

``POST .../publish`` is not in §7.6.3: there the JOB publishes. This lane's rule is the
opposite -- the job computes DRAFT/SHADOW/WITHHELD and a named human moves a card on -- so
the act needs a route. Nothing on this surface sends anything to anyone.

Behind ``SCORECARDS_ENABLED`` (default OFF): every route answers **404** while the flag is
off, so the surface is what it was before this lane existed.

WHO SEES WHAT. ``require_role(*SCORECARD_READERS)`` gates the reads with §9.3's scorecards
row exactly: noc_analyst (read), shift_supervisor (read + dispute), duty_manager (all),
management (read), msp_coordinator (own vendor), legal (read), admin (all); field_engineer
and planning have no cell and get 403 once auth is on. The tuple is defined HERE, not in
``api/deps.py``: it is this router's statement of its own row. ``require_role`` is inert
while ``AUTH_DISABLED=true`` (the demo default), and two rules here must hold in the demo
too, so they are enforced on the principal's ROLE in this module:

* §7.6.2: a SHADOW card is "visible only to duty_manager/management". The same is true of
  DRAFT and WITHHELD -- they are the operator's working papers. Any other role is shown
  only PUBLISHED and FINAL cards, and an unreleased card's id is a **404** to them, never a
  403 that would confirm the card exists.
* §9.3: ``msp_coordinator`` reads its OWN vendor's cards. The principal carries no vendor
  binding yet (``api/auth.py``), so an AUTHENTICATED msp_coordinator is shown nothing until
  one exists -- fail closed: one vendor reading a competitor's SLA record and proposed
  credits is a commercial harm, an empty list is an inconvenience. With auth off there is
  no identity to bind, exactly the distinction ``api.deps._actor`` draws, so the demo's role
  switcher sees every RELEASED card.

Operator scoping is in the WHERE clause on every read (``_owned`` / ``_get_owned``); another
operator's card is a 404. Lines carry no ``operator_id`` and are only ever reached through
an owned card. The write gates are checked in the service as well as by ``require_role``,
for the same reason ``api/routers/clocks.py`` gives.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from noc_agents.api import auth
from noc_agents.api.auth import require_role
from noc_agents.api.deps import SUPERVISORS, _actor, _get_owned, _owned, _settings
from noc_agents.db.models import get_session
from noc_agents.db.models_scorecards import RELEASED_STATUSES, SCORECARD_STATUSES, ScorecardEvidenceError, VendorScorecardLineRow, VendorScorecardRow
from noc_agents.db.models_vendors import VendorRow
from noc_agents.services.scorecard import (
    PUBLISHER_ROLES,
    REFUSAL_GUARD,
    REFUSAL_TABLE,
    REVIEWER_ROLES,
    ScorecardPermissionError,
    ScorecardStateError,
    compute_on_request,
    failure_reason,
    finalise_scorecard,
    last_ended_period,
    lines_of,
    period_bounds,
    publish_scorecard,
    record_shadow_review,
    scorecard_out,
)
from noc_agents.services.vendors import FLAG, lane_enabled, load_sla_terms, normalise_code

router = APIRouter(prefix="/api/v1", tags=["scorecards"])

#: §9.3, the scorecards row: every role with a "read" cell. field_engineer and planning are
#: absent on purpose. A RELEASED card is readable by all of these; which of them may see an
#: UNRELEASED one is ``INTERNAL_READERS`` below, and the vendor role sees its own vendor only.
SCORECARD_READERS: tuple[str, ...] = ("noc_analyst", "shift_supervisor", "duty_manager", "management", "msp_coordinator", "legal", "admin")
#: Roles that may see a card BEFORE it is released (§7.6.2 names duty_manager and management
#: for SHADOW; admin is listed explicitly, as everywhere -- no implicit bypass).
INTERNAL_READERS: tuple[str, ...] = ("duty_manager", "management", "admin")
#: The vendor-side role (§9.3 "own vendor read").
VENDOR_ROLE = "msp_coordinator"


def require_lane() -> None:
    """Dependency: 404 while ``SCORECARDS_ENABLED`` is off (see the module docstring)."""
    if not lane_enabled():
        raise HTTPException(404, f"Not found ({FLAG} is off)")


class ShadowReviewIn(BaseModel):
    """``reviewed_by`` is honoured only while auth is off (``_actor``), like every write route."""

    rationale: str = Field(max_length=2000)
    reviewed_by: str | None = None


class PublishIn(BaseModel):
    reason: str = Field(max_length=2000)
    published_by: str | None = None


class FinaliseIn(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)
    finalised_by: str | None = None


# ----------------------------------------------------------------------------------- visibility


def _visible_statuses(principal: auth.Principal) -> tuple[str, ...]:
    return SCORECARD_STATUSES if principal.role in INTERNAL_READERS else RELEASED_STATUSES


def _vendor_binding(principal: auth.Principal) -> tuple[bool, str | None]:
    """``(restricted, vendor_code)``. ``(True, None)`` means "restricted to nothing".

    The seam for §9.3's "own vendor": when the identity provider starts putting a vendor on
    the principal (``vendor_code``), this function is the only thing that has to learn it.
    """
    if principal.role != VENDOR_ROLE or not principal.authenticated:
        return False, None
    code = normalise_code(getattr(principal, "vendor_code", None))
    return True, code or None


def _may_see(session, principal: auth.Principal, card: VendorScorecardRow) -> bool:
    if card.status not in _visible_statuses(principal):
        return False
    restricted, code = _vendor_binding(principal)
    if not restricted:
        return True
    vendor = session.get(VendorRow, card.vendor_id)
    return bool(code) and vendor is not None and vendor.code == code


def _card_for(session, principal: auth.Principal, card_id: str) -> VendorScorecardRow:
    """The card if this operator owns it AND this caller may see it; otherwise 404."""
    card = _get_owned(session, VendorScorecardRow, card_id, what="scorecard")
    if not _may_see(session, principal, card):
        raise HTTPException(404, "scorecard not found")
    return card


def _owned_line(session, principal: auth.Principal, line_id: str) -> tuple[VendorScorecardRow, VendorScorecardLineRow]:
    """A line, reached the only way a line may be reached: through a card the active operator
    owns and the caller may see. Unused by the routes below; it is the dispute lane's door."""
    line = session.get(VendorScorecardLineRow, line_id)
    if line is None:
        raise HTTPException(404, "scorecard line not found")
    try:
        card = _card_for(session, principal, line.scorecard_id)
    except HTTPException:
        raise HTTPException(404, "scorecard line not found") from None
    return card, line


def _out(session, card: VendorScorecardRow, *, with_lines: bool) -> dict[str, Any]:
    vendor = session.get(VendorRow, card.vendor_id)
    return scorecard_out(card, vendor, lines_of(session, card.id) if with_lines else None)


# ---------------------------------------------------------------------------------------- reads


@router.get("/scorecards", dependencies=[Depends(require_lane)])
def list_scorecards(
    vendor: str | None = None,
    period: str | None = None,
    status: str | None = None,
    principal: auth.Principal = Depends(require_role(*SCORECARD_READERS)),
) -> list[dict]:
    """Cards this caller may see, newest period first, without lines (``GET /scorecards/{id}`` has them)."""
    s = _settings()
    visible = _visible_statuses(principal)
    if status is not None:
        wanted = status.strip().upper()
        if wanted not in SCORECARD_STATUSES:
            raise HTTPException(400, f"status must be one of {list(SCORECARD_STATUSES)}")
        visible = tuple(v for v in visible if v == wanted)
    restricted, code = _vendor_binding(principal)
    if not visible or (restricted and not code):
        return []
    session = get_session()
    try:
        stmt = _owned(VendorScorecardRow).where(VendorScorecardRow.status.in_(visible))
        if period is not None:
            try:
                stmt = stmt.where(VendorScorecardRow.period == period_bounds(period, s.operator.timezone).label)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        for wanted_code in (normalise_code(vendor) if vendor else None, code if restricted else None):
            if wanted_code:
                stmt = stmt.where(VendorScorecardRow.vendor_id.in_(select(VendorRow.id).where(VendorRow.code == wanted_code)))
        rows = session.scalars(stmt.order_by(VendorScorecardRow.period.desc(), VendorScorecardRow.vendor_id)).all()
        return [_out(session, row, with_lines=False) for row in rows]
    finally:
        session.close()


@router.get("/scorecards/{card_id}", dependencies=[Depends(require_lane)])
def get_scorecard(card_id: str, principal: auth.Principal = Depends(require_role(*SCORECARD_READERS))) -> dict:
    session = get_session()
    try:
        return _out(session, _card_for(session, principal, card_id), with_lines=True)
    finally:
        session.close()


# --------------------------------------------------------------------------------------- writes


@router.post("/scorecards/compute", dependencies=[Depends(require_lane)])
def compute_scorecards(
    period: str | None = None,
    vendor: str | None = None,
    principal: auth.Principal = Depends(require_role(*SUPERVISORS)),
) -> dict:
    """Compute (or recompute) a period: every vendor with incidents, or just ``vendor``.

    ``period`` defaults to the last month that has ended. 400 for a malformed or unfinished
    period, 404 for an unknown vendor, 503 when the terms file is missing or unversioned -- a
    card is never computed on air. Three distinct 409s, each detail starting with its stable
    prefix (``services.scorecard.REFUSAL_*``): ``cannot recompute a released card: ...``
    (PUBLISHED/FINAL, §7.6.6), ``evidence guard refused the write: ...`` (a mapper guard) and
    ``the card could not be written: <constraint clause>`` (the table; the clause names the
    constraint or its columns and never a value). Whatever it computes is DRAFT, SHADOW or
    WITHHELD; this route cannot publish.

    The response carries COUNTS for everyone the route admits; the per-vendor statuses and
    the card ids are added only for ``INTERNAL_READERS`` -- a shift supervisor may trigger the
    computation but is not shown which vendor's card came out WITHHELD, exactly as
    ``GET /scorecards/{id}`` would 404 them on that card.
    """
    s = _settings()
    session = get_session()
    try:
        try:
            load_sla_terms(cfg=s.operator)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(503, f"sla_terms unavailable: {exc}") from exc
        label = period or last_ended_period(tz_name=s.operator.timezone)
        try:
            report, run_id = compute_on_request(session, s, label, vendor_code=vendor, actor=_actor(principal, None))
        except ScorecardStateError as exc:  # "cannot recompute a released card: ..." -- the service's own prefix
            session.rollback()
            raise HTTPException(409, str(exc)) from exc
        except ScorecardEvidenceError as exc:  # a mapper guard refused: card id and column names, never values
            session.rollback()
            raise HTTPException(409, f"{REFUSAL_GUARD}: {exc}") from exc
        except IntegrityError as exc:  # the table refused the write: the constraint clause only, never the statement or its values
            session.rollback()
            raise HTTPException(409, f"{REFUSAL_TABLE}: {failure_reason(exc).partition(': ')[2] or 'constraint failed'}") from exc
        except LookupError as exc:
            session.rollback()
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            session.rollback()
            raise HTTPException(400, str(exc)) from exc
        session.commit()
        out: dict[str, Any] = {"ok": True, "period": report.period, "run_id": run_id, **report.counts()}
        if principal.role in INTERNAL_READERS:
            out.update(computed_detail=list(report.computed), skipped_detail=list(report.skipped), scorecard_ids=list(report.card_ids))
        return out
    finally:
        session.close()


def _transition(card_id: str, principal: auth.Principal, act) -> dict:
    """Shared shape of the three human transitions: fetch through the scoping helpers, let the
    SERVICE decide, map its refusals to 403 / 409 / 400, commit, return the card with lines."""
    session = get_session()
    try:
        card = _card_for(session, principal, card_id)
        try:
            act(session, card)
        except ScorecardPermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except (ScorecardStateError, ScorecardEvidenceError) as exc:  # incl. ScorecardGateError: WITHHELD / unreviewed first period / a guard
            session.rollback()
            raise HTTPException(409, str(exc)) from exc
        except (FileNotFoundError, ValueError) as exc:
            session.rollback()
            raise HTTPException(400, str(exc)) from exc
        session.commit()
        return {"ok": True, "scorecard": _out(session, card, with_lines=True)}
    finally:
        session.close()


@router.post("/scorecards/{card_id}/shadow-review", dependencies=[Depends(require_lane)])
def shadow_review(card_id: str, body: ShadowReviewIn, principal: auth.Principal = Depends(require_role(*REVIEWER_ROLES))) -> dict:
    """§7.6.2: a named human records that they inspected a vendor's FIRST card. Does not publish."""
    return _transition(
        card_id,
        principal,
        lambda session, card: record_shadow_review(
            session, card, reviewer=_actor(principal, body.reviewed_by), reviewer_role=principal.role, rationale=body.rationale
        ),
    )


@router.post("/scorecards/{card_id}/publish", dependencies=[Depends(require_lane)])
def publish(card_id: str, body: PublishIn, principal: auth.Principal = Depends(require_role(*PUBLISHER_ROLES))) -> dict:
    """Release a card. 409 past a failed data-quality gate or for an unreviewed first period."""
    s = _settings()
    return _transition(
        card_id,
        principal,
        lambda session, card: publish_scorecard(
            session,
            card,
            publisher=_actor(principal, body.published_by),
            publisher_role=principal.role,
            reason=body.reason,
            terms=load_sla_terms(cfg=s.operator),
            cfg=s.operator,
        ),
    )


@router.post("/scorecards/{card_id}/finalise", dependencies=[Depends(require_lane)])
def finalise(card_id: str, body: FinaliseIn | None = None, principal: auth.Principal = Depends(require_role(*PUBLISHER_ROLES))) -> dict:
    """PUBLISHED -> FINAL once the dispute window has closed and no line's dispute is OPEN."""
    body = body or FinaliseIn()
    return _transition(
        card_id,
        principal,
        lambda session, card: finalise_scorecard(
            session, card, actor=_actor(principal, body.finalised_by), actor_role=principal.role, reason=body.reason or ""
        ),
    )
