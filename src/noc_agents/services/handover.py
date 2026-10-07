"""Shift handover: the package, and the approval gate that now stands in front of it.

``build_handover`` is unchanged in what it returns — ``watch_count`` and every other key
of that dict are a contract (``tests/system/test_api_system.py``, the demo script, the
frontend). Only the query it runs was lifted into :func:`open_incidents` so the gate can
pick the same rows without building the package twice.

The gate (spec §5.3.12, defect #30). Until now ``POST /api/v1/shifts/handover`` queued the
mail and the request drained it: the handover left with no human ever seeing it. With
``HANDOVER_REQUIRES_HITL`` true — **the default** — the route instead

1. builds a ``NocAlert(msg_type=ACK, scope=INTERNAL, audience=NOC_SHIFT)`` for the package,
2. raises an ``APPROVE_HANDOVER`` ``HitlTaskRow`` carrying that envelope, and
3. enqueues the email **HELD** with ``requires_hitl=1`` and no approval.

Nothing else is needed to keep it in: ``outbox.drain_once`` only ever claims ``PENDING``
rows, and ``outbox.dispatch`` refuses (``REJECTED_UNAPPROVED``) any channel row whose
envelope requires approval and has none. This module adds no second check — it relies on
that one, which is the one every other channel already relies on.

:func:`release_handover` is the approve side: HELD → PENDING for the rows **this task**
gates, addressed by ``outbox.hitl_task_id`` alone. It deliberately does not use
``outbox.release_held``, whose blast radius is the incident: that would SUPPRESS a
broadcast draft still waiting for its own approval on the anchor incident.

**The anchor incident.** When this gate was written ``hitl_tasks.incident_id`` was NOT NULL
and operator ownership of a task was derived by joining it to ``incidents``
(``api.deps._OWNED_VIA_INCIDENT``), so a task that belonged to no incident could be neither
stored nor fetched. The handover task was therefore anchored to the top watchlist incident and
says what it is really about in ``entity_type="handover"`` / ``entity_id=<shift id>``. Two
consequences were deliberate and documented here rather than hidden: with **no** open incident
there is no anchor, so no task can be raised and the handover is refused rather than sent
unapproved (fail closed); and the task is not fed to ``sync_incident_hitl_scalars`` because
the anchor incident does not require HITL — the handover does.

Since schema_version 8 the schema no longer forces any of that: ``incident_id`` is nullable
and a task is owned through its own ``hitl_tasks.operator_id`` (``db/migrate.py``, "THE ONE
EXCEPTION"; the maintenance lane dropped its copy of this workaround at the same time). The
anchor is KEPT here on purpose, and not out of inertia:

* ``main.py``'s approve route releases the HELD handover mail only inside ``if inc:`` — it
  reaches ``release_handover`` through the task's incident. A handover task with no incident
  would be approved and the mail would sit HELD for ever;
* :func:`handover_alert` builds the ACK envelope from the anchor incident;
* ``tests/integration/test_handover_hitl.py`` pins the fail-closed answer with nothing open.

So un-anchoring the handover is a change to ``main.py`` and to those tests, not to this module
alone, and it is left for its own reviewed commit. Nothing here had to change for v8: the task
is still written with ``incident_id`` only, and ``db.models._own_hitl_task`` derives its
``operator_id`` from that incident at insert (``tests/unit/test_hitl_ownership.py``).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from noc_agents.config import OperatorConfig
from noc_agents.db.models import HitlTaskRow, IncidentRow, OutboxRow, new_id, utcnow
from noc_agents.domain.alerts import INCIDENT_NUMBER_PATTERN, AudienceSpec, Content, NocAlert
from noc_agents.domain.enums import HitlTaskType
from noc_agents.orchestrator import outbox
from noc_agents.services.clock import fmt_eat
from noc_agents.services.lifecycle import NOT_OPEN_STATUSES
from noc_agents.services.alerts import build_alert
from noc_agents.services.hitl import envelope_payload
from noc_agents.services.notify import dispatch_handover_email
from noc_agents.services.shifts import current_shift

#: ``HANDOVER_REQUIRES_HITL`` — spec §5.3.12 says default **true**, so only an explicit
#: falsey value turns the gate off (an unset or misspelt value keeps the gate on).
HANDOVER_HITL_FLAG = "HANDOVER_REQUIRES_HITL"
_FALSE = {"0", "false", "no", "off"}

#: The enum member's value, not a bare literal: the §5.2 import-time check validates members,
#: so a typo here is now an AttributeError at import rather than a card nobody can route
#: (CONFORMANCE A-06). ``.value`` keeps the stored string byte-identical.
HANDOVER_TASK_TYPE = HitlTaskType.APPROVE_HANDOVER.value
#: ``hitl_tasks.entity_type`` for these rows: what the task is really about (§7.5).
HANDOVER_ENTITY_TYPE = "handover"
#: The raiser. A principal-shaped string that can never equal a human's name, so the
#: raiser ≠ approver rule (§6.5) never blocks the supervisor who pressed the button.
HANDOVER_RAISER = "agent:ShiftHandoverAgent"
#: §6.1 audience for the ACK envelope. The outbox payload keeps today's ``HANDOVER``
#: audience label so the mail that finally leaves is byte-identical to today's.
HANDOVER_AUDIENCE = "NOC_SHIFT"
HANDOVER_RECIPIENTS_REF = "audiences.NOC_SHIFT"


def handover_requires_hitl() -> bool:
    """``HANDOVER_REQUIRES_HITL`` — **default true** (spec §5.3.12)."""
    return (os.getenv(HANDOVER_HITL_FLAG) or "").strip().lower() not in _FALSE


def open_incidents(session: Session, cfg: OperatorConfig) -> list[IncidentRow]:
    """This operator's open incidents (``NOT_OPEN_STATUSES`` excluded), P1 first then oldest."""
    rows = session.scalars(
        select(IncidentRow)
        .where(
            IncidentRow.operator_id == cfg.operator_id,
            IncidentRow.status.not_in(NOT_OPEN_STATUSES),
        )
        .order_by(IncidentRow.priority.asc(), IncidentRow.created_at.asc())
    ).all()
    # Sort P1 first manually
    order = {"P1": 0, "P2": 1, "P3": 2, "P4": 3}
    return sorted(rows, key=lambda r: (order.get(r.priority, 9), r.created_at))


def watchlist(open_rows: list[IncidentRow]) -> list[IncidentRow]:
    """The rows the package prints: priority failures, else the ten oldest open tickets."""
    watch = [r for r in open_rows if r.priority in ("P1", "P2") or r.mpesa_risk or r.problem_id]
    return watch if watch else open_rows[:10]


_ENVELOPE_NUMBER = re.compile(INCIDENT_NUMBER_PATTERN)


def anchor_incident(session: Session, cfg: OperatorConfig) -> IncidentRow | None:
    """The incident an ``APPROVE_HANDOVER`` task hangs off, or None when none can.

    The top watchlist row whose number the §6.1 envelope accepts: the most severe open ticket
    the outgoing shift is handing over. A ticket numbered in another style (a dated
    ``ATL-20260916-00001``, or a row written before inc9) cannot head the envelope, which
    would refuse it, so the next one down anchors instead; with none left the caller answers
    NOT_QUEUED. It is a **scoping** anchor (see the module docstring), not the subject.
    """
    watch = watchlist(open_incidents(session, cfg))
    for row in watch:
        if _ENVELOPE_NUMBER.match(row.incident_number or ""):
            return row
    return None


def build_handover(session: Session, cfg: OperatorConfig) -> dict:
    tz = ZoneInfo(cfg.timezone)
    now = datetime.now(tz)
    st = current_shift(cfg, now)
    open_rows = open_incidents(session, cfg)

    watch = watchlist(open_rows)

    n_p1 = sum(1 for r in open_rows if r.priority == "P1")
    n_p2 = sum(1 for r in open_rows if r.priority == "P2")
    subject = (
        f"[{cfg.operator_id.upper()}] [{st.upper()} shift] NOC Handover "
        f"{now.strftime('%Y-%m-%d')} EAT — {n_p1} P1 / {n_p2} P2 open"
    )
    lines = [
        subject,
        "",
        f"Operator: {cfg.display_name}",
        f"Generated: {now.strftime('%Y-%m-%d %H:%M')} EAT",
        f"Outgoing shift: {st.upper()}",
        "",
        "WATCHLIST (priority failures — own and chase):",
        "Time | INC | Pri | Site | Type | Region | Domain | Owner | Status | M-PESA",
        "-" * 100,
    ]
    for r in watch:
        lines.append(
            f"{fmt_eat(r.created_at, '%Y-%m-%d %H:%M')} | {r.incident_number} | {r.priority} | "
            f"{r.site_id} | {r.site_type} | {r.region_code} | {r.failure_domain} | "
            f"{r.assignee_name} | {r.status} | {'Y' if r.mpesa_risk else 'N'}"
        )
    lines.extend(
        [
            "",
            "Instructions for incoming shift:",
            "1. Confirm P1/P2 owners responded within SLA note interval.",
            "2. Do not rely on phone calls for status — use ticket notes + Mission Control.",
            "3. Escalate silent MSPs via HITL reassignment if needed.",
            "",
            f"Distribution: {', '.join(cfg.shifts[st].distribution_list if st in cfg.shifts else [])}",
        ]
    )
    body = "\n".join(lines)
    return {
        "subject": subject,
        "body": body,
        "shift": st,
        "watch_count": len(watch),
        "open_total": len(open_rows),
        "n_p1": n_p1,
        "n_p2": n_p2,
        "incidents": [
            {
                "incident_number": r.incident_number,
                "priority": r.priority,
                "site_id": r.site_id,
                "owner": r.assignee_name,
                "status": r.status,
                "region_code": r.region_code,
                "mpesa_risk": r.mpesa_risk,
            }
            for r in watch
        ],
    }


# ---------------------------------------------------------------------------------------
# The approval gate (spec §5.3.12, defect #30)
# ---------------------------------------------------------------------------------------


def handover_alert(
    inc: IncidentRow,
    cfg: OperatorConfig,
    package: dict,
    *,
    hitl_task_id: str | None = None,
    sent: datetime | None = None,
) -> NocAlert:
    """The §6.1 envelope for a handover: ``ACK`` / ``INTERNAL`` / audience ``NOC_SHIFT``.

    Built from the anchor incident by ``services.alerts.build_alert`` so every deterministic
    field (timing, area, facts, sender) comes from the one builder, then three things are
    replaced because they belong to the handover rather than to that incident:

    * ``content["en"]`` — the package's subject and body (capped to the §6.1 field limits;
      the mail that leaves carries the full body from the outbox payload);
    * ``governance.requires_hitl`` — always true here. ``build_alert`` would otherwise let a
      P3/P4 anchor stamp ``approved_by="policy:…"`` on a message no policy has approved;
    * ``governance.hitl_task_id`` — the task that gates it.

    ``classification`` and ``area`` still describe the anchor incident. That is the honest
    consequence of anchoring (module docstring), not an accident: the envelope says which
    ticket's shift is being handed over.
    """
    alert = build_alert(
        inc,
        cfg,
        msg_type="ACK",
        scope="INTERNAL",
        audiences=[
            AudienceSpec(
                audience=HANDOVER_AUDIENCE,
                channels=["EMAIL"],
                language="en",
                recipients_ref=HANDOVER_RECIPIENTS_REF,
            )
        ],
        sent=sent,
        hitl_task_id=hitl_task_id,
    )
    alert.content = {
        "en": Content(
            headline=str(package["subject"])[:160],
            body=str(package["body"])[:2000],
            instruction=None,
        )
    }
    alert.governance.requires_hitl = True
    alert.governance.approved_by = None
    alert.governance.approved_at = None
    return alert


def queue_handover(
    session: Session,
    cfg: OperatorConfig,
    package: dict,
    *,
    shift_id: str,
) -> dict:
    """Raise the ``APPROVE_HANDOVER`` task and queue the email HELD. Sends nothing.

    Runs inside the caller's transaction: the task, the outbox row and anything else the
    request writes commit together, and a rollback takes the whole handover with it.

    Returns the ``hitl`` block of the route's response::

        {"required": True, "task_id": ..., "alert_id": ..., "outbox_id": ...,
         "status": "HELD", "blocked_reason": None}

    ``status="NOT_QUEUED"`` with a ``blocked_reason`` is the fail-closed answer when there
    is no open incident to anchor the task on: no task is raised (the module docstring says
    why this gate still anchors), so no approval is possible, so nothing is queued — rather
    than queueing a row nobody could ever release.
    """
    anchor = anchor_incident(session, cfg)
    if anchor is None and watchlist(open_incidents(session, cfg)):
        # Tickets are open, but none is numbered in the style the alert envelope accepts.
        return {
            "required": True,
            "task_id": None,
            "alert_id": None,
            "outbox_id": None,
            "status": "NOT_QUEUED",
            "blocked_reason": (
                "no open ticket is numbered in the style the alert envelope accepts (INC and six "
                "digits), so none can anchor an APPROVE_HANDOVER task; nothing queued and nothing sent"
            ),
        }
    if anchor is None:
        return {
            "required": True,
            "task_id": None,
            "alert_id": None,
            "outbox_id": None,
            "status": "NOT_QUEUED",
            # The true reason, not the schema's: since schema_version 8 a task may have no
            # incident, but the approve route releases handover mail only through the task's
            # incident, so a task raised without one could be approved and never sent.
            "blocked_reason": (
                "no open incident to anchor an APPROVE_HANDOVER task on: approval releases the "
                "handover mail only through the task's incident, so a task without one could be "
                "approved but never sent; nothing queued and nothing sent"
            ),
        }

    task = HitlTaskRow(
        id=new_id(),
        incident_id=anchor.id,
        task_type=HANDOVER_TASK_TYPE,
        status="PENDING",
        created_by=HANDOVER_RAISER,
        entity_type=HANDOVER_ENTITY_TYPE,
        entity_id=shift_id,
    )
    alert = handover_alert(anchor, cfg, package, hitl_task_id=task.id)
    task.proposed_payload = {
        "shift_id": shift_id,
        "shift": package.get("shift"),
        "subject": package.get("subject"),
        "body": package.get("body"),
        "watch_count": package.get("watch_count"),
        "open_total": package.get("open_total"),
        "anchor_incident_number": anchor.incident_number,
        "envelope": envelope_payload(alert),  # the key services.hitl.stored_envelope reads
    }
    session.add(task)
    session.flush()

    # The row is created by notify.dispatch_handover_email so the payload shape and the
    # idempotency key keep ONE home, then held before this transaction commits — no drain
    # runs inside a transaction, so the PENDING moment is not observable by any dispatcher.
    queued = dispatch_handover_email(
        session,
        package["subject"],
        package["body"],
        operator_id=cfg.operator_id,
        shift_id=shift_id,
    )
    row = session.get(OutboxRow, queued["outbox_id"])
    row.status = outbox.HELD
    row.requires_hitl = 1
    row.approved_by = None
    row.approved_at = None
    row.hitl_task_id = task.id
    row.alert_id = alert.alert_id
    row.envelope_json = json.dumps(envelope_payload(alert))
    session.flush()
    return {
        "required": True,
        "task_id": task.id,
        "alert_id": alert.alert_id,
        "outbox_id": row.id,
        "status": row.status,
        "blocked_reason": None,
    }


def release_handover(session: Session, task: HitlTaskRow, *, approved_by: str, approved_at: datetime) -> int:
    """HELD → PENDING for the outbox rows **this** task gates. Returns how many moved.

    Addressed by ``hitl_task_id`` only, never by incident: the anchor incident may have its
    own HELD broadcast drafts waiting for their own approval, and ``outbox.release_held``
    would suppress them. Transmits nothing — the drain after the caller's commit does that.
    """
    return session.execute(
        update(OutboxRow)
        .where(OutboxRow.hitl_task_id == task.id, OutboxRow.status == outbox.HELD)
        .values(status=outbox.PENDING, approved_by=approved_by, approved_at=approved_at, updated_at=utcnow())
    ).rowcount
