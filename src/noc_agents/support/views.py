"""The desk's read side: the contract's JSON shapes, the queue with its counts, and the metrics.

Every function takes the operator id and puts it in the WHERE clause, the codebase's rule for
operator-owned reads (``api/deps``): another operator's complaint is not filtered out after the
fetch, it is never fetched.

**Two views of a complaint.** Staff (anyone holding a support read role, and everyone in the
demo with ``AUTH_DISABLED=true``) see the full trace, with the VERIFIED account holder and
account reference first. The PUBLIC view -- what an anonymous caller of ``POST /complaints``
gets back once auth is on -- has the same shape but is BUILT from facts the caller already holds,
never filtered from the staff trace, because a filter leaks whatever it forgets:

* the steps are a fixed outline (received, sorted, then answered / fixed / passed to a person),
  so whether the action agent found something to act on, planned a call or gave up shows nowhere;
* the only tool call shown is the one that FIXED the case (name and status, nothing else); a
  parked, held or refused call would tell the caller about the account's limits or history;
* ``awaiting_approval`` reads as ``escalated``, and an account-derived escalation reason
  (``escalation.ACCOUNT_REASONS``) reads as ``account_review`` with the policy's generic sentence;
* the customer block carries only the name the caller typed and the masked number they typed.

Without that the public form is an oracle: type any number, learn whose it is, what was sent from
it and what its limits are. The reply needs no filtering -- every reply is written to echo only
what the customer typed (``actions.success_reply``, ``escalation.customer_facing``).

A public caller whose submission hits the two-minute dedupe gets :func:`duplicate_view`: the
reference and status of the case on file and nothing else of it -- the first submission may have
come from someone else, with their name on it.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from noc_agents.db.models import IncidentRow
from noc_agents.db.models_support import (
    SupportComplaintRow,
    SupportMessageRow,
    SupportStepRow,
    SupportToolCallRow,
)
from noc_agents.services.clock import iso_z
from noc_agents.support.escalation import ACCOUNT_REASONS, ACCOUNT_REVIEW_CODE
from noc_agents.support.policy import SupportPolicy, load_policy
from noc_agents.support.text import mask_msisdn_staff
from noc_agents.support.vocab import CATEGORIES

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200


def _loads(text: str | None, default: Any) -> Any:
    return json.loads(text) if text else default


def get_complaint(session: Session, operator_id: str, complaint_id: str) -> SupportComplaintRow | None:
    """One complaint the operator owns, or None (the router answers 404 either way)."""
    return session.scalar(
        select(SupportComplaintRow).where(
            SupportComplaintRow.id == complaint_id, SupportComplaintRow.operator_id == operator_id
        )
    )


def _linked_incident(session: Session, row: SupportComplaintRow) -> dict[str, Any] | None:
    if not row.linked_incident_id:
        return None
    inc = session.scalar(
        select(IncidentRow).where(IncidentRow.id == row.linked_incident_id, IncidentRow.operator_id == row.operator_id)
    )
    if inc is None:
        return None
    # The incident's CURRENT status, read live: the customer's ticket follows the NOC's.
    return {"id": inc.id, "incident_number": inc.incident_number, "title": inc.title, "status": inc.status}


def _escalation_out(row: SupportComplaintRow, *, public: bool, policy: SupportPolicy) -> dict[str, Any] | None:
    if not row.escalation_reason_code:
        return None
    code, reason = row.escalation_reason_code, row.escalation_reason
    if public and code in ACCOUNT_REASONS:
        code, reason = ACCOUNT_REVIEW_CODE, policy.account_review_reason
    return {
        "reason_code": code,
        "reason": reason,
        "at": iso_z(row.escalated_at),
        "claimed_by": None if public else row.claimed_by,
    }


def complaint_out(session: Session, row: SupportComplaintRow, *, public: bool = False,
                  policy: SupportPolicy | None = None) -> dict[str, Any]:
    """The contract's ``Complaint``; ``public`` gives the caller-safe view (module docstring)."""
    policy = policy or load_policy()
    status = "escalated" if public and row.status == "awaiting_approval" else row.status
    return {
        "id": row.id,
        "ref": row.ref,
        "created_at": iso_z(row.created_at),
        "updated_at": iso_z(row.updated_at),
        "channel": row.channel,
        "customer": {
            # Staff: the verified holder first; the public: only what the caller typed.
            "name": row.customer_name if public else (row.account_holder or row.customer_name),
            # Staff see four digits (docs/CLOSE_THE_LOOP.md 7.2), the public the masked form they typed.
            "msisdn_masked": row.msisdn_masked if public else mask_msisdn_staff(row.msisdn),
            "account_ref": None if public else row.account_ref,
        },
        "language": row.language,
        "subject": row.subject,
        "body": row.body,
        "category": row.category,
        "urgency": row.urgency,
        "sentiment": row.sentiment,
        "route": row.route,
        "status": status,
        "outcome": row.outcome,
        "confidence": row.confidence,
        "escalation": _escalation_out(row, public=public, policy=policy),
        "reply": row.reply,
        "citations": _loads(row.citations_json, []),
        "linked_incident": _linked_incident(session, row),
        "sla_due_at": iso_z(row.sla_due_at),
    }


def step_out(row: SupportStepRow) -> dict[str, Any]:
    return {
        "seq": row.seq,
        "agent": row.agent,
        "action": row.action,
        "summary": row.summary,
        "detail": _loads(row.detail_json, {}),
        "duration_ms": row.duration_ms,
        "at": iso_z(row.at),
    }


def tool_call_out(row: SupportToolCallRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "tool": row.tool,
        "args": _loads(row.args_json, {}),
        "result": _loads(row.result_json, None),
        "status": row.status,
        "policy": row.policy,
        "at": iso_z(row.at),
        "decided_by": row.decided_by,
    }


def message_out(row: SupportMessageRow, *, public: bool = False) -> dict[str, Any]:
    name = None if public and row.author == "staff" else row.name
    return {"id": row.id, "author": row.author, "name": name, "body": row.body, "at": iso_z(row.at)}


#: The public outline's last step, by where the case ended: (agent, action, summary).
_PUBLIC_OUTCOME: dict[str, tuple[str, str, str]] = {
    "resolver": ("resolver", "answered", "Answered from our help articles."),
    "action": ("action", "called_tool", "Fixed by our action agent."),
    "human": ("escalation", "escalated", "Passed to a member of our team."),
}
#: Tools that are bookkeeping around the fix, never "the" fix.
_BOOKKEEPING_TOOLS = frozenset({"lookup_account", "update_ticket"})


def _public_steps(row: SupportComplaintRow, steps: list[SupportStepRow]) -> list[dict[str, Any]]:
    """The fixed outline: received, sorted, and where it ended -- the same shape for every account."""
    if not steps:
        return []
    first, last = steps[0], steps[-1]
    sorted_step = next((s for s in steps if s.agent == "triage"), None)
    outline = [("intake", "received", "Your complaint was received.", first.duration_ms, first.at)]
    if sorted_step is not None:
        outline.append(("triage", "classified", f"Sorted as {row.category.replace('_', ' ')}.",
                        sorted_step.duration_ms, sorted_step.at))
    rest = sum(s.duration_ms for s in steps if s is not first and s is not sorted_step)
    agent, action, summary = _PUBLIC_OUTCOME[row.route]
    outline.append((agent, action, summary, rest, last.at))
    return [{"seq": n, "agent": a, "action": act, "summary": text, "detail": {}, "duration_ms": ms, "at": iso_z(at)}
            for n, (a, act, text, ms, at) in enumerate(outline, start=1)]


def _public_calls(calls: list[SupportToolCallRow]) -> list[dict[str, Any]]:
    """Only the call that fixed the case, by name and status -- nothing about how it was decided."""
    return [{"id": c.id, "tool": c.tool, "args": {}, "result": None, "status": c.status, "policy": None,
             "at": iso_z(c.at), "decided_by": None}
            for c in calls if c.tool not in _BOOKKEEPING_TOOLS and c.status in ("ok", "approved")][:1]


def detail(session: Session, row: SupportComplaintRow, *, public: bool = False,
           policy: SupportPolicy | None = None) -> dict[str, Any]:
    """``{ complaint, steps, tool_calls, messages }``, each list in the order it happened."""
    steps = list(session.scalars(
        select(SupportStepRow).where(SupportStepRow.complaint_id == row.id).order_by(SupportStepRow.seq)
    ).all())
    calls = list(session.scalars(
        select(SupportToolCallRow)
        .where(SupportToolCallRow.complaint_id == row.id)
        .order_by(SupportToolCallRow.at, SupportToolCallRow.id)
    ).all())
    messages = session.scalars(
        select(SupportMessageRow)
        .where(SupportMessageRow.complaint_id == row.id)
        .order_by(SupportMessageRow.at, SupportMessageRow.id)
    ).all()
    return {
        "complaint": complaint_out(session, row, public=public, policy=policy),
        "steps": _public_steps(row, steps) if public else [step_out(s) for s in steps],
        "tool_calls": _public_calls(calls) if public else [tool_call_out(c) for c in calls],
        "messages": [message_out(m, public=public) for m in messages],
    }


def duplicate_view(row: SupportComplaintRow, *, msisdn_masked: str) -> dict[str, Any]:
    """What a PUBLIC caller gets for a submission the dedupe matched: the case's reference and
    status, and nothing else of it. Every key of the contract's shape is present (so a client
    reads it like any detail); the rest are empty, and the masked number is the caller's own."""
    complaint = {key: None for key in (
        "id", "created_at", "updated_at", "channel", "language", "subject", "body", "category", "urgency",
        "sentiment", "route", "outcome", "confidence", "escalation", "reply", "linked_incident", "sla_due_at")}
    complaint.update({
        "ref": row.ref,
        "status": "escalated" if row.status == "awaiting_approval" else row.status,
        "customer": {"name": None, "msisdn_masked": msisdn_masked, "account_ref": None},
        "citations": [],
    })
    return {"complaint": complaint, "steps": [], "tool_calls": [], "messages": []}


def _counts(session: Session, operator_id: str, column: Any) -> dict[str, int]:
    rows = session.execute(
        select(column, func.count()).where(SupportComplaintRow.operator_id == operator_id).group_by(column)
    ).all()
    return {str(key): int(count) for key, count in rows}


def list_complaints(
    session: Session,
    operator_id: str,
    *,
    status: str | None = None,
    route: str | None = None,
    category: str | None = None,
    q: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
) -> dict[str, Any]:
    """The queue, newest first, plus counts over ALL the operator's complaints (for filter chips)."""
    stmt = select(SupportComplaintRow).where(SupportComplaintRow.operator_id == operator_id)
    if status:
        stmt = stmt.where(SupportComplaintRow.status == status)
    if route:
        stmt = stmt.where(SupportComplaintRow.route == route)
    if category:
        stmt = stmt.where(SupportComplaintRow.category == category)
    if q and q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(
            SupportComplaintRow.ref.ilike(like),
            SupportComplaintRow.subject.ilike(like),
            SupportComplaintRow.body.ilike(like),
        ))
    rows = session.scalars(
        stmt.order_by(SupportComplaintRow.created_at.desc(), SupportComplaintRow.ref.desc())
        .limit(max(1, min(limit, MAX_LIST_LIMIT)))
    ).all()
    return {
        "items": [complaint_out(session, row) for row in rows],
        "counts": {
            "by_status": _counts(session, operator_id, SupportComplaintRow.status),
            "by_route": _counts(session, operator_id, SupportComplaintRow.route),
            "by_category": _counts(session, operator_id, SupportComplaintRow.category),
        },
    }


def metrics(session: Session, operator_id: str, *, hours: int, now: datetime) -> dict[str, Any]:
    """The desk's headline numbers over the last ``hours`` (0 = all time).

    ``resolution_rate`` is complaints closed without a person (answered or acted on) over all;
    ``escalation_rate`` is complaints that EVER went to a person over all, so a case a person
    later resolved still counts as escalated. ``escalated`` counts the cases with a person now
    (``escalated`` or ``in_progress``); ``awaiting_approval`` is separate, as in the contract.
    """
    stmt = select(SupportComplaintRow).where(SupportComplaintRow.operator_id == operator_id)
    if hours > 0:
        stmt = stmt.where(SupportComplaintRow.created_at >= now - timedelta(hours=hours))
    rows = session.scalars(stmt).all()
    total = len(rows)

    def count(predicate: Any) -> int:
        return sum(1 for r in rows if predicate(r))

    auto = count(lambda r: r.outcome == "auto_resolved")
    acted = count(lambda r: r.outcome == "action_completed")
    by_category = {category: 0 for category in CATEGORIES}
    for r in rows:
        by_category[r.category] = by_category.get(r.category, 0) + 1
    return {
        "total": total,
        "auto_resolved": auto,
        "action_completed": acted,
        "escalated": count(lambda r: r.status in ("escalated", "in_progress")),
        "human_resolved": count(lambda r: r.outcome == "human_resolved"),
        "awaiting_approval": count(lambda r: r.status == "awaiting_approval"),
        "resolution_rate": round((auto + acted) / total, 4) if total else 0.0,
        "escalation_rate": round(count(lambda r: r.escalated_at is not None) / total, 4) if total else 0.0,
        "median_handle_ms": int(statistics.median(r.handle_ms for r in rows)) if rows else 0,
        "by_category": by_category,
    }
