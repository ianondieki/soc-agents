"""Transactional outbox (spec §7.0.2).

Every outbound side effect — the SMTP send, the SMS, the Excel append — is a row in the
``outbox`` table, INSERTed inside the same transaction as the incident and transmitted
afterwards by ``drain_once``. A rollback therefore takes the side effect with it (verified
defects #6/#8/#17: an email could leave and the incident be rolled back behind it, and
the rolled-back INC number be reused), and a crash after the commit leaves a PENDING row
for the next drain instead of a lost notification.

Lifecycle of a row::

    enqueue()        PENDING            (HELD while a HITL task is open)
    release_held()   HELD -> PENDING    once approved; superseded HELD rows -> SUPPRESSED
    drain_once()     PENDING -> CLAIMED compare-and-set with a 120 s lease
    dispatch()       -> SENT | FAILED (transient) | DEAD (permanent) | REJECTED_UNAPPROVED
    record outcome   FAILED with attempts left -> PENDING again (backoff + jitter);
                     one commit per row, then that row's realtime events are published

Rules the rest of the code base relies on:

* producers (BROADCAST, LEDGER, the HITL release, the handover route) only ``enqueue``;
* ``dispatch`` never raises, and refuses (REJECTED_UNAPPROVED) any EMAIL/SMS/WHATSAPP/
  ICS_INVITE row with ``requires_hitl=1`` and no ``approved_at``;
* only transient errors are retried — OSError, httpx.TransportError, SMTP 4xx, HTTP
  429/5xx — at most ``max_attempts`` (3) times with jitter; everything else is DEAD at once;
* a row CLAIMED for longer than the lease belongs to a crashed drainer and is reclaimed by
  the next drain (the crashed attempt counts towards ``max_attempts``).

The cross-border transfer register (spec §7.0.10, §9.2)
-------------------------------------------------------

This is the only path in the running application that puts personal data on a wire, so it
is where the Kenya DPA 2019 / General Regs reg 41(2) record is written:
``services/external_calls.record_transfer`` runs inside ``drain_once`` immediately before
the transmit. Three decisions, each argued rather than defaulted:

**Record BEFORE the transmit, in its own committed transaction.** Ordering is the whole
design. Record-after has a window in which the bytes are already at Google and the process
dies before the row commits — and the outbox is built to *retry* an outcome it never
recorded, so that window produces both an unrecorded transfer (the reg 41(2) failure) and
a second copy of the same mail. Record-before inverts both halves: if recording fails, or
if the process dies between the record and the transmit, nothing has been sent, so the
retry is the first and only send. The cost is an over-record — a row for a transfer that
may not have completed (SMTP 5xx, or a crash in the gap) — and one row per *attempt*. That
is the right way round: a register that over-states is conservative and explainable, one
that under-states is the breach. It also means a broken register REFUSES to transmit
(``FAILED``, retried, then visible as ``outbox.failed``): you may not lawfully move
personal data abroad if you cannot write down that you did.

**A mock send writes NOTHING.** With ``EMAIL_ENABLED=false`` (the default) or no recipient
configured, ``adapters/email_smtp.send_email`` returns ``mode="mock"`` and opens no socket.
Recording that would tell the ODPC that incident data reached a US relay when it never
left the process — the register is a statement of fact about what crossed the border, not
a log of intentions, and a register padded with transfers that never happened is unusable
for the reg 41(2) return and indefensible the first time anyone checks one row against the
mail server. ``transfer_plan`` therefore returns ``None`` for a mock, for SMS (no adapter
until P3) and for ``EXCEL_ROW`` (a local workbook). The register's completeness is bought
back by the test that drives the configured path with the transport mocked.

**The paperwork gate records the gap here; it does not block the mail.** §7.0.10 scopes
the refusal to ``LLM_ENABLED=true`` on a hosted provider and to ``residency="abroad"`` MCP
cards; the SMTP relay is on the *record* list, not the *gate* list. Whether an unfiled TIA
should also silence outage notifications to the NOC's own staff is the operator's call,
not this module's — see ``TRANSFER_GATE_BLOCKS_SEND`` below.

``dispatch()`` remains the bare transmit primitive and writes no record; ``drain_once`` is
the compliant path and the only one the application uses.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import smtplib
import socket
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from noc_agents.adapters import email_smtp
from noc_agents.config import get_settings
from noc_agents.db.models import OutboxRow, new_id, utcnow
from noc_agents.realtime.hub import RealtimeEvent, hub
from noc_agents.services import notify
from noc_agents.services.external_calls import TransferPaperworkMissing, record_transfer
from noc_agents.services.ledger import append_excel_row

try:  # a hard dependency today, but the classifier must not be the thing that breaks a drain
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

LEASE = timedelta(seconds=120)
DEFAULT_MAX_ATTEMPTS = 3

# kinds
EMAIL, SMS, WHATSAPP, ICS_INVITE, EXCEL_ROW = "EMAIL", "SMS", "WHATSAPP", "ICS_INVITE", "EXCEL_ROW"
CHANNEL_KINDS = frozenset({EMAIL, SMS, WHATSAPP, ICS_INVITE})  # the kinds an approval gates

# statuses
PENDING, HELD, CLAIMED, SENT, DELIVERED = "PENDING", "HELD", "CLAIMED", "SENT", "DELIVERED"
FAILED, SUPPRESSED, REJECTED_UNAPPROVED, DEAD = "FAILED", "SUPPRESSED", "REJECTED_UNAPPROVED", "DEAD"

# transfer register (§7.0.10)
TRANSFER_ACTOR = "outbox.dispatcher"
TRANSFER_ACTOR_ROLE = "AGENT"
DEFAULT_SMTP_HOST = "smtp.gmail.com"  # mirrors adapters/email_smtp.send_email's own default

# THE DEFERRED DECISION (report to the operator, do not settle it here). False: an unfiled
# DPIA/TIA for the relay is written into the register as paperwork_status="unfiled" and the
# mail still goes out. True: the same case refuses the send (DEAD) and writes nothing,
# because nothing left. Blocking an outage notification to the NOC's own staff on a Legal
# filing is an availability decision with operational consequences; §7.0.10 does not ask
# for it on this channel, so the code records the gap and leaves the switch in plain sight.
# Both sides of this constant are exercised by tests/integration/test_transfer_register.py.
TRANSFER_GATE_BLOCKS_SEND = False


@dataclass(frozen=True)
class DispatchResult:
    """What one transmit attempt did. ``FAILED`` means transient (the drain decides whether
    to retry); ``DEAD`` is final. ``delivery`` carries the channel adapter's own
    ``mode`` / ``to`` / ``detail`` for the audit note and the realtime event."""

    status: str  # SENT | FAILED | DEAD | REJECTED_UNAPPROVED
    last_error: str | None = None
    provider: str | None = None
    provider_message_id: str | None = None
    delivery: dict[str, Any] = field(default_factory=dict)


@dataclass
class DrainReport:
    claimed: int = 0
    sent: int = 0
    retried: int = 0  # transient failure, attempts left: back to PENDING with next_attempt_at
    failed: int = 0  # transient failure, attempts exhausted
    dead: int = 0
    rejected: int = 0
    reclaimed: int = 0  # stale CLAIMED rows handed back to PENDING before this pass claimed
    lost_lease: int = 0  # dispatched, but another drainer had reclaimed the row meanwhile
    errors: int = 0  # outcome recording raised; the row stays CLAIMED for lease reclaim
    outcomes: dict[str, DispatchResult] = field(default_factory=dict)  # outbox id -> result

    def __str__(self) -> str:
        return (
            f"outbox drain: claimed={self.claimed} sent={self.sent} retried={self.retried} "
            f"failed={self.failed} dead={self.dead} rejected={self.rejected} reclaimed={self.reclaimed}"
        )


# --- producer API ---------------------------------------------------------------------------


def enqueue(
    session: Session,
    *,
    kind: str,
    idempotency_key: str,
    payload: dict,
    envelope: Any | None = None,
    incident_id: str | None = None,
    run_id: str | None = None,
    hitl_task_id: str | None = None,
    alert_id: str | None = None,
    requires_hitl: bool = False,
    approved_by: str | None = None,
    approved_at: datetime | None = None,
    held: bool = False,
    operator_id: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> OutboxRow:
    """INSERT OR IGNORE on the unique key; returns the existing row when present.

    Runs inside the caller's transaction on purpose: the row commits — or rolls back — with
    the incident. ``operator_id`` may also come from ``payload["operator_id"]`` or the
    envelope; the column is NOT NULL.
    """
    op = operator_id or payload.get("operator_id") or _envelope_operator(envelope)
    if not op:
        raise ValueError("outbox.enqueue: operator_id is required (argument, payload or envelope)")
    now = utcnow()
    values = dict(
        id=new_id(),
        operator_id=op,
        created_at=now,
        updated_at=now,
        kind=kind,
        idempotency_key=idempotency_key,
        incident_id=incident_id,
        run_id=run_id,
        hitl_task_id=hitl_task_id,
        alert_id=alert_id,
        payload_json=json.dumps(payload, default=str),
        envelope_json=_dump_envelope(envelope),
        requires_hitl=1 if requires_hitl else 0,
        approved_by=approved_by,
        approved_at=approved_at,
        status=HELD if held else PENDING,
        attempts=0,
        max_attempts=max_attempts,
    )
    _insert_or_ignore(session, values)
    return session.execute(select(OutboxRow).where(OutboxRow.idempotency_key == idempotency_key)).scalar_one()


def release_held(session: Session, *, incident_id: str, alert_id: str, approved_by: str, approved_at: datetime) -> int:
    """After a HITL approval re-render: HELD → PENDING for the NEW alert's rows; old HELD rows → SUPPRESSED.

    Returns the number of rows released. Runs inside the caller's transaction; the drain
    after that commit transmits them.
    """
    now = utcnow()
    released = session.execute(
        update(OutboxRow)
        .where(OutboxRow.incident_id == incident_id, OutboxRow.alert_id == alert_id, OutboxRow.status == HELD)
        .values(status=PENDING, approved_by=approved_by, approved_at=approved_at, updated_at=now)
    ).rowcount
    session.execute(
        update(OutboxRow)
        .where(OutboxRow.incident_id == incident_id, OutboxRow.status == HELD)
        .values(status=SUPPRESSED, last_error=f"superseded by alert {alert_id}", updated_at=now)
    )
    return released


# --- the drain ------------------------------------------------------------------------------


def drain_once(session: Session, *, now: datetime | None = None, limit: int = 50, worker: str | None = None) -> DrainReport:
    """Claim PENDING rows with compare-and-set (UPDATE … SET claimed_at=now, claimed_by=me,
    attempts=attempts+1 WHERE id=? AND status='PENDING' AND (claimed_at IS NULL OR
    claimed_at < now-120s)), dispatch, record outcomes.

    Sync; safe to call from tests, the demo script and the scheduler thread. Call it AFTER
    the producer's commit: the claim is committed before anything is transmitted, so no
    transaction is open while an adapter runs, and one outcome is committed per row so a
    crash mid-drain loses at most the in-flight row (which the lease then reclaims).
    ``now`` is injectable for tests of the lease and the retry schedule.
    """
    now = now or utcnow()
    me = worker or _worker_id()
    report = DrainReport()
    report.reclaimed = _reclaim_stale(session, now)
    ids = _claim(session, now, limit, me)
    jobs = _detached_copies(session, ids)
    session.commit()  # claims durable; the session holds no transaction from here until the outcome
    report.claimed = len(jobs)
    for job in jobs:
        result = _register_then_dispatch(session, job)
        try:
            events = _record_outcome(session, job, result, now, me, report)
            session.commit()
        except Exception:  # noqa: BLE001 — one broken row must not stop the drain; the lease reclaims it
            session.rollback()
            report.errors += 1
            log.exception("outbox: recording the outcome of row %s (%s) failed", job.id, job.kind)
            continue
        for ev in events:  # after the outcome commit: what the UI hears is already durable
            hub.publish_sync(ev)
    return report


def dispatch(row: OutboxRow) -> DispatchResult:
    """Route by kind. REFUSE (status=REJECTED_UNAPPROVED) EMAIL/SMS/WHATSAPP/ICS_INVITE rows whose
    requires_hitl=1 and approved_at IS NULL. Never raises; returns SENT/FAILED/DEAD with last_error."""
    try:
        if row.kind in CHANNEL_KINDS and int(row.requires_hitl or 0) and row.approved_at is None:
            return DispatchResult(REJECTED_UNAPPROVED, last_error="refused: requires HITL approval and approved_at is NULL")
        transmit = _TRANSMITTERS.get(row.kind)
        if transmit is None:
            return DispatchResult(DEAD, last_error=f"no transmitter for outbox kind {row.kind!r}")
        return transmit(row)
    except Exception as exc:  # noqa: BLE001 — the contract is "never raises"
        return DispatchResult(FAILED if is_transient(exc) else DEAD, last_error=_error_text(exc))


# --- transmitters (one per kind; each runs with no transaction open) -------------------------


def _transmit_email(row: OutboxRow) -> DispatchResult:
    payload = _payload(row)
    result = notify.transmit_email(payload)
    delivery = {"mode": result.mode, "to": list(result.to), "detail": result.detail}
    if result.ok:
        return DispatchResult(SENT, provider=result.mode, delivery=delivery)
    # The adapter swallows its own exceptions into EmailResult(mode="error", detail=...), so the
    # transient/permanent call is made from the SMTP reply code it quotes (none quoted: I/O).
    status = FAILED if _email_error_is_transient(result.detail) else DEAD
    return DispatchResult(status, last_error=result.detail, provider=result.mode, delivery=delivery)


def _transmit_sms(row: OutboxRow) -> DispatchResult:
    # No SMS adapter until P3 (adapters/sms_africastalking.py). The row is recorded as a mock
    # send, exactly as the SMS drafts were marked SENT without transmission before the outbox.
    payload = _payload(row)
    return DispatchResult(
        SENT,
        provider="mock",
        delivery={
            "mode": "mock",
            "to": [],
            "detail": f"SMS adapter not configured — message for {payload.get('audience')} stored only (mock)",
        },
    )


def _transmit_excel_row(row: OutboxRow) -> DispatchResult:
    payload = _payload(row)
    path = append_excel_row(payload["operator_id"], payload["file"], payload["cells"])
    return DispatchResult(SENT, provider="openpyxl", provider_message_id=str(path))


_TRANSMITTERS: dict[str, Callable[[OutboxRow], DispatchResult]] = {
    EMAIL: _transmit_email,
    SMS: _transmit_sms,
    EXCEL_ROW: _transmit_excel_row,
}


# --- the cross-border transfer register (§7.0.10, §9.2) --------------------------------------
#
# Read the "transfer register" section of the module docstring before changing anything here:
# the ordering (record, commit, then transmit) is what keeps a failed record from turning into
# a second copy of the same mail.


@dataclass(frozen=True)
class TransferPlan:
    """The reg 41(2) facts about one transmit that is genuinely going to leave the machine."""

    recipient: str  # slugs to a key in config/operators/<op>/transfers.yaml
    recipient_country: str  # ISO-3166 alpha-2; "KE" is domestic, "??" is an unknown relay
    residency: str  # "local" | "kenya" | "abroad", the registry.py vocabulary
    justification: str
    data_description: str


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1"})
_PRIVATE_HOST = re.compile(r"^(?:10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)")
_GOOGLE_SMTP_DOMAINS = ("gmail.com", "googlemail.com", "google.com")


def smtp_relay_identity(host: str) -> tuple[str, str, str]:
    """``(recipient, recipient_country, residency)`` for an SMTP relay host.

    The recipient names are chosen so that ``external_calls.normalise_key`` turns them into
    the keys already in ``transfers.yaml`` — ``"Gmail SMTP"`` → ``gmail_smtp`` — so the
    register and the paperwork line up without a second mapping table to drift.

    Loopback and RFC-1918 hosts are the operator's own relay on the operator's own network,
    which is in Kenya: ``KE`` / ``local``, no cross-border paperwork. A Gmail relay is Google
    LLC in the US: ``US`` / ``abroad``. Anything else is recorded as ``"??"`` / ``abroad`` —
    unknown reads as cross-border, which is the conservative side and the one the ODPC would
    take.
    """
    h = (host or "").strip().strip("[]").rstrip(".").lower()
    if h in _LOOPBACK_HOSTS or _PRIVATE_HOST.match(h) or h.endswith(".local"):
        return f"Operator SMTP relay ({h})", "KE", "local"
    if any(h == d or h.endswith("." + d) for d in _GOOGLE_SMTP_DOMAINS):
        return "Gmail SMTP", "US", "abroad"
    return f"SMTP relay ({h})", "??", "abroad"


def _email_transfer_plan(job: OutboxRow) -> TransferPlan | None:
    """``None`` when this EMAIL row will not actually leave the machine.

    The predicate is the adapter's own: ``send_email`` returns ``mode="mock"`` and opens no
    socket when there is no configured recipient or ``EMAIL_ENABLED`` is off. Asking the
    same two questions here is the only way to decide *before* the send, which is where the
    record has to happen — see the module docstring on ordering.
    """
    if not (email_smtp.demo_recipients() and email_smtp.email_configured()):
        return None
    recipient, country, residency = smtp_relay_identity(os.getenv("SMTP_HOST") or DEFAULT_SMTP_HOST)
    payload = _payload(job)
    incident_number = payload.get("incident_number") or "an incident"
    audience = payload.get("audience") or "NOC"
    return TransferPlan(
        recipient=recipient,
        recipient_country=country,
        residency=residency,
        # No lawful basis is asserted here: the basis and the safeguards belong in the DPIA
        # and the TIA for this recipient, not in a string invented by the dispatcher.
        justification=(
            f"Operational outage notification for {incident_number} to the {audience} "
            f"distribution list, transmitted through the configured SMTP relay "
            f"(outbox {job.kind} {job.id}, attempt {job.attempts}). Lawful basis and "
            f"safeguards: the DPIA/TIA on file for this recipient."
        ),
        data_description=(
            "Incident notification e-mail (subject and body): incident number, site name and "
            "region, alarm code and failure domain, priority, users affected, ETR and assigned "
            "team; staff/vendor mailbox addresses in the SMTP envelope."
        ),
    )


_TRANSFER_PLANS: dict[str, Callable[[OutboxRow], TransferPlan | None]] = {EMAIL: _email_transfer_plan}


def transfer_plan(job: OutboxRow) -> TransferPlan | None:
    """The register entry for a row, or ``None`` when nothing crosses a boundary.

    ``EXCEL_ROW`` writes a local workbook; ``SMS`` has no adapter until P3 and is a mock; a
    mock EMAIL transmits nothing. None of those are transfers, so none of them get a row.
    """
    planner = _TRANSFER_PLANS.get(job.kind)
    return planner(job) if planner is not None else None


def _register_then_dispatch(session: Session, job: OutboxRow) -> DispatchResult:
    """Write the reg 41(2) record, commit it, and only then let the bytes leave.

    Every exit from here leaves the session with no open transaction, which is the
    invariant ``drain_once`` relies on while an adapter runs.
    """
    plan = transfer_plan(job)
    if plan is None:
        return dispatch(job)
    try:
        record_transfer(
            session,
            recipient=plan.recipient,
            recipient_country=plan.recipient_country,
            justification=plan.justification,
            data_description=plan.data_description,
            actor=TRANSFER_ACTOR,
            actor_role=TRANSFER_ACTOR_ROLE,
            incident_id=job.incident_id,
            residency=plan.residency,
            settings=get_settings(job.operator_id),
            enforce_gate=TRANSFER_GATE_BLOCKS_SEND,
        )
        session.commit()  # durable BEFORE the transmit: a crash in the gap loses a send, never a record
    except TransferPaperworkMissing as exc:  # only reachable with TRANSFER_GATE_BLOCKS_SEND on
        session.rollback()
        log.warning("outbox: %s row %s refused by the transfer paperwork gate: %s", job.kind, job.id, exc)
        return DispatchResult(DEAD, last_error=f"refused: {exc}"[:2000])
    except Exception:  # noqa: BLE001 — unrecordable means untransmittable; the row retries
        session.rollback()
        log.exception(
            "outbox: the transfer register could not be written for %s row %s — NOT transmitting", job.kind, job.id
        )
        return DispatchResult(FAILED, last_error="transfer register write failed; nothing was transmitted")
    return dispatch(job)


# --- error classification --------------------------------------------------------------------

_SMTP_REPLY = re.compile(r"\((\d{3}), b['\"]")  # the (code, b'text') tuple smtplib puts in str(exc)


def is_transient(exc: BaseException) -> bool:
    """The spec's list: OSError, httpx.TransportError, SMTP 4xx, HTTP 429/5xx. Every
    smtplib exception is an OSError, so the reply-code cases are decided first."""
    if isinstance(exc, smtplib.SMTPResponseException):
        return 400 <= exc.smtp_code < 500
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = [code for code, _msg in exc.recipients.values()]
        return bool(codes) and all(400 <= c < 500 for c in codes)
    if httpx is not None:
        if isinstance(exc, httpx.HTTPStatusError):
            code = exc.response.status_code
            return code == 429 or code >= 500
        if isinstance(exc, httpx.TransportError):
            return True
    return isinstance(exc, OSError)


def _email_error_is_transient(detail: str | None) -> bool:
    codes = [int(c) for c in _SMTP_REPLY.findall(detail or "")]
    return not codes or all(400 <= c < 500 for c in codes)


# --- internals ------------------------------------------------------------------------------


def _reclaim_stale(session: Session, now: datetime) -> int:
    """A CLAIMED row older than the lease was abandoned by a crashed drainer: hand it back
    (or, when it had already used every attempt, close it as FAILED)."""
    stale = now - LEASE
    session.execute(
        update(OutboxRow)
        .where(OutboxRow.status == CLAIMED, OutboxRow.claimed_at < stale, OutboxRow.attempts >= OutboxRow.max_attempts)
        .values(status=FAILED, updated_at=now, last_error="lease expired with no attempts left")
    )
    return session.execute(
        update(OutboxRow)
        .where(OutboxRow.status == CLAIMED, OutboxRow.claimed_at < stale)
        .values(status=PENDING, updated_at=now)  # claimed_at is kept: the CAS below admits a stale claim
    ).rowcount


def _claim(session: Session, now: datetime, limit: int, me: str) -> list[str]:
    stale = now - LEASE
    candidates = session.scalars(
        select(OutboxRow.id)
        .where(
            OutboxRow.status == PENDING,
            or_(OutboxRow.next_attempt_at.is_(None), OutboxRow.next_attempt_at <= now),
        )
        .order_by(OutboxRow.created_at, OutboxRow.id)
        .limit(limit)
    ).all()
    claimed: list[str] = []
    for rid in candidates:
        won = session.execute(
            update(OutboxRow)
            .where(
                OutboxRow.id == rid,
                OutboxRow.status == PENDING,
                or_(OutboxRow.claimed_at.is_(None), OutboxRow.claimed_at < stale),
            )
            .values(status=CLAIMED, claimed_at=now, claimed_by=me, attempts=OutboxRow.attempts + 1, updated_at=now)
        ).rowcount
        if won == 1:
            claimed.append(rid)
    return claimed


def _detached_copies(session: Session, ids: list[str]) -> list[OutboxRow]:
    """Plain copies of the claimed rows, never attached to the session: the commit that
    follows cannot expire them, so dispatch reads them without opening a transaction."""
    if not ids:
        return []
    table = OutboxRow.__table__
    rows = session.execute(select(table).where(table.c.id.in_(ids)).order_by(table.c.created_at, table.c.id)).mappings()
    return [OutboxRow(**dict(m)) for m in rows]


def _record_outcome(
    session: Session, job: OutboxRow, result: DispatchResult, now: datetime, me: str, report: DrainReport
) -> list[RealtimeEvent]:
    values: dict[str, Any] = {
        "updated_at": now,
        "last_error": result.last_error,
        "provider": result.provider,
        "provider_message_id": result.provider_message_id,
    }
    final: str | None = result.status
    if result.status == SENT:
        values.update(status=SENT, sent_at=now)
        report.sent += 1
    elif result.status == REJECTED_UNAPPROVED:
        values.update(status=REJECTED_UNAPPROVED)
        report.rejected += 1
    elif result.status == DEAD:
        values.update(status=DEAD)
        report.dead += 1
    elif result.status == FAILED:
        if job.attempts >= job.max_attempts:
            values.update(status=FAILED)
            report.failed += 1
        else:
            values.update(status=PENDING, claimed_at=None, claimed_by=None, next_attempt_at=now + _backoff(job.attempts))
            report.retried += 1
            final = None
    else:
        raise ValueError(f"dispatch returned unknown status {result.status!r} for row {job.id}")
    # Conditional on still holding the claim: if the lease expired mid-dispatch and another
    # drainer reclaimed the row, its outcome is the one that stands.
    held = session.execute(
        update(OutboxRow)
        .where(OutboxRow.id == job.id, OutboxRow.status == CLAIMED, OutboxRow.claimed_by == me)
        .values(**values)
    ).rowcount
    if held != 1:
        report.lost_lease += 1
        log.warning("outbox: row %s was reclaimed by another drainer before its outcome was recorded", job.id)
        return []
    report.outcomes[job.id] = result
    if final is None:
        return []
    events = _finalize(session, job, result, final, now)
    if final in (FAILED, DEAD, REJECTED_UNAPPROVED):
        events.append(
            RealtimeEvent(
                type="outbox.failed",
                operator_id=job.operator_id,
                incident_id=job.incident_id,
                payload={
                    "outbox_id": job.id,
                    "kind": job.kind,
                    "incident_number": _payload(job).get("incident_number"),
                    "status": final,
                    "attempts": job.attempts,
                    "error": result.last_error,
                },
            )
        )
    return events


def _finalize(session: Session, job: OutboxRow, result: DispatchResult, final: str, now: datetime) -> list[RealtimeEvent]:
    """Kind-specific bookkeeping on a terminal outcome (drafts, notes, events)."""
    if job.kind == EMAIL:
        delivery = result.delivery or {"mode": "error", "to": [], "detail": result.last_error or final}
        return notify.record_email_outcome(session, job, final_status=final, delivery=delivery, now=now)
    if job.kind == SMS:
        return notify.record_sms_outcome(session, job, final_status=final, now=now)
    return []


def _backoff(attempts: int) -> timedelta:
    base = min(60.0, 2.0 ** max(attempts, 1))
    return timedelta(seconds=base + random.uniform(0, base / 2))


def _payload(row: OutboxRow) -> dict:
    return json.loads(row.payload_json or "{}")


def _insert_or_ignore(session: Session, values: dict) -> None:
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        session.execute(sqlite_insert(OutboxRow).values(**values).on_conflict_do_nothing(index_elements=["idempotency_key"]))
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        session.execute(pg_insert(OutboxRow).values(**values).on_conflict_do_nothing(index_elements=["idempotency_key"]))
    else:
        try:
            with session.begin_nested():
                session.execute(insert(OutboxRow).values(**values))
        except IntegrityError:
            pass


def _dump_envelope(envelope: Any | None) -> str | None:
    if envelope is None:
        return None
    if hasattr(envelope, "model_dump"):
        return json.dumps(envelope.model_dump(mode="json"), default=str)
    return json.dumps(envelope, default=str)


def _envelope_operator(envelope: Any | None) -> str | None:
    if envelope is None:
        return None
    if isinstance(envelope, dict):
        return envelope.get("operator_id")
    return getattr(envelope, "operator_id", None)


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:2000]
