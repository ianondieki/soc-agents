"""Outbox dead-letter view and retry (spec §10.4, §7.0.2, §6.6) -- conformance item C-10.

    GET  /api/v1/outbox?status=FAILED|DEAD&limit=    this operator's dead letters, newest first    platform readers
    POST /api/v1/outbox/{id}/retry                   {reason?}  re-queue ONE row                    admin

**What may be retried, decided from ``orchestrator/outbox.py``.** Exactly the two statuses the
view lists:

* ``FAILED``: a transient error (I/O, SMTP 4xx, HTTP 429/5xx) that used up ``max_attempts``, or
  a claim whose lease expired with no attempts left;
* ``DEAD``: an error the classifier called permanent. Permanent for the *machine*, which will
  not try again, but the cause is usually something a human fixes: credentials after an SMTP
  535, a DPIA/TIA filed after the paperwork gate refused, an address added to the operator
  profile after ``resolve_recipients`` refused.

Every transmitter re-asks its own questions on the next attempt (the approval gate, the
redaction backstop, the transfer register, ``EMAIL_DAILY_CAP``), so a retry re-asks them all
and answers none. Everything else is a 409, each for its own reason: ``SENT``/``DELIVERED``
already left, so a retry is the duplicate send; ``SUPPRESSED`` is a superseded draft or one a
human rejected, and sending it is defect #11; ``REJECTED_UNAPPROVED`` needs an approval, not a
retry; ``PENDING``/``HELD``/``CLAIMED`` are still in flight, and a CLAIMED row's lease belongs
to a drainer. Two FAILED/DEAD rows are refused as well: a channel row that requires a HITL
approval it does not have (a retry is not an approval), and a row whose payload housekeeping
archived under §9.4 (there is nothing left to send).

**No duplicate send.** The retry re-queues the SAME row: same id, same ``idempotency_key``,
same payload, same approval. No row is inserted, so the unique key never gets a second
holder, and the status change is a compare-and-set (``WHERE status IN (FAILED, DEAD)``), so two
admins clicking at once re-queue it once and the second gets a 409. It clears
``claimed_at``/``claimed_by`` (a terminal row keeps its last claim stamp, and ``_claim``'s CAS
would otherwise wait out the 120 s lease) and ``next_attempt_at``.

**``attempts`` is not reset.** It is the honest count of transmissions: the reg 41(2) transfer
register numbers its records by it ("attempt N"), so resetting it would write a second
"attempt 1" for the same row. A row whose automatic budget is spent therefore gets exactly one
attempt per retry, and fails straight back to FAILED on a transient error; a row with budget
left (DEAD on its first try) resumes with what it has. One human click never buys three
automatic resends of a multi-batch email, and ``notify.transmit_email`` re-sends every batch.

**The route transmits nothing.** The next drain does: the scheduler's ``outbox_dispatch`` job
every 5 s, the next producer's sync drain, or ``POST /api/v1/scheduler/run/outbox_dispatch``.
Draining here would walk around ``OUTBOX_DISPATCH_ENABLED=false`` during a §9.6 freeze, which
is exactly what CONFORMANCE A-10 found the manual scheduler run doing.

**The list is a summary, not the row.** Not returned: ``payload_json`` (email subject and body
with names in them, SMS text, ICS attendee addresses, ledger cells), ``envelope_json``,
``idempotency_key`` (a complaint reminder's key carries the assigned manager's name),
``approved_by`` (who approved what is the audit trail, which §9.3 gives to AUDIT_READERS
only), and ``provider_message_id`` (a ledger row's is a file path). ``last_error`` is returned
with e-mail addresses and MSISDNs scrubbed, because an ``SMTPRecipientsRefused`` quotes the
refused mailbox, and it is truncated.

**Scoping.** ``outbox`` carries ``operator_id``: the list goes through ``_owned``, the retry
through ``_get_owned`` (another operator's id is a 404, never a 403), and the compare-and-set
through ``_operator_scoped`` as well, so the write is bounded by the same clause as the read.
"""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import update

from noc_agents.api import auth
from noc_agents.api.auth import require_role
from noc_agents.api.deps import _actor, _get_owned, _operator_scoped, _owned, _settings
from noc_agents.db.models import AuditRow, OutboxRow, get_session, utcnow
from noc_agents.orchestrator import outbox
from noc_agents.services.housekeeping import ARCHIVED_KEY
from noc_agents.services.redaction import scrub_contacts

router = APIRouter(prefix="/api/v1", tags=["outbox_admin"])

#: §9.3 row "Templates status, outbox retry, scheduler run, MCP status, agents": read for the
#: four internal roles, all of it for admin. The same five roles as ``main.PLATFORM_READERS``,
#: spelled out here because a router may not import ``main`` (``routers/__init__`` explains
#: the cycle); ``tests/unit/test_outbox_admin.py`` pins the two tuples equal.
PLATFORM_READERS: tuple[str, ...] = ("noc_analyst", "shift_supervisor", "duty_manager", "management", "admin")
#: The action in that row: admin only.
RETRY_ROLES: tuple[str, ...] = ("admin",)

#: §10.4's dead-letter view, and the only statuses a retry accepts (module docstring).
DEAD_LETTER_STATUSES: tuple[str, ...] = (outbox.FAILED, outbox.DEAD)
RETRYABLE_STATUSES: frozenset[str] = frozenset(DEAD_LETTER_STATUSES)

#: Why each other status is refused, in the words the 409 carries.
_NOT_RETRYABLE: dict[str, str] = {
    outbox.SENT: "it was already transmitted, so a retry would send it a second time",
    outbox.DELIVERED: "it was already delivered, so a retry would send it a second time",
    outbox.SUPPRESSED: "it was superseded by a re-render or rejected at HITL, and a suppressed draft is never sent",
    outbox.REJECTED_UNAPPROVED: "it was refused for want of an approval; it needs a HITL approval, not a retry",
    outbox.PENDING: "it is already queued; the next drain sends it",
    outbox.HELD: "it is held behind an open HITL task; the approval releases it",
    outbox.CLAIMED: "a drainer is dispatching it right now under its lease",
}

_ERROR_CHARS = 500  # of last_error in a summary or an audit row: enough to recognise the failure


class RetryIn(BaseModel):
    """Optional: why the admin is retrying, kept on the audit row.

    No field names who retried: that is the principal. An unknown field is a 422 rather than
    silently dropped, the same rule as the template approval body.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str | None = None


def _payload(row: OutboxRow) -> dict[str, Any]:
    """The row's payload, or ``{}``. Tolerant on purpose: a view of broken rows must not 500 on one."""
    try:
        data = json.loads(row.payload_json or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _safe_error(text: str | None) -> str | None:
    """``last_error`` with e-mail addresses and MSISDNs replaced, truncated."""
    if not text:
        return text
    return scrub_contacts(text)[:_ERROR_CHARS]


def _retry_refusal(row: OutboxRow, payload: dict[str, Any]) -> str | None:
    """Why ``row`` may not be retried, or ``None`` when it may."""
    if row.status not in RETRYABLE_STATUSES:
        reason = _NOT_RETRYABLE.get(row.status, "only FAILED and DEAD rows are retried")
        return f"outbox row is {row.status}: {reason}"
    if row.kind in outbox.CHANNEL_KINDS and int(row.requires_hitl or 0) and row.approved_at is None:
        # The dispatcher would refuse it again (REJECTED_UNAPPROVED); refusing here says why.
        return f"this {row.kind} row requires a HITL approval it does not have; a retry is not an approval"
    if payload.get(ARCHIVED_KEY):
        return "its payload was archived under the §9.4 outbox retention, so there is nothing left to send"
    return None


def outbox_summary(row: OutboxRow) -> dict[str, Any]:
    """One row as the dead-letter view shows it. See the module docstring for what is left out."""
    payload = _payload(row)
    return {
        "id": row.id,
        "kind": row.kind,
        "status": row.status,
        "attempts": int(row.attempts or 0),
        "max_attempts": int(row.max_attempts or 0),
        "incident_id": row.incident_id,
        "incident_number": _text(payload.get("incident_number")),
        "audience": _text(payload.get("audience")),
        "run_id": row.run_id,
        "alert_id": row.alert_id,
        "hitl_task_id": row.hitl_task_id,
        "requires_hitl": bool(int(row.requires_hitl or 0)),
        "approved_at": row.approved_at,
        "provider": row.provider,
        "last_error": _safe_error(row.last_error),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "next_attempt_at": row.next_attempt_at,
        "retryable": _retry_refusal(row, payload) is None,
    }


def _statuses(raw: str | None) -> tuple[str, ...]:
    """``?status=`` as a tuple of dead-letter statuses; absent means both.

    Accepts one status or several separated by ``,`` or ``|`` (§10.4 writes ``FAILED|DEAD``),
    in any case. Anything outside the view is a 422 rather than an empty list: asking the
    dead-letter view for SENT rows is a mistake worth being told about.
    """
    if raw is None or not raw.strip():
        return DEAD_LETTER_STATUSES
    asked = [part.strip().upper() for part in re.split(r"[,|]", raw) if part.strip()]
    unknown = sorted(set(asked) - set(DEAD_LETTER_STATUSES))
    if unknown or not asked:
        raise HTTPException(422, f"status must be one or more of {list(DEAD_LETTER_STATUSES)}; got {unknown or raw!r}")
    return tuple(dict.fromkeys(asked))


@router.get("/outbox", dependencies=[Depends(require_role(*PLATFORM_READERS))])
def list_dead_letters(status: str | None = None, limit: int = Query(100, ge=1, le=500)) -> list[dict]:
    """This operator's FAILED/DEAD outbox rows, most recently failed first, as summaries."""
    wanted = _statuses(status)
    session = get_session()
    try:
        rows = session.scalars(
            _owned(OutboxRow)
            .where(OutboxRow.status.in_(wanted))
            .order_by(OutboxRow.updated_at.desc(), OutboxRow.id)
            .limit(limit)
        ).all()
        return [outbox_summary(row) for row in rows]
    finally:
        session.close()


@router.post("/outbox/{outbox_id}/retry")
def retry_outbox_row(
    outbox_id: str,
    body: RetryIn | None = None,
    principal: auth.Principal = Depends(require_role(*RETRY_ROLES)),
) -> dict:
    """Re-queue one FAILED/DEAD row as PENDING for the next drain. 404 foreign/unknown, 409 otherwise."""
    body = body or RetryIn()
    actor = _actor(principal, None)
    session = get_session()
    try:
        row = _get_owned(session, OutboxRow, outbox_id, what="outbox row")
        refusal = _retry_refusal(row, _payload(row))
        if refusal:
            raise HTTPException(409, refusal)
        previous_status = row.status
        now = utcnow()
        won = session.execute(
            _operator_scoped(update(OutboxRow), OutboxRow)
            .where(OutboxRow.id == row.id, OutboxRow.status.in_(RETRYABLE_STATUSES))
            .values(status=outbox.PENDING, claimed_at=None, claimed_by=None, next_attempt_at=None, updated_at=now)
        ).rowcount
        if won != 1:
            session.rollback()
            raise HTTPException(409, "the outbox row changed while it was being re-queued; reload it and decide again")
        session.add(
            AuditRow(
                operator_id=_settings().operator.operator_id,
                actor=actor,
                action="outbox.retried",
                entity_type="outbox",
                entity_id=row.id,
                rationale=(body.reason or "").strip(),
                payload_json=json.dumps(
                    {
                        "kind": row.kind,
                        "from": previous_status,
                        "to": outbox.PENDING,
                        "attempts": int(row.attempts or 0),
                        "max_attempts": int(row.max_attempts or 0),
                        "incident_id": row.incident_id,
                        "last_error": _safe_error(row.last_error),
                        "role": principal.role,
                    },
                    default=str,
                ),
            )
        )
        session.commit()
        session.refresh(row)
        return {"ok": True, "previous_status": previous_status, "outbox": outbox_summary(row)}
    finally:
        session.close()
