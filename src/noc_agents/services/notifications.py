"""The notification centre's inbox: what needs a person's eyes, newest first.

The live stream (``/api/v1/stream/events``) carries the moment something happens, but only
the last few frames survive a reload, so a bell that listened to the stream alone would come
back empty after every refresh. This rollup rebuilds the same attention items from durable
state instead, so the inbox after a reload says what it said before it:

``p1_open``
    An open P1 ticket (not restored, closed or cancelled) that BECAME P1 inside the window, stamped
    with that moment. Usually that is when it was opened. A ticket the Severity agent rated lower
    and a person raised to P1 when approving its broadcast (``main._apply_overrides``) became P1
    at that decision, so it arrives then, unread, however old the ticket is. The override is not
    recorded anywhere else, so it is read back from the Severity step's own rating.
``restore_breached``
    An open ticket whose restore clock (``sla_restore_due``) ran out inside the window and is
    not restored yet.
``approval_waiting``
    A card still waiting for a decision (PENDING or CLAIMED), however long ago it was raised: a
    card a day old is more urgent than a new one, not less, so the window does not apply. A signed-in
    principal sees only the card types §9.3 lets that role decide (``hitl_deciders``), exactly as
    ``/api/v1/hitl/pending`` filters; the demo (AUTH_DISABLED) sees every card, as that route does.
``run_failed``
    An agent run that FAILED inside the window: the step it stopped at and its error summary.

Read-only: nothing here writes, enqueues or sends. Every query goes through ``api.deps._owned`` /
``_operator_scoped``, so another operator's tickets, cards and runs never reach the inbox (the
counts carry no row id for a reviewer to notice is foreign). Each source is counted in SQL and
fetched newest first with the limit in SQL, so a crash-looping graph with thousands of failed
runs costs one count and ``limit`` rows. Every item has the same keys, null where a key does not
apply, and every timestamp is a ``Z`` string at seconds precision (``services.clock.iso_z``).
The frontend composes the words; this payload carries facts only.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from noc_agents.api.deps import HITL_DECIDERS, _operator_scoped, _owned, hitl_deciders
from noc_agents.db.models import AgentRunRow, AgentRunStepRow, HitlTaskRow, IncidentRow, utcnow
from noc_agents.services.clock import iso_z, to_utc
from noc_agents.services.hitl import GATING_TASK_TYPE

DEFAULT_WINDOW_HOURS = 24
MAX_WINDOW_HOURS = 24 * 7
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

#: Every item carries exactly these keys (pinned by tests/unit/test_notifications.py).
ITEM_KEYS: tuple[str, ...] = (
    "id",
    "kind",
    "group",
    "at",
    "incident_id",
    "incident_number",
    "priority",
    "site_name",
    "region_code",
    "task_id",
    "task_type",
    "graph",
    "node",
    "error",
)

#: Which tab of the inbox each kind belongs to: an alarm, a person's decision, an agent.
GROUP_OF: dict[str, str] = {
    "p1_open": "alarm",
    "restore_breached": "alarm",
    "approval_waiting": "person",
    "run_failed": "agent",
}

_NOT_OPEN = ("RESTORED", "CLOSED", "CANCELLED")
_WAITING = ("PENDING", "CLAIMED")
#: A run's error summary is a sentence for a person, not a traceback; anything longer is cut.
ERROR_CHARS = 240
#: The Severity step's own rating, as it writes it ("priority=P2 mpesa_risk=True").
_RATED = re.compile(r"\bpriority=(P[1-4])\b")


def _naive_utc(dt: datetime) -> datetime:
    """Storage is naive UTC; compare like with like."""
    aware = to_utc(dt)
    assert aware is not None
    return aware.replace(tzinfo=None)


def _item(kind: str, at: datetime | None, item_id: str, **facts: Any) -> dict[str, Any]:
    out: dict[str, Any] = {key: None for key in ITEM_KEYS}
    out.update({"id": item_id, "kind": kind, "group": GROUP_OF[kind], "at": iso_z(at)})
    for key, value in facts.items():
        if key not in out:
            raise KeyError(key)  # a typo would otherwise add an unpinned key
        out[key] = value
    return out


def _incident_facts(inc: IncidentRow | None) -> dict[str, Any]:
    if inc is None:
        return {}
    return {
        "incident_id": inc.id,
        "incident_number": inc.incident_number,
        "priority": inc.priority,
        "site_name": inc.site_name or inc.site_id,
        "region_code": inc.region_code,
    }


def _count(session: Session, stmt) -> int:
    return int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)


def _became_p1_at(session: Session, inc: IncidentRow) -> datetime:
    """When ``inc`` became P1: when it was opened, unless the agents rated it lower, in which case
    when a person approved its broadcast (the only path that changes a priority). Falls back to
    the ticket's last update when the rating says lower but no approval is on record."""
    rated = session.scalar(
        _operator_scoped(
            select(AgentRunStepRow.output_summary).join(AgentRunRow, AgentRunRow.id == AgentRunStepRow.run_id),
            AgentRunRow,
        )
        .where(AgentRunRow.incident_id == inc.id, AgentRunStepRow.node_name == "SEVERITY")
        .order_by(AgentRunRow.started_at.asc(), AgentRunStepRow.seq.asc())
        .limit(1)
    )
    match = _RATED.search(rated or "")
    if not match or match.group(1) == "P1":
        return inc.created_at
    decided = session.scalar(
        _owned(HitlTaskRow)
        .where(
            HitlTaskRow.incident_id == inc.id,
            HitlTaskRow.task_type == GATING_TASK_TYPE,
            HitlTaskRow.status == "APPROVED",
            HitlTaskRow.resolved_at.is_not(None),
        )
        .order_by(HitlTaskRow.resolved_at.desc())
        .limit(1)
    )
    if decided is not None and decided.resolved_at is not None:
        return decided.resolved_at
    return inc.updated_at or inc.created_at


def _decidable_by(role: str | None):
    """The SQL filter for the card types ``role`` may decide: ``hitl_deciders`` as a WHERE."""
    known = [t for t, roles in HITL_DECIDERS.items() if role in roles]
    clauses = [HitlTaskRow.task_type.in_(known)] if known else []
    if role in hitl_deciders(None):  # a type §9.3 does not name takes row 2's deciders
        clauses.append(HitlTaskRow.task_type.not_in(list(HITL_DECIDERS)))
    return or_(*clauses) if clauses else HitlTaskRow.id.is_(None)


def _incident_of(session: Session, cache: dict[str, IncidentRow | None], incident_id: str | None) -> IncidentRow | None:
    if not incident_id:
        return None
    if incident_id not in cache:
        cache[incident_id] = session.scalar(_owned(IncidentRow).where(IncidentRow.id == incident_id))
    return cache[incident_id]


def _p1_items(session: Session, since: datetime, now: datetime) -> tuple[int, list[dict[str, Any]]]:
    """Open P1 tickets that became P1 in the window. A ticket raised to P1 later was updated then,
    so "opened or updated in the window" finds every candidate; the moment it became P1 decides."""
    items = []
    for inc in session.scalars(
        _owned(IncidentRow).where(
            IncidentRow.priority == "P1",
            IncidentRow.status.not_in(_NOT_OPEN),
            or_(IncidentRow.created_at >= since, IncidentRow.updated_at >= since),
        )
    ):
        became = _became_p1_at(session, inc)
        if since <= became <= now:
            items.append(_item("p1_open", became, f"p1:{inc.id}", **_incident_facts(inc)))
    return len(items), items


def _breach_items(session: Session, since: datetime, now: datetime, limit: int) -> tuple[int, list[dict[str, Any]]]:
    """Restore clocks that ran out in the window on tickets still open."""
    breached = _owned(IncidentRow).where(
        IncidentRow.sla_restore_due.is_not(None),
        IncidentRow.sla_restore_due >= since,
        IncidentRow.sla_restore_due <= now,
        IncidentRow.status.not_in(_NOT_OPEN),
    )
    rows = session.scalars(breached.order_by(IncidentRow.sla_restore_due.desc()).limit(limit))
    items = [_item("restore_breached", inc.sla_restore_due, f"sla:{inc.id}", **_incident_facts(inc)) for inc in rows]
    return _count(session, breached), items


def _card_items(
    session: Session, incidents: dict[str, IncidentRow | None], role: str | None, authenticated: bool, limit: int
) -> tuple[int, list[dict[str, Any]]]:
    """Every card still waiting for a decision, whatever its age; a signed-in role sees its own kinds."""
    waiting = _owned(HitlTaskRow).where(HitlTaskRow.status.in_(_WAITING))
    if authenticated:
        waiting = waiting.where(_decidable_by(role))
    items = [
        _item(
            "approval_waiting",
            task.created_at,
            f"hitl:{task.id}",
            task_id=task.id,
            task_type=task.task_type,
            **_incident_facts(_incident_of(session, incidents, task.incident_id)),
        )
        for task in session.scalars(waiting.order_by(HitlTaskRow.created_at.desc()).limit(limit))
    ]
    return _count(session, waiting), items


def _short_error(summary: str | None) -> str | None:
    error = (summary or "").strip() or None
    if error and len(error) > ERROR_CHARS:
        error = error[: ERROR_CHARS - 1].rstrip() + "…"
    return error


def _failed_run_items(
    session: Session, incidents: dict[str, IncidentRow | None], since: datetime, limit: int
) -> tuple[int, list[dict[str, Any]]]:
    """Agent runs that failed in the window, newest first."""
    failed = _owned(AgentRunRow).where(AgentRunRow.status == "FAILED", AgentRunRow.started_at >= since)
    stamp = func.coalesce(AgentRunRow.finished_at, AgentRunRow.started_at)
    items = [
        _item(
            "run_failed",
            run.finished_at or run.started_at,
            f"run:{run.id}",
            graph=run.graph_name,
            node=run.current_node,
            error=_short_error(run.error_summary),
            **_incident_facts(_incident_of(session, incidents, run.incident_id)),
        )
        for run in session.scalars(failed.order_by(stamp.desc()).limit(limit))
    ]
    return _count(session, failed), items


def notifications(
    session: Session,
    *,
    role: str | None = None,
    authenticated: bool = False,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    limit: int = DEFAULT_LIMIT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The inbox for the active operator: ``counts`` per group (before the limit) and ``items``."""
    now = _naive_utc(now) if now is not None else _naive_utc(utcnow())
    since = now - timedelta(hours=window_hours)
    incidents: dict[str, IncidentRow | None] = {}

    p1_count, p1 = _p1_items(session, since, now)
    breach_count, breaches = _breach_items(session, since, now, limit)
    card_count, cards = _card_items(session, incidents, role, authenticated, limit)
    run_count, runs = _failed_run_items(session, incidents, since, limit)

    counts = {"alarm": p1_count + breach_count, "person": card_count, "agent": run_count}
    # Newest first; the id breaks a tie so the order is stable between two reads.
    items = sorted([*p1, *breaches, *cards, *runs], key=lambda it: (it["at"] or "", it["id"]), reverse=True)
    return {
        "generated_at": iso_z(now),
        "window_hours": window_hours,
        "since": iso_z(since),
        "counts": counts,
        "total": sum(counts.values()),
        "items": items[:limit],
    }
