"""The desk: one complaint through intake, triage, resolver or action agent, escalation, reply.

:func:`process_complaint` is the whole pipeline and the only way a complaint is created -- the
API's public form, the demo seeder and the eval runner all call it, so what the evals measure
is exactly what customers get. It needs a SQLAlchemy session and nothing else of the web layer.

The order, and why:

1. **Intake** -- validate, normalise, mask the MSISDN, detect the language, and return the
   complaint already on file if the same number sent the same text in the last two minutes
   (a double-tapped submit button must not open two tickets).
2. **Triage** -- category, urgency, sentiment, risk flags, confidence, route (:mod:`triage`).
   It runs BEFORE the reference is allocated, because allocating takes SQLite's write lock and
   triage may (with ``LLM_ENABLED``) wait on a model; nothing waits on a model while holding it.
3. **Reference** -- ``CMP-000123``, allocated under the write lock, and the duplicate check is
   repeated under that lock so two identical submissions racing each other still make one case.
4. **Action agent** (route ``action``) -- looks the account up if the tool needs it, plans the
   call (:mod:`actions`) and runs it. Tools have no side effects until their call is recorded,
   so the outcome can be weighed by the escalation table before anything is committed.
   When there is nothing safe to act on, the resolver answers instead.
5. **Resolver** (route ``resolver``, or the action agent's fallback) -- BM25 over the knowledge
   base; answers only when grounded (:mod:`resolver`).
6. **Escalation** -- the policy table over everything the agents found (:mod:`escalation`);
   the first matching rule is the reason, and the customer gets an honest holding reply.
7. **Reply and persist** -- the complaint, every step (summary, detail, duration), every tool
   call and the messages are written in one transaction; ``support.created`` (and
   ``support.escalated``) leave through the hub only after that commit
   (``realtime/commit_hook``), so the UI never hears about a complaint the database lost.

``route`` on the stored complaint is where the case ENDED: ``resolver`` (answered), ``action``
(a tool fixed it) or ``human`` (a person owns it) -- triage's first choice is in the triage
step's detail. That is the route the eval compares with the golden label.

The human half lives here too -- :func:`claim`, :func:`resolve`, :func:`approve`,
:func:`reject` -- each appending a ``human`` step to the same trace.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from time import perf_counter
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from noc_agents.db.models import new_id, utcnow
from noc_agents.db.models_support import (
    SupportComplaintRow,
    SupportMessageRow,
    SupportStepRow,
    SupportToolCallRow,
)
from noc_agents.realtime.commit_hook import buffer_event
from noc_agents.realtime.hub import RealtimeEvent
from noc_agents.services.numbering import next_support_ref
from noc_agents.support import actions
from noc_agents.support.accounts import Account
from noc_agents.support.context import SupportContext, default_context
from noc_agents.support.escalation import Escalation, EscalationFacts, holding_reply, matching
from noc_agents.support.resolver import ResolverResult, resolve
from noc_agents.support.text import InvalidMsisdn, body_hash, clean, mask_msisdn, normalise_msisdn
from noc_agents.support.tools import ToolEnv, ToolOutcome, run_tool
from noc_agents.support.triage import TriageResult, triage
from noc_agents.support.vocab import CHANNELS, HUMAN_QUEUE, OUTCOME_FOR_STATUS

log = logging.getLogger(__name__)

MIN_BODY_CHARS = 5
MAX_BODY_CHARS = 4000
MAX_SUBJECT_CHARS = 90
AGENT_NAME = "Support desk"

EVENT_CREATED = "support.created"
EVENT_ESCALATED = "support.escalated"
EVENT_UPDATED = "support.updated"


class DeskInputError(ValueError):
    """The complaint cannot be accepted as given (the API answers 422)."""


class DeskConflict(Exception):
    """A person's action does not fit the case's current state (the API answers 409)."""


class DeskNotFound(LookupError):
    """The tool call is not on this complaint (the API answers 404)."""


@dataclass(frozen=True)
class DeskResult:
    complaint: SupportComplaintRow
    created: bool  # False: an identical complaint inside the dedupe window was returned


# --------------------------------------------------------------------------- the trace


@dataclass
class _Step:
    agent: str
    action: str
    summary: str
    detail: dict[str, Any]
    duration_ms: int
    at: datetime


@dataclass
class _Call:
    tool: str
    args: dict[str, Any]
    outcome: ToolOutcome
    at: datetime


class _Trace:
    """Steps and tool calls in the order they happened, timestamped from the complaint's ``now``."""

    def __init__(self, now: datetime) -> None:
        self.now = now
        self.t0 = perf_counter()
        self.steps: list[_Step] = []
        self.calls: list[_Call] = []

    def at(self) -> datetime:
        return self.now + timedelta(seconds=perf_counter() - self.t0)

    def step(self, agent: str, action: str, summary: str, detail: dict[str, Any], *, since: float) -> None:
        ms = max(0, round((perf_counter() - since) * 1000))
        self.steps.append(_Step(agent, action, summary, detail, ms, self.at()))

    def call(self, tool: str, args: dict[str, Any], outcome: ToolOutcome, *, ms: int) -> None:
        at = self.at()
        self.calls.append(_Call(tool, args, outcome, at))
        self.steps.append(_Step(
            "action", "called_tool", f"Called {tool}: {outcome.status.replace('_', ' ')}.",
            {"tool": tool, "status": outcome.status, "policy": outcome.policy, "args": args, "result": outcome.result},
            ms, at,
        ))


def _timed_tool(name: str, env: ToolEnv, args: dict[str, Any]) -> tuple[ToolOutcome, int]:
    t = perf_counter()
    outcome = run_tool(name, env, args)
    return outcome, max(0, round((perf_counter() - t) * 1000))


@dataclass(frozen=True)
class _Attempt:
    """The action agent's planned call and its (not yet recorded) outcome."""

    planned: actions.PlannedCall
    outcome: ToolOutcome
    ms: int


# ------------------------------------------------------------------------------ intake


def validate_input(body: str, msisdn: str, channel: str) -> tuple[str, str]:
    """The cleaned body and the E.164 MSISDN, or :class:`DeskInputError` naming what is wrong."""
    text = clean(body)
    if not MIN_BODY_CHARS <= len(text) <= MAX_BODY_CHARS:
        raise DeskInputError(f"body must be {MIN_BODY_CHARS} to {MAX_BODY_CHARS} characters")
    if channel not in CHANNELS:
        raise DeskInputError(f"channel must be one of {list(CHANNELS)}")
    try:
        return text, normalise_msisdn(msisdn)
    except InvalidMsisdn as exc:
        raise DeskInputError(str(exc)) from None


def _duplicate(session: Session, operator_id: str, msisdn: str, text: str, now: datetime, window: int) -> SupportComplaintRow | None:
    if window <= 0:
        return None
    return session.scalar(
        select(SupportComplaintRow)
        .where(
            SupportComplaintRow.operator_id == operator_id,
            SupportComplaintRow.msisdn == msisdn,
            SupportComplaintRow.body_hash == body_hash(text),
            SupportComplaintRow.created_at >= now - timedelta(seconds=window),
            SupportComplaintRow.created_at <= now,
        )
        .order_by(SupportComplaintRow.created_at.desc())
        .limit(1)
    )


def _subject(subject: str | None, text: str) -> str:
    """The given subject, or the complaint's first line cut at a word boundary, at most 90 chars."""
    source = clean(subject) or clean(text.splitlines()[0] if text else "")
    if len(source) <= MAX_SUBJECT_CHARS:
        return source
    cut = source[: MAX_SUBJECT_CHARS - 1].rsplit(" ", 1)[0]
    return cut + "…"


def _greeting(name: str | None) -> str:
    """First name the CUSTOMER gave, else "there" -- never the account holder's (see views)."""
    first = clean(name).split(" ")[0] if clean(name) else ""
    return first or "there"


def _due_text(due: datetime, timezone: str) -> str:
    local = due.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(timezone))
    return f"{local:%H:%M} {local.tzname()} on {local:%a} {local.day} {local:%b}"


def _repeat_count(session: Session, ctx: SupportContext, operator_id: str, msisdn: str, category: str,
                  account: Account | None, now: datetime) -> int:
    """Complaints on ``category`` from this number in the repeat window, this one included."""
    window = ctx.policy.repeat.window_days
    on_file = session.scalar(
        select(func.count()).select_from(SupportComplaintRow).where(
            SupportComplaintRow.operator_id == operator_id,
            SupportComplaintRow.msisdn == msisdn,
            SupportComplaintRow.category == category,
            SupportComplaintRow.created_at >= now - timedelta(days=window),
        )
    ) or 0
    history = sum(1 for c in account.complaint_history if c.category == category and c.days_ago <= window) if account else 0
    return int(on_file) + history + 1


# ----------------------------------------------------------------------------- the case


@dataclass
class _Case:
    """What the agents decide for one complaint, before it is written."""

    id: str
    ref: str
    text: str
    msisdn: str
    masked: str
    greet: str
    account: Account | None
    triaged: TriageResult
    trace: _Trace
    route: str = "resolver"
    status: str = "answered"
    reply: str | None = None
    citations: list[dict[str, Any]] = field(default_factory=list)
    linked_incident_id: str | None = None
    escalation: Escalation | None = None


def _run_action(session: Session, ctx: SupportContext, case: _Case, operator_id: str, now: datetime) -> _Attempt | None:
    """Look up, plan, and run the call without recording it. None: nothing safe to act on."""
    trace, tool = case.trace, case.triaged.tool or ""
    env = ToolEnv(session, operator_id, case.msisdn, case.account, ctx.policy, now)
    if tool in actions.NEEDS_ACCOUNT:
        outcome, ms = _timed_tool("lookup_account", env, {})
        trace.call("lookup_account", {"msisdn": case.masked}, outcome, ms=ms)
    t = perf_counter()
    planned = actions.plan_call(case.triaged, case.text, case.account)
    if planned is None:
        trace.step("action", "no_safe_action", "Nothing on record could be acted on safely; the resolver will answer.",
                   {"tool": tool, "account_found": case.account is not None}, since=t)
        return None
    trace.step("action", "planned", f"Planned {planned.tool} on the target the complaint identifies.",
               {"tool": planned.tool, "args": planned.args, "why": planned.why}, since=t)
    outcome, ms = _timed_tool(planned.tool, env, planned.args)
    return _Attempt(planned, outcome, ms)


def _run_resolver(ctx: SupportContext, case: _Case) -> ResolverResult:
    t = perf_counter()
    result = resolve(case.text, kb=ctx.kb, policy=ctx.policy, category=case.triaged.category,
                     name=case.greet, ref=case.ref, msisdn_masked=case.masked)
    if result.escalate and result.article is not None:
        summary = f"Matched {result.article.id}, an escalate-only article: handing to a person."
    elif result.grounded and result.article is not None:
        summary = f"Found {result.article.id} (score {result.score:.1f}), above the grounding threshold."
    else:
        summary = f"No article reached the grounding threshold of {ctx.policy.grounding_threshold:g} (best {result.score:.1f})."
    case.trace.step("resolver", "retrieved", summary, result.detail(), since=t)
    return result


def _escalate(ctx: SupportContext, case: _Case, hits: list[Escalation], pending: _Attempt | None, due: datetime) -> None:
    """Hand the case to a person. A call over its limit is recorded (parked for approval), as is a
    call that could not finish; a call that WOULD have succeeded is held, not run -- the person
    who now owns the case decides."""
    t = perf_counter()
    first = hits[0]
    recorded = pending is not None and pending.outcome.status != "ok"
    if pending is not None and recorded:
        case.trace.call(pending.planned.tool, pending.planned.args, pending.outcome, ms=pending.ms)
    case.escalation = first
    case.route = "human"
    case.status = "awaiting_approval" if first.reason_code == "over_refund_limit" else "escalated"
    case.reply = holding_reply(first, name=case.greet, ref=case.ref, due=_due_text(due, ctx.timezone))
    held = pending.planned.tool if pending is not None and not recorded else None
    case.trace.step(
        "escalation", "escalated", f"Sent to a person: {first.reason_code.replace('_', ' ')} ({first.evidence}).",
        {"reason_code": first.reason_code, "reason": first.reason, "evidence": first.evidence,
         "also_matched": [h.reason_code for h in hits[1:]], "held_tool": held},
        since=t,
    )


def _complete_action(session: Session, ctx: SupportContext, case: _Case, operator_id: str, now: datetime,
                     attempt: _Attempt) -> None:
    """Record the successful call, link the incident if there is one, reply, and note the ticket."""
    tool = attempt.planned.tool
    case.trace.call(tool, attempt.planned.args, attempt.outcome, ms=attempt.ms)
    result = attempt.outcome.result or {}
    if tool == "link_incident":
        case.linked_incident_id = result.get("incident_id")
    case.route, case.status = "action", "action_taken"
    case.reply = actions.success_reply(tool, result, name=case.greet, ref=case.ref, msisdn_masked=case.masked)
    env = ToolEnv(session, operator_id, case.msisdn, case.account, ctx.policy, now)
    note = {"status": "action_taken", "note": actions.ticket_note(tool, result)}
    outcome, ms = _timed_tool("update_ticket", env, note)
    case.trace.call("update_ticket", note, outcome, ms=ms)


def _answer(case: _Case, resolution: ResolverResult) -> None:
    case.route, case.status = "resolver", "answered"
    case.reply = resolution.reply
    case.citations = resolution.citations()


def process_complaint(
    session: Session,
    *,
    operator_id: str,
    body: str,
    msisdn: str,
    name: str | None = None,
    subject: str | None = None,
    channel: str = "web",
    account_ref: str | None = None,
    ctx: SupportContext | None = None,
    now: datetime | None = None,
    port: Any | None = None,
    emit_events: bool = True,
) -> DeskResult:
    """Run one complaint through the desk and commit it. See the module docstring for the order.

    ``port`` is an LLM port the caller has already decided it may use (only triage's tie-break
    uses it); ``None`` keeps the desk deterministic. ``emit_events=False`` is for the eval
    runner, whose complaints live in a throwaway database and must not reach the live hub.
    Raises :class:`DeskInputError` for a body, MSISDN or channel the contract does not accept.
    """
    ctx = ctx or default_context()
    now = now or utcnow()
    text, e164 = validate_input(body, msisdn, channel)
    trace = _Trace(now)
    started = perf_counter()
    window = ctx.policy.dedupe_window_seconds

    existing = _duplicate(session, operator_id, e164, text, now, window)
    if existing is not None:
        return DeskResult(existing, created=False)
    account = ctx.accounts.find(e164)
    masked = mask_msisdn(e164)
    t = perf_counter()
    triaged = triage(text, gazetteer=ctx.gazetteer, policy=ctx.policy, port=port)
    trace.step("intake", "received",
               f"Received a {channel.replace('_', ' ')} complaint from {masked} ({triaged.language}).",
               {"channel": channel, "msisdn_masked": masked, "language": triaged.language, "chars": len(text),
                "account_found": account is not None, "tier": account.tier if account else None},
               since=started)
    trace.step("triage", "classified",
               f"Classified as {triaged.category} (confidence {triaged.confidence:.2f}), urgency {triaged.urgency}, "
               f"sentiment {triaged.sentiment}; route {triaged.route}.",
               triaged.detail(), since=t)

    ref = next_support_ref(session, operator_id)  # takes the write lock (see the module docstring)
    existing = _duplicate(session, operator_id, e164, text, now, window)
    if existing is not None:
        session.rollback()  # gives the reference back
        return DeskResult(existing, created=False)

    case = _Case(id=new_id(), ref=ref, text=text, msisdn=e164, masked=masked, greet=_greeting(name),
                 account=account, triaged=triaged, trace=trace)
    due = now + timedelta(hours=ctx.policy.sla_hours[triaged.urgency])

    attempt = _run_action(session, ctx, case, operator_id, now) if triaged.route == "action" else None
    falls_back = triaged.route == "action" and (attempt is None or attempt.outcome.fallback)
    if falls_back and attempt is not None:  # e.g. no open incident for the place: it was looked for
        trace.call(attempt.planned.tool, attempt.planned.args, attempt.outcome, ms=attempt.ms)
    resolution = _run_resolver(ctx, case) if triaged.route == "resolver" or falls_back else None
    pending = None if falls_back else attempt

    extra_flag = (resolution.escalate,) if resolution is not None and resolution.escalate else ()
    facts = EscalationFacts(
        risk_flags=tuple(dict.fromkeys(triaged.risk_flags + extra_flag)),
        over_limit=pending is not None and pending.outcome.status == "needs_approval",
        repeat_count=_repeat_count(session, ctx, operator_id, e164, triaged.category, account, now),
        sentiment=triaged.sentiment,
        tier=account.tier if account else None,
        confidence=triaged.confidence,
        grounded=resolution.grounded if resolution is not None else None,
        tool_failed=pending is not None and pending.outcome.status in ("refused", "failed"),
    )
    hits = matching(facts, ctx.policy)
    if hits:
        _escalate(ctx, case, hits, pending, due)
    elif pending is not None:
        _complete_action(session, ctx, case, operator_id, now, pending)
    elif resolution is not None:
        _answer(case, resolution)
    else:  # unreachable: a human-routed case always matches a risk rule
        raise RuntimeError("the desk reached no decision")

    row = _persist(session, case, operator_id=operator_id, channel=channel, name=name, subject=subject,
                   account_ref=account_ref, now=now, due=due, started=started)
    if emit_events:
        _emit(session, EVENT_CREATED, row)
        if row.escalation_reason_code:
            _emit(session, EVENT_ESCALATED, row, reason_code=row.escalation_reason_code)
    session.commit()
    return DeskResult(row, created=True)


def _persist(session: Session, case: _Case, *, operator_id: str, channel: str, name: str | None,
             subject: str | None, account_ref: str | None, now: datetime, due: datetime, started: float) -> SupportComplaintRow:
    triaged, account = case.triaged, case.account
    row = SupportComplaintRow(
        id=case.id, operator_id=operator_id, ref=case.ref, created_at=now, updated_at=now, channel=channel,
        customer_name=clean(name) or None, account_holder=account.name if account else None,
        msisdn=case.msisdn, msisdn_masked=case.masked,
        account_ref=clean(account_ref) or (account.account_ref if account else None),
        language=triaged.language, subject=_subject(subject, case.text), body=case.text, body_hash=body_hash(case.text),
        category=triaged.category, urgency=triaged.urgency, sentiment=triaged.sentiment,
        route=case.route, status=case.status, outcome=OUTCOME_FOR_STATUS.get(case.status),
        confidence=triaged.confidence, triage_json=json.dumps(triaged.detail()),
        reply=case.reply, citations_json=json.dumps(case.citations), linked_incident_id=case.linked_incident_id,
        sla_due_at=due, handle_ms=max(0, round((perf_counter() - started) * 1000)),
    )
    if case.escalation is not None:
        row.escalation_reason_code = case.escalation.reason_code
        row.escalation_reason = case.escalation.reason
        row.escalated_at = now
    session.add(row)
    for seq, step in enumerate(case.trace.steps, start=1):
        session.add(SupportStepRow(complaint_id=row.id, seq=seq, agent=step.agent, action=step.action,
                                   summary=step.summary, detail_json=json.dumps(step.detail, default=str),
                                   duration_ms=step.duration_ms, at=step.at))
    for call in case.trace.calls:
        session.add(_call_row(row.id, call.tool, call.args, call.outcome, call.at))
    session.add(SupportMessageRow(complaint_id=row.id, author="customer", name=clean(name) or None, body=case.text, at=now))
    if case.reply:
        session.add(SupportMessageRow(complaint_id=row.id, author="agent", name=AGENT_NAME, body=case.reply, at=case.trace.at()))
    return row


def _call_row(complaint_id: str, tool: str, args: dict[str, Any], outcome: ToolOutcome, at: datetime) -> SupportToolCallRow:
    return SupportToolCallRow(
        complaint_id=complaint_id, tool=tool, args_json=json.dumps(args, default=str),
        result_json=json.dumps(outcome.result, default=str) if outcome.result is not None else None,
        status=outcome.status, policy=outcome.policy, subject_ref=outcome.subject_ref, at=at,
    )


def _emit(session: Session, kind: str, row: SupportComplaintRow, **extra: Any) -> None:
    """Buffer a hub event for after the commit. Ids and labels only: no names, numbers or text."""
    payload = {"id": row.id, "ref": row.ref, "status": row.status, "route": row.route,
               "category": row.category, "urgency": row.urgency, **extra}
    buffer_event(session, RealtimeEvent(type=kind, operator_id=row.operator_id, payload=payload,
                                        incident_id=row.linked_incident_id))


# ------------------------------------------------------------------------- human actions


def _next_seq(session: Session, row: SupportComplaintRow) -> int:
    return int(session.scalar(select(func.max(SupportStepRow.seq)).where(SupportStepRow.complaint_id == row.id)) or 0) + 1


def _human_step(session: Session, row: SupportComplaintRow, action: str, summary: str, detail: dict[str, Any],
                now: datetime, *, agent: str = "human") -> None:
    session.add(SupportStepRow(complaint_id=row.id, seq=_next_seq(session, row), agent=agent, action=action,
                               summary=summary, detail_json=json.dumps(detail, default=str), duration_ms=0, at=now))
    session.flush()


def _set_status(row: SupportComplaintRow, status: str, now: datetime) -> None:
    row.status = status
    row.outcome = OUTCOME_FOR_STATUS.get(status, row.outcome)
    row.updated_at = now


def _finish(session: Session, row: SupportComplaintRow, emit_events: bool, **extra: Any) -> None:
    if emit_events:
        _emit(session, EVENT_UPDATED, row, **extra)
    session.commit()


def claim(session: Session, row: SupportComplaintRow, *, actor: str, now: datetime | None = None,
          emit_events: bool = True) -> None:
    """A person takes an escalated (or awaiting-approval) case: ``in_progress``."""
    now = now or utcnow()
    if row.status == "in_progress":
        raise DeskConflict(f"already claimed by {row.claimed_by}")
    if row.status not in ("escalated", "awaiting_approval"):
        raise DeskConflict(f"only an escalated case can be claimed (this one is {row.status})")
    row.claimed_by, row.claimed_at = actor, now
    _set_status(row, "in_progress", now)
    _human_step(session, row, "claimed", f"Claimed by {actor}.", {"claimed_by": actor}, now)
    _finish(session, row, emit_events, claimed_by=actor)


def _pending_calls(session: Session, row: SupportComplaintRow) -> list[SupportToolCallRow]:
    return list(session.scalars(select(SupportToolCallRow).where(
        SupportToolCallRow.complaint_id == row.id, SupportToolCallRow.status == "needs_approval")).all())


def resolve_case(session: Session, row: SupportComplaintRow, *, actor: str, reply: str, note: str | None = None,
                 now: datetime | None = None, emit_events: bool = True) -> None:
    """A person closes the case with a reply to the customer: ``resolved``."""
    now = now or utcnow()
    if row.status not in HUMAN_QUEUE:
        raise DeskConflict(f"only a case with a person can be resolved (this one is {row.status})")
    text = clean(reply)
    if not MIN_BODY_CHARS <= len(text) <= MAX_BODY_CHARS:
        raise DeskInputError(f"reply must be {MIN_BODY_CHARS} to {MAX_BODY_CHARS} characters")
    for call in _pending_calls(session, row):
        call.status, call.decided_by, call.decided_at = "rejected", actor, now
        call.policy = f"superseded: the case was resolved by {actor}"
    row.reply, row.resolved_by, row.resolved_at = text, actor, now
    _set_status(row, "resolved", now)
    session.add(SupportMessageRow(complaint_id=row.id, author="staff", name=actor, body=text, at=now))
    _human_step(session, row, "resolved", f"Resolved by {actor}.", {"resolved_by": actor, "note": clean(note) or None}, now)
    _finish(session, row, emit_events)


def _call_for(session: Session, row: SupportComplaintRow, tool_call_id: str) -> SupportToolCallRow:
    call = session.scalar(select(SupportToolCallRow).where(
        SupportToolCallRow.id == tool_call_id, SupportToolCallRow.complaint_id == row.id))
    if call is None:
        raise DeskNotFound("tool call not found on this complaint")
    if call.status != "needs_approval":
        raise DeskConflict(f"only a call waiting for approval can be decided (this one is {call.status})")
    if row.status not in ("awaiting_approval", "in_progress"):
        raise DeskConflict(f"the case is {row.status}, not waiting for an approval")
    return call


def approve(session: Session, row: SupportComplaintRow, tool_call_id: str, *, actor: str,
            ctx: SupportContext | None = None, now: datetime | None = None, emit_events: bool = True) -> None:
    """A person approves a call over its limit; the tool runs with the approval and the status follows."""
    ctx = ctx or default_context()
    now = now or utcnow()
    call = _call_for(session, row, tool_call_id)
    account = ctx.accounts.find(row.msisdn)
    env = ToolEnv(session, row.operator_id, row.msisdn, account, ctx.policy, now)
    args = json.loads(call.args_json or "{}")
    outcome = run_tool(call.tool, env, args, approved=True)
    call.decided_by, call.decided_at, call.policy = actor, now, outcome.policy
    _human_step(session, row, "approved", f"{actor} approved {call.tool}.", {"tool_call_id": call.id, "tool": call.tool}, now)
    if outcome.status != "ok":
        call.status = "failed" if outcome.status == "failed" else "refused"
        rule = ctx.policy.rule("tool_failed")
        row.escalation_reason_code, row.escalation_reason, row.escalated_at = rule.reason_code, rule.reason, now
        _set_status(row, "escalated", now)
        _human_step(session, row, "escalated", f"The approved {call.tool} could not complete; back to a person.",
                    {"status": outcome.status, "policy": outcome.policy}, now, agent="escalation")
        _finish(session, row, emit_events, tool=call.tool)
        return
    call.status = "approved"
    call.result_json = json.dumps(outcome.result, default=str)
    call.subject_ref = outcome.subject_ref
    result = outcome.result or {}
    note = {"status": "action_taken", "note": actions.ticket_note(call.tool, result) + f" Approved by {actor}."}
    session.add(_call_row(row.id, "update_ticket", note, run_tool("update_ticket", env, note), now))
    row.reply = actions.success_reply(call.tool, result, name=_greeting(row.customer_name), ref=row.ref,
                                      msisdn_masked=row.msisdn_masked)
    _set_status(row, "action_taken", now)
    session.add(SupportMessageRow(complaint_id=row.id, author="agent", name=AGENT_NAME, body=row.reply, at=now))
    _human_step(session, row, "called_tool", f"Called {call.tool} with {actor}'s approval: ok.",
                {"tool": call.tool, "status": "approved", "policy": outcome.policy, "result": result}, now, agent="action")
    _finish(session, row, emit_events, tool=call.tool)


def reject(session: Session, row: SupportComplaintRow, tool_call_id: str, *, actor: str, reason: str,
           now: datetime | None = None, emit_events: bool = True) -> None:
    """A person refuses a call over its limit; the case goes back to ``escalated``."""
    now = now or utcnow()
    why = clean(reason)
    if not why:
        raise DeskInputError("reason is required")
    call = _call_for(session, row, tool_call_id)
    call.status, call.decided_by, call.decided_at = "rejected", actor, now
    call.policy = f"rejected by {actor}: {why}"
    _set_status(row, "escalated", now)
    _human_step(session, row, "rejected", f"{actor} rejected {call.tool}.", {"tool_call_id": call.id, "reason": why}, now)
    _finish(session, row, emit_events, tool=call.tool)
