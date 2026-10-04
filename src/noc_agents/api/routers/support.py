"""The support desk's routes, ``/api/v1/support/...`` (docs/SUPPORT_DESK.md "API").

Thin transport over ``noc_agents.support``: parse and validate the body, apply RBAC, call the
desk, turn its verdicts into status codes, serialise. Every decision worth arguing about --
triage, grounding, tool limits, escalation -- lives in the package, where the eval suite
measures it without an HTTP client.

**RBAC.** §9.3 predates the desk and has no row for it, so the gate is the contract's, spelled
out in ``api/deps.SUPPORT_READERS``: reads for the operations floor plus management, writes
(claim, resolve, approve, reject, seed, run the evals) for ``OPERATIONS``. Inert while
``AUTH_DISABLED=true``, like every gate in this codebase.

**The one public route.** ``POST /complaints`` is the customer's registration form, so it
takes no role. What makes that safe: it is rate-limited per MSISDN and per client address
(``support/ratelimit.py``); a reversal runs only on a transaction code the caller typed
(``needs_verification`` otherwise); every reply echoes only what the caller typed; and a caller
without a support read role gets the PUBLIC view of the result (``support/views.py``), built from
facts the caller already holds -- so the form cannot be used to learn whose a number is, what
was sent from it or what its limits are. A public submission that hits the dedupe gets only the
reference and status of the case on file.

**Configuration.** If a file under ``config/support/`` is missing or invalid every route answers
503 naming the file, rather than 500 per request.

**The flag is a 404, not a 403** (the ``complaints``/``pir`` precedent): with
``SUPPORT_DESK_ENABLED=false`` every route here answers 404, the public form included.

**The production guard is registration-time** (§7.0.5, as ``main.py``'s ledger download and
the complaints lane): customer complaints are personal data, so with ``AUTH_DISABLED=true``
and ``NOC_ENV=production`` none of these routes is registered at all -- an unauthenticated
production deployment has no support surface rather than one whose gates are all inert.

**Operator scoping.** Every read filters on the active operator's id in the WHERE clause
(``support/views.get_complaint`` and friends); another operator's complaint is a 404,
indistinguishable from an id that does not exist.
"""

from __future__ import annotations

from typing import Any, Literal, get_args

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, field_validator

from noc_agents.api import auth
from noc_agents.api.auth import require_role
from noc_agents.api.deps import OPERATIONS, SUPPORT_READERS, _actor, _settings
from noc_agents.db.models import get_session, utcnow
from noc_agents.db.models_support import SupportComplaintRow
from noc_agents.support import desk, evals, views
from noc_agents.support.context import ENABLED_ENV, SupportConfigError, default_context, support_desk_enabled
from noc_agents.support.llm_port import triage_port
from noc_agents.support.ratelimit import complaint_limiter
from noc_agents.support.seed import seed_demo
from noc_agents.support.text import InvalidMsisdn, clean, mask_msisdn, normalise_msisdn
from noc_agents.support.vocab import CATEGORIES, CHANNELS, ROUTES, STATUSES

PREFIX = "/api/v1/support"


def require_support_ready() -> None:
    """404 the whole lane while ``SUPPORT_DESK_ENABLED`` is off; 503 while its configuration is broken."""
    if not support_desk_enabled():
        raise HTTPException(404, f"the support desk is not enabled on this deployment ({ENABLED_ENV}=false)")
    try:
        default_context()
    except SupportConfigError as exc:
        raise HTTPException(503, f"the support desk is unavailable: {exc}") from None


_lane = APIRouter(prefix=PREFIX, tags=["support"], dependencies=[Depends(require_support_ready)])

Channel = Literal["web", "sms", "app", "call_centre", "social"]
StatusFilter = Literal["answered", "action_taken", "awaiting_approval", "escalated", "in_progress", "resolved", "closed"]
RouteFilter = Literal["resolver", "action", "human"]
CategoryFilter = Literal["network", "data_bundles", "mpesa", "billing", "sim_and_fraud", "device_settings",
                         "roaming", "account", "other"]
# The Literals give OpenAPI and the 422s their enums; the vocab module is the source of truth.
for _literal, _vocab in ((Channel, CHANNELS), (StatusFilter, STATUSES), (RouteFilter, ROUTES), (CategoryFilter, CATEGORIES)):
    if set(get_args(_literal)) != set(_vocab):  # at import, not as a wrong 422 in production
        raise RuntimeError(f"api/routers/support.py: {get_args(_literal)} drifted from {_vocab}")


# ------------------------------------------------------------------------------- bodies


class ComplaintIn(BaseModel):
    """The public registration form."""

    body: str = Field(min_length=desk.MIN_BODY_CHARS, max_length=desk.MAX_BODY_CHARS)
    msisdn: str = Field(max_length=32, description="07XXXXXXXX, 01XXXXXXXX, +2547XXXXXXXX or +2541XXXXXXXX")
    name: str | None = Field(None, max_length=128)
    subject: str | None = Field(None, max_length=desk.MAX_SUBJECT_CHARS)
    channel: Channel = "web"
    account_ref: str | None = Field(None, max_length=32)

    @field_validator("body")
    @classmethod
    def _body(cls, value: str) -> str:
        text = clean(value)
        if len(text) < desk.MIN_BODY_CHARS:
            raise ValueError(f"body must be at least {desk.MIN_BODY_CHARS} characters of text")
        return text

    @field_validator("msisdn")
    @classmethod
    def _msisdn(cls, value: str) -> str:
        try:
            return normalise_msisdn(value)
        except InvalidMsisdn as exc:
            raise ValueError(str(exc)) from None


class ResolveIn(BaseModel):
    reply: str = Field(min_length=desk.MIN_BODY_CHARS, max_length=desk.MAX_BODY_CHARS)
    note: str | None = Field(None, max_length=1000)


class RejectIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


# ------------------------------------------------------------------------------ helpers


def _operator_id() -> str:
    return _settings().operator.operator_id


def _complaint_or_404(session: Any, complaint_id: str) -> SupportComplaintRow:
    row = views.get_complaint(session, _operator_id(), complaint_id)
    if row is None:
        raise HTTPException(404, "complaint not found")
    return row


def _desk_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, desk.DeskNotFound):
        return HTTPException(404, str(exc))
    if isinstance(exc, desk.DeskConflict):
        return HTTPException(409, str(exc))
    return HTTPException(422, str(exc))


# ------------------------------------------------------------------------------ the form


@_lane.post("/complaints", status_code=201)
def register_complaint(body: ComplaintIn, request: Request, response: Response) -> dict[str, Any]:
    """Register a complaint and run the whole desk. 201 with the detail; 200 with the complaint
    already on file when the same number sent the same text in the last two minutes (only its
    reference and status, to a public caller); 429 when the number or the client address has hit
    the form's rate limit."""
    ctx = default_context()
    limit = ctx.policy.rate_limit
    client = request.client.host if request.client else "unknown"
    retry = complaint_limiter.check(
        [(f"msisdn:{_operator_id()}:{body.msisdn}", limit.max_requests), (f"ip:{client}", limit.per_ip_max_requests)],
        window_seconds=limit.window_seconds,
    )
    if retry is not None:
        raise HTTPException(429, "too many complaints; please wait before trying again",
                            headers={"Retry-After": str(max(1, int(retry) + 1))})
    principal = auth.current_principal(request)
    # A support reader (everyone in the demo, auth off) sees the trace; anyone else the public view.
    public = principal is None or principal.role not in SUPPORT_READERS
    session = get_session()
    try:
        port = triage_port(session, actor=principal.display_name if principal else "public complaint form",
                           actor_role=principal.role if principal else "public")
        try:
            result = desk.process_complaint(
                session, operator_id=_operator_id(), body=body.body, msisdn=body.msisdn, name=body.name,
                subject=body.subject, channel=body.channel, account_ref=body.account_ref, ctx=ctx, port=port,
            )
        except desk.DeskInputError as exc:
            raise _desk_errors(exc) from None
        if not result.created:
            response.status_code = 200
            if public:
                return views.duplicate_view(result.complaint, msisdn_masked=mask_msisdn(body.msisdn))
        return views.detail(session, result.complaint, public=public, policy=ctx.policy)
    finally:
        session.close()


# ---------------------------------------------------------------------------------- reads


@_lane.get("/complaints", dependencies=[Depends(require_role(*SUPPORT_READERS))])
def list_complaints(
    status: StatusFilter | None = None,
    route: RouteFilter | None = None,
    category: CategoryFilter | None = None,
    q: str | None = Query(None, max_length=200),
    limit: int = Query(views.DEFAULT_LIST_LIMIT, ge=1, le=views.MAX_LIST_LIMIT),
) -> dict[str, Any]:
    """The queue, newest first; ``counts`` are over all the operator's complaints."""
    session = get_session()
    try:
        return views.list_complaints(session, _operator_id(), status=status, route=route, category=category, q=q, limit=limit)
    finally:
        session.close()


@_lane.get("/complaints/{complaint_id}", dependencies=[Depends(require_role(*SUPPORT_READERS))])
def get_complaint(complaint_id: str) -> dict[str, Any]:
    session = get_session()
    try:
        return views.detail(session, _complaint_or_404(session, complaint_id))
    finally:
        session.close()


@_lane.get("/kb", dependencies=[Depends(require_role(*SUPPORT_READERS))])
def list_articles() -> dict[str, Any]:
    return {"articles": [article.as_dict() for article in default_context().kb.articles]}


@_lane.get("/kb/search", dependencies=[Depends(require_role(*SUPPORT_READERS))])
def search_articles(q: str = Query(..., min_length=2, max_length=200)) -> dict[str, Any]:
    """BM25 over the knowledge base, raw scores (the same retrieval the resolver uses)."""
    return {"results": [hit.as_result() for hit in default_context().kb.search(q, limit=10)]}


@_lane.get("/metrics", dependencies=[Depends(require_role(*SUPPORT_READERS))])
def support_metrics(hours: int = Query(0, ge=0, le=24 * 366, description="hours back; 0 = all time")) -> dict[str, Any]:
    session = get_session()
    try:
        return views.metrics(session, _operator_id(), hours=hours, now=utcnow())
    finally:
        session.close()


@_lane.get("/evals/latest", dependencies=[Depends(require_role(*SUPPORT_READERS))])
def latest_eval() -> dict[str, Any]:
    session = get_session()
    try:
        report = evals.latest_report(session, _operator_id())
    finally:
        session.close()
    if report is None:
        raise HTTPException(404, "no eval run on record yet; POST /api/v1/support/evals/run")
    return report


# ---------------------------------------------------------------------------- the people


@_lane.post("/complaints/{complaint_id}/claim")
def claim_complaint(complaint_id: str, principal: auth.Principal = Depends(require_role(*OPERATIONS))) -> dict[str, Any]:
    session = get_session()
    try:
        row = _complaint_or_404(session, complaint_id)
        try:
            desk.claim(session, row, actor=_actor(principal, None))
        except (desk.DeskConflict, desk.DeskInputError) as exc:
            raise _desk_errors(exc) from None
        return views.detail(session, row)
    finally:
        session.close()


@_lane.post("/complaints/{complaint_id}/resolve")
def resolve_complaint(complaint_id: str, body: ResolveIn,
                      principal: auth.Principal = Depends(require_role(*OPERATIONS))) -> dict[str, Any]:
    session = get_session()
    try:
        row = _complaint_or_404(session, complaint_id)
        try:
            desk.resolve_case(session, row, actor=_actor(principal, None), reply=body.reply, note=body.note)
        except (desk.DeskConflict, desk.DeskInputError) as exc:
            raise _desk_errors(exc) from None
        return views.detail(session, row)
    finally:
        session.close()


@_lane.post("/complaints/{complaint_id}/actions/{tool_call_id}/approve")
def approve_action(complaint_id: str, tool_call_id: str,
                   principal: auth.Principal = Depends(require_role(*OPERATIONS))) -> dict[str, Any]:
    session = get_session()
    try:
        row = _complaint_or_404(session, complaint_id)
        try:
            desk.approve(session, row, tool_call_id, actor=_actor(principal, None), ctx=default_context())
        except (desk.DeskConflict, desk.DeskNotFound, desk.DeskInputError) as exc:
            raise _desk_errors(exc) from None
        return views.detail(session, row)
    finally:
        session.close()


@_lane.post("/complaints/{complaint_id}/actions/{tool_call_id}/reject")
def reject_action(complaint_id: str, tool_call_id: str, body: RejectIn,
                  principal: auth.Principal = Depends(require_role(*OPERATIONS))) -> dict[str, Any]:
    session = get_session()
    try:
        row = _complaint_or_404(session, complaint_id)
        try:
            desk.reject(session, row, tool_call_id, actor=_actor(principal, None), reason=body.reason)
        except (desk.DeskConflict, desk.DeskNotFound, desk.DeskInputError) as exc:
            raise _desk_errors(exc) from None
        return views.detail(session, row)
    finally:
        session.close()


@_lane.post("/evals/run", dependencies=[Depends(require_role(*OPERATIONS))])
def run_evals() -> dict[str, Any]:
    """Run the golden set in-process (deterministic, isolated databases) and keep the report."""
    operator_id = _operator_id()
    try:
        report = evals.run_eval(operator_id=operator_id, ctx=default_context())
    except FileNotFoundError:
        raise HTTPException(503, "the golden set is not installed on this deployment") from None
    except evals.GoldenSetError as exc:
        # A malformed golden file is a deployment fault, not a crash: say which line, keep the last report.
        raise HTTPException(503, f"the golden set is invalid: {exc}") from None
    session = get_session()
    try:
        evals.store_report(session, operator_id, report)
        session.commit()
    finally:
        session.close()
    return report


@_lane.post("/demo/seed")
def seed_complaints(principal: auth.Principal = Depends(require_role(*OPERATIONS))) -> dict[str, int]:
    """About a dozen realistic complaints through the real pipeline (``support/seed.py``)."""
    session = get_session()
    try:
        created = seed_demo(session, operator_id=_operator_id(), actor=_actor(principal, None), ctx=default_context())
        return {"created": created}
    finally:
        session.close()


# --------------------------------------------------------------------- registration (§7.0.5)

#: Every route above, as ``"METHOD /path"``: what the production guard refuses to register.
PRODUCTION_GUARDED_ROUTES: tuple[str, ...] = tuple(
    f"{method} {route.path}" for route in _lane.routes for method in sorted(getattr(route, "methods", ()) or ())
)

router = APIRouter()
if auth.production_guard_active():
    auth.log_production_guard(PRODUCTION_GUARDED_ROUTES)
else:
    router.include_router(_lane)
