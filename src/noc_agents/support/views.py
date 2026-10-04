"""The desk's read side: the contract's JSON shapes, the queue with its counts, and the metrics.

Every function takes the operator id and puts it in the WHERE clause, the codebase's rule for
operator-owned reads (``api/deps``): another operator's complaint is not filtered out after the
fetch, it is never fetched.

**Two views of a complaint.** Staff (anyone holding a support read role, and everyone in the
demo with ``AUTH_DISABLED=true``) see the full trace. The PUBLIC view -- what an anonymous
caller of ``POST /complaints`` gets back once auth is on -- has the same shape with the
account-derived parts blanked: step details, tool arguments and results, the account holder's
name and the account reference. Without that, the public form would be an oracle: type any
number, read that line's owner, balances and recent M-PESA transfers from the action agent's
``lookup_account`` result. The customer still gets the reply, the reference, the citations and
the step summaries, which are written to carry no account data.
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


def complaint_out(session: Session, row: SupportComplaintRow, *, public: bool = False) -> dict[str, Any]:
    """The contract's ``Complaint``."""
    escalation = None
    if row.escalation_reason_code:
        escalation = {
            "reason_code": row.escalation_reason_code,
            "reason": row.escalation_reason,
            "at": iso_z(row.escalated_at),
            "claimed_by": row.claimed_by,
        }
    return {
        "id": row.id,
        "ref": row.ref,
        "created_at": iso_z(row.created_at),
        "updated_at": iso_z(row.updated_at),
        "channel": row.channel,
        "customer": {
            "name": row.customer_name if public else (row.customer_name or row.account_holder),
            "msisdn_masked": row.msisdn_masked,
            "account_ref": None if public else row.account_ref,
        },
        "language": row.language,
        "subject": row.subject,
        "body": row.body,
        "category": row.category,
        "urgency": row.urgency,
        "sentiment": row.sentiment,
        "route": row.route,
        "status": row.status,
        "outcome": row.outcome,
        "confidence": row.confidence,
        "escalation": escalation,
        "reply": row.reply,
        "citations": _loads(row.citations_json, []),
        "linked_incident": _linked_incident(session, row),
        "sla_due_at": iso_z(row.sla_due_at),
    }


def step_out(row: SupportStepRow, *, public: bool = False) -> dict[str, Any]:
    return {
        "seq": row.seq,
        "agent": row.agent,
        "action": row.action,
        "summary": row.summary,
        "detail": {} if public else _loads(row.detail_json, {}),
        "duration_ms": row.duration_ms,
        "at": iso_z(row.at),
    }


def tool_call_out(row: SupportToolCallRow, *, public: bool = False) -> dict[str, Any]:
    return {
        "id": row.id,
        "tool": row.tool,
        "args": {} if public else _loads(row.args_json, {}),
        "result": None if public else _loads(row.result_json, None),
        "status": row.status,
        "policy": row.policy,
        "at": iso_z(row.at),
        "decided_by": row.decided_by,
    }


def message_out(row: SupportMessageRow) -> dict[str, Any]:
    return {"id": row.id, "author": row.author, "name": row.name, "body": row.body, "at": iso_z(row.at)}


def detail(session: Session, row: SupportComplaintRow, *, public: bool = False) -> dict[str, Any]:
    """``{ complaint, steps, tool_calls, messages }``, each list in the order it happened."""
    steps = session.scalars(
        select(SupportStepRow).where(SupportStepRow.complaint_id == row.id).order_by(SupportStepRow.seq)
    ).all()
    calls = session.scalars(
        select(SupportToolCallRow)
        .where(SupportToolCallRow.complaint_id == row.id)
        .order_by(SupportToolCallRow.at, SupportToolCallRow.id)
    ).all()
    messages = session.scalars(
        select(SupportMessageRow)
        .where(SupportMessageRow.complaint_id == row.id)
        .order_by(SupportMessageRow.at, SupportMessageRow.id)
    ).all()
    return {
        "complaint": complaint_out(session, row, public=public),
        "steps": [step_out(s, public=public) for s in steps],
        "tool_calls": [tool_call_out(c, public=public) for c in calls],
        "messages": [message_out(m) for m in messages],
    }


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
