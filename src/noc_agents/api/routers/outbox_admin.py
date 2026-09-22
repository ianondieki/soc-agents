"""Outbox dead-letter view and retry (spec §10.4, §7.0.2, §6.6) -- conformance item C-10.

    GET  /api/v1/outbox?status=FAILED|DEAD&limit=    this operator's dead letters, newest first    platform readers
    POST /api/v1/outbox/{id}/retry                   {reason?}  re-queue ONE row                    admin

**Which statuses, decided from ``orchestrator/outbox.py``.** Only the two the view lists:

* ``FAILED``: a transient error (I/O, SMTP 4xx, HTTP 429/5xx) that used up ``max_attempts``, or
  a claim whose lease expired with no attempts left;
* ``DEAD``: an error the classifier called permanent. Permanent for the *machine*, which will
  not try again, but the cause is often something a human fixes: credentials after an SMTP 535,
  a DPIA/TIA filed after the paperwork gate refused, a mailbox corrected after a 550.

Every transmitter re-asks its own questions on the next attempt (the approval gate, the
redaction backstop, the transfer register, ``EMAIL_DAILY_CAP``), so a retry re-asks them all
and answers none. Every other status is a 409, each for its own reason: ``SENT``/``DELIVERED``
already left, so a retry is the duplicate send; ``SUPPRESSED`` is a superseded draft or one a
human rejected, and sending it is defect #11; ``REJECTED_UNAPPROVED`` needs an approval, not a
retry; ``PENDING``/``HELD``/``CLAIMED`` are still in flight, and a CLAIMED row's lease belongs
to a drainer. Two FAILED/DEAD rows are refused whoever produced them: a channel row that
requires a HITL approval it does not have (a retry is not an approval), and a row whose payload
housekeeping archived under §9.4 (there is nothing left to send).

**Which producers.** A status is not enough. Some producers recover a failed message
themselves, by queueing a NEW row under a new key, and those recoveries are safe only because
the failed row never transmits again. Retrying such a row as well is two recovery paths for one
message: that is how the Communications Authority received the same statutory notice twice
(review routes-correctness#1). So the retry is offered only where it is the ONE recovery the
producer has, and everywhere else the 409 names the producer's own path. ``producer_of`` reads
the producer from the idempotency key, the one column no later write changes:

    producer               key                                          retried here?
    incident broadcast     EMAIL:<incident|alert>:<audience>, SMS:...   yes
    shift ledger row       EXCEL_ROW:<incident>:<shift>                 yes
    PIR model draft        pir-llm-draft:<review>                       yes
    complaint reminder     complaint.reminder|<op>|<manager>|<day>      only on its own day
    maintenance invite     ics:<op>:<uid>:<METHOD>:<sequence>           only the newest for its uid
    regulatory notice      EMAIL:regulatory:<notice>[:<attempt>]        never
    shift handover         EMAIL:handover:<op>:<shift>:<uuid>           never
    anything else          --                                           never

* **Incident broadcasts** (``agents/broadcast.py``, ``notify.dispatch_incident_email``/``_sms``,
  the HITL release in ``services/hitl.py``, v1 and v2), **ledger rows** (``agents/ledger.py``)
  and **PIR drafts** (``services/pir.queue_llm_draft``) are keyed once per incident, alert,
  shift or review: queueing again returns the same row, so nothing else can ever recover a
  failed one. The approval stays on the row and the dispatcher re-checks it. A PIR draft's
  transmitter writes no text anywhere, so a second call cannot write a second draft.
* **Complaint reminders** (``services/complaints.send_due_reminders``) are keyed by manager and
  UTC day, and the job queues a fresh one each day with the complaints still overdue. Today's
  failed reminder has no other way out today; an earlier day's is superseded, and its list may
  name complaints that have since been closed.
* **Maintenance invites** (``services/ics.invite_idempotency_key``) are keyed by SEQUENCE: a
  reschedule or a cancellation is a new row. Retrying a superseded one sends an out-of-date
  event, and an old REQUEST arriving after the CANCEL can put a cancelled window back into a
  lenient calendar client. The newest invite for a uid has no other recovery.
* **Regulatory notices** (``services/regulatory.release_notice``) are re-released by the
  regulatory lane itself, through ``POST /api/v1/regulatory/{id}/send``, as a new
  attempt-numbered row. That path moves the failed attempt into ``dispatch_history`` (the M10
  evidence) and is safe only because the failed row stays dead. Retrying the old row sent a
  second notice to the Authority and overwrote the failed attempt in the evidence
  (routes-correctness#1, #3). Never retried here.
* **Shift handovers** (``notify.dispatch_handover_email``) get a fresh key on every
  ``POST /api/v1/shifts/handover``, which composes the handover from the shift as it stands.
  A retry would re-send an old snapshot, and a second handover if someone has re-run it.
* **Anything else** is refused until someone decides its rule here. That fails closed: an
  unclassified producer may own a recovery path of its own.

**No duplicate row.** The retry re-queues the SAME row: same id, same ``idempotency_key``, same
payload, same approval. No row is inserted, so the unique key never gets a second holder. The
status change is a compare-and-set, ``WHERE status IN (FAILED, DEAD)`` and the payload is not
archived, so two admins clicking at once re-queue it once, and a housekeeping sweep that
archives the payload between the check and the write turns the retry into a 409 instead of a
queued row with nothing to send (routes-correctness#6). It clears ``claimed_at``/``claimed_by``
(a terminal row keeps its last claim stamp, and ``_claim``'s CAS would otherwise wait out the
120 s lease) and ``next_attempt_at``.

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
refused mailbox, and it is truncated. Each row carries its ``producer`` and, when it may not be
retried, the ``retry_refusal`` the 409 would give, which names where to recover it.

**Scoping.** ``outbox`` carries ``operator_id``: the list goes through ``_owned``, the retry
through ``_get_owned`` (another operator's id is a 404, never a 403), and the compare-and-set
and the invite lookup through ``_operator_scoped``, so every read and write here is bounded by
the same clause.
"""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update

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

#: The producers ``producer_of`` tells apart (table in the module docstring).
BROADCAST = "incident_broadcast"
LEDGER = "shift_ledger"
PIR_DRAFT = "pir_llm_draft"
COMPLAINT_REMINDER = "complaint_reminder"
MAINTENANCE_INVITE = "maintenance_invite"
REGULATORY_NOTICE = "regulatory_notice"
SHIFT_HANDOVER = "shift_handover"
UNKNOWN_PRODUCER = "unknown"

#: The payload field a regulatory row names its notice by: the same key
#: ``orchestrator/outbox._PRODUCER_OUTCOMES`` feeds the dispatch outcome back on.
REGULATORY_PAYLOAD_KEY = "regulatory_notification_id"

#: ``"_archived": true``, as housekeeping's archive summary serialises its marker (the same
#: ``json.dumps`` defaults), so the compare-and-set can refuse an archived payload in SQL.
_ARCHIVED_MARKER = json.dumps({ARCHIVED_KEY: True})[1:-1]


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


def producer_of(row: OutboxRow, payload: dict[str, Any]) -> str:
    """Which producer queued ``row``, read from its idempotency key (each producer's own format).

    The key is written once and never changed, so the answer cannot move between the check and
    the compare-and-set. A regulatory row is also recognised by its payload marker. The checks
    run most specific first, because the regulatory and handover keys both begin with
    ``EMAIL:``, as an incident broadcast's does.
    """
    key = row.idempotency_key or ""
    if payload.get(REGULATORY_PAYLOAD_KEY) or key.startswith("EMAIL:regulatory:"):  # regulatory.release_notice
        return REGULATORY_NOTICE
    if key.startswith("EMAIL:handover:"):  # notify.dispatch_handover_email
        return SHIFT_HANDOVER
    if key.startswith("complaint.reminder|"):  # complaints.send_due_reminders
        return COMPLAINT_REMINDER
    if row.kind == outbox.ICS_INVITE and _invite_version(key) is not None:  # ics.invite_idempotency_key
        return MAINTENANCE_INVITE
    if row.kind == outbox.LLM_CALL and key.startswith("pir-llm-draft:"):  # pir.queue_llm_draft
        return PIR_DRAFT
    if row.kind == outbox.EXCEL_ROW and key.startswith("EXCEL_ROW:"):  # agents/ledger.py
        return LEDGER
    if row.kind in (outbox.EMAIL, outbox.SMS) and key.startswith(f"{row.kind}:"):
        # agents/broadcast.py, notify.dispatch_incident_email/_sms, the HITL release (v1 and v2)
        return BROADCAST
    return UNKNOWN_PRODUCER


def _invite_version(key: str) -> tuple[str, str, int] | None:
    """``(prefix, METHOD, sequence)`` from ``ics:<op>:<uid>:<METHOD>:<sequence>``, or ``None``.

    Split from the right, so a uid that contains a colon still parses.
    """
    parts = key.rsplit(":", 2)
    if len(parts) != 3 or not parts[0].startswith("ics:"):
        return None
    try:
        return parts[0], parts[1].upper(), int(parts[2])
    except ValueError:
        return None


def _newer_invite(session, row: OutboxRow) -> str | None:
    """The invite that supersedes ``row`` for the same uid, as "METHOD sequence N", or ``None``."""
    mine = _invite_version(row.idempotency_key or "")
    if mine is None:
        return None
    prefix, method, sequence = mine
    keys = session.scalars(
        _operator_scoped(select(OutboxRow.idempotency_key), OutboxRow).where(
            OutboxRow.kind == outbox.ICS_INVITE,
            OutboxRow.idempotency_key.startswith(prefix + ":", autoescape=True),
            OutboxRow.id != row.id,
        )
    ).all()
    for key in keys:
        other = _invite_version(key or "")
        if other is None or other[0] != prefix:  # a longer uid that merely starts with this one
            continue
        if other[2] > sequence or (other[1] == "CANCEL" and method != "CANCEL" and other[2] >= sequence):
            return f"{other[1]} sequence {other[2]}"
    return None


def _producer_refusal(session, row: OutboxRow, payload: dict[str, Any], producer: str) -> str | None:
    """Why ``producer`` does not let this row be retried here, naming its own recovery path."""
    key = row.idempotency_key or ""
    if producer == REGULATORY_NOTICE:
        notice = _text(payload.get(REGULATORY_PAYLOAD_KEY)) or (key.split(":")[2] if key.count(":") >= 2 else "{id}")
        return (
            "a regulatory notification is never retried here: re-release it through "
            f"POST /api/v1/regulatory/{notice}/send, which queues a new attempt-numbered row, keeps this "
            "failed attempt in the notice's evidence, and relies on this row never transmitting; retrying it "
            "as well would send the Communications Authority a second notice"
        )
    if producer == SHIFT_HANDOVER:
        return (
            "a shift handover is never retried here: re-run POST /api/v1/shifts/handover, which composes the "
            "handover from the shift as it stands; retrying this row would re-send an old snapshot, and a "
            "second handover if it has been re-run"
        )
    if producer == MAINTENANCE_INVITE:
        newer = _newer_invite(session, row)
        if newer:
            return (
                f"this invite was superseded by {newer} for the same window, and the attendees' calendars "
                "must get only the newest version, which the maintenance lane issues"
            )
    if producer == COMPLAINT_REMINDER:
        day = key.rsplit("|", 1)[-1]
        if day != utcnow().date().isoformat():  # the producer's own day: naive UTC, see send_due_reminders
            return (
                f"this is the complaint reminder for {day}; the follow-up job queues a fresh one each day with the "
                "complaints still overdue, and this one may list complaints that have since been closed"
            )
    if producer == UNKNOWN_PRODUCER:
        return (
            f"no retry rule has been decided for the producer of this {row.kind} row, so it is refused until one "
            "is (api/routers/outbox_admin.py, producer_of)"
        )
    return None


def _retry_refusal(session, row: OutboxRow, payload: dict[str, Any]) -> str | None:
    """Why ``row`` may not be retried, or ``None`` when it may."""
    if row.status not in RETRYABLE_STATUSES:
        reason = _NOT_RETRYABLE.get(row.status, "only FAILED and DEAD rows are retried")
        return f"outbox row is {row.status}: {reason}"
    if payload.get(ARCHIVED_KEY):
        return "its payload was archived under the §9.4 outbox retention, so there is nothing left to send"
    # The producer's rule before the approval check: where the producer owns the recovery, its
    # path is the answer whether or not this row was ever approved.
    refusal = _producer_refusal(session, row, payload, producer_of(row, payload))
    if refusal:
        return refusal
    if row.kind in outbox.CHANNEL_KINDS and int(row.requires_hitl or 0) and row.approved_at is None:
        # The dispatcher would refuse it again (REJECTED_UNAPPROVED); refusing here says why.
        return f"this {row.kind} row requires a HITL approval it does not have; a retry is not an approval"
    return None


def outbox_summary(session, row: OutboxRow) -> dict[str, Any]:
    """One row as the dead-letter view shows it. See the module docstring for what is left out."""
    payload = _payload(row)
    refusal = _retry_refusal(session, row, payload)
    return {
        "id": row.id,
        "kind": row.kind,
        "producer": producer_of(row, payload),
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
        "retryable": refusal is None,
        "retry_refusal": refusal,
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
        return [outbox_summary(session, row) for row in rows]
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
        payload = _payload(row)
        refusal = _retry_refusal(session, row, payload)
        if refusal:
            raise HTTPException(409, refusal)
        previous_status = row.status
        now = utcnow()
        won = session.execute(
            _operator_scoped(update(OutboxRow), OutboxRow)
            .where(
                OutboxRow.id == row.id,
                OutboxRow.status.in_(RETRYABLE_STATUSES),
                # Re-checked in the write itself: a sweep may archive the payload after the check.
                ~OutboxRow.payload_json.contains(_ARCHIVED_MARKER, autoescape=True),
            )
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
                        "producer": producer_of(row, payload),
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
        return {"ok": True, "previous_status": previous_status, "outbox": outbox_summary(session, row)}
    finally:
        session.close()
