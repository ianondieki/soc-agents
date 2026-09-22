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
                     DEFERRED (EMAIL over EMAIL_DAILY_CAP) -> PENDING until the window
                     rolls, no attempt spent — see ``_email_cap_gate``;
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

This is the path the running application puts personal data on a wire from, so it is where
the Kenya DPA 2019 / General Regs reg 41(2) record is written:
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
mail server. ``transfer_plan`` therefore returns ``None`` for a mock (of either SMTP kind
— an ``ICS_INVITE`` with no attendees or no relay configured moves nothing either), for SMS
(no adapter until P3) and for ``EXCEL_ROW`` (a local workbook). The register's completeness is bought
back by the test that drives the configured path with the transport mocked.

**The paperwork gate records the gap here; it does not block the mail.** §7.0.10 scopes
the refusal to ``LLM_ENABLED=true`` on a hosted provider and to ``residency="abroad"`` MCP
cards; the SMTP relay is on the *record* list, not the *gate* list. Whether an unfiled TIA
should also silence outage notifications to the NOC's own staff is the operator's call,
not this module's — see ``TRANSFER_GATE_BLOCKS_SEND`` below.

**``LLM_CALL`` records its own transfer, inside the transmitter.** It is the one kind whose
record has to be linked to something else — ``llm_calls.audit_id`` points at the reg 41(2)
row — so the transmitter opens its own short session and writes both there, in the same
record-then-commit-then-call order argued above. ``transfer_plan`` therefore covers the two
SMTP kinds only (``EMAIL`` and ``ICS_INVITE``) and ``_register_then_dispatch`` is untouched
by that lane. The §7.0.10 paperwork gate is ON
for it (``enforce_gate=True``), unlike the SMTP relay: the spec makes the TIA a gating
artefact for a hosted model provider, and no draft is worth being the first unlawful transfer.

``dispatch()`` remains the bare transmit primitive and writes no record for the channel
kinds; ``drain_once`` is the compliant path and the only one the application uses.
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
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Callable
from urllib.parse import urlparse

from pydantic import BaseModel
from sqlalchemy import insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from noc_agents.adapters import email_smtp
from noc_agents.config import get_settings
from noc_agents.db.models import OutboxRow, get_session, new_id, utcnow
from noc_agents.llm import client as llm_client
from noc_agents.llm.port import record_llm_call
from noc_agents.llm.redaction import EMAIL_RE, PHONE_RE
from noc_agents.realtime.hub import RealtimeEvent, hub
from noc_agents.services import ics, notify
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
# An out-of-band model call (§7.7.3). NOT a channel kind: nothing is delivered to a person, so
# the HITL approval gate below does not apply to it — the human gate on this lane is that a
# named reviewer publishes the review, never that a draft was requested.
LLM_CALL = "LLM_CALL"
CHANNEL_KINDS = frozenset({EMAIL, SMS, WHATSAPP, ICS_INVITE})  # the kinds an approval gates

# statuses
PENDING, HELD, CLAIMED, SENT, DELIVERED = "PENDING", "HELD", "CLAIMED", "SENT", "DELIVERED"
FAILED, SUPPRESSED, REJECTED_UNAPPROVED, DEAD = "FAILED", "SUPPRESSED", "REJECTED_UNAPPROVED", "DEAD"
# A dispatch OUTCOME, never a stored status: EMAIL_DAILY_CAP held the row back (§7.9.1).
# ``_record_outcome`` turns it into PENDING + next_attempt_at, so ``outbox.status`` never reads it.
DEFERRED = "DEFERRED"

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

    status: str  # SENT | FAILED | DEAD | REJECTED_UNAPPROVED | DEFERRED
    last_error: str | None = None
    provider: str | None = None
    provider_message_id: str | None = None
    delivery: dict[str, Any] = field(default_factory=dict)
    retry_at: datetime | None = None  # DEFERRED only: the instant the daily-cap window frees a slot
    cap_note: str | None = None  # EMAIL_DAILY_CAP WorkNote, written by _record_outcome with this outcome
    smtp_messages: int = 0  # EMAIL: messages the relay accepted on this attempt (counted by the cap)


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
    deferred: int = 0  # held by EMAIL_DAILY_CAP: back to PENDING until the window rolls, no attempt spent
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
        result = _cap_then_dispatch(session, job, now)
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
    # What the relay accepted — several messages for a batched audience, some for a partial
    # failure, 0 for a mock — so EMAIL_DAILY_CAP counts what really left (review E01).
    accepted = notify.smtp_messages_accepted(result)
    if result.ok:
        return DispatchResult(SENT, provider=result.mode, delivery=delivery, smtp_messages=accepted)
    # The adapter swallows its own exceptions into EmailResult(mode="error", detail=...), so the
    # transient/permanent call is made from the SMTP reply code it quotes (none quoted: I/O).
    status = FAILED if _email_error_is_transient(result.detail) else DEAD
    return DispatchResult(status, last_error=result.detail, provider=result.mode, delivery=delivery, smtp_messages=accepted)


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


def _transmit_ics_invite(row: OutboxRow) -> DispatchResult:
    """The iMIP maintenance-window invite (§7.5.6, RFC 6047).

    ``services/ics`` hands over a finished ``EmailMessage``, so this is the EMAIL path with
    two differences, both of which matter to whether a calendar client accepts the invite:

    * the recipients are the ATTENDEES named in the calendar object, taken from the payload
      and never from ``DEMO_EMAIL_TO`` — an invite rerouted to the demo mailbox is an
      engineer who never learns about the window;
    * ``From:`` is the ORGANIZER, which ``adapters/email_smtp.send_message`` leaves alone.

    A payload this dispatcher does not understand (an unknown ``payload_version``, a missing
    calendar object, no attendees) is DEAD, not retried: ``build_imip_message`` refuses to
    guess, and a second attempt would guess no better.
    """
    payload = _payload(row)
    try:
        msg = ics.build_imip_message(payload)
    except ics.IcsValidationError as exc:
        return DispatchResult(DEAD, last_error=f"refused: {exc}"[:2000])
    result = email_smtp.send_message(msg)
    delivery = {"mode": result.mode, "to": list(result.to), "detail": result.detail}
    if result.ok:
        return DispatchResult(SENT, provider=result.mode, delivery=delivery)
    # Same reading as _transmit_email: the adapter swallows its own exceptions, so the
    # transient/permanent call comes from the SMTP reply code it quotes.
    status = FAILED if _email_error_is_transient(result.detail) else DEAD
    return DispatchResult(status, last_error=result.detail, provider=result.mode, delivery=delivery)


def _transmit_excel_row(row: OutboxRow) -> DispatchResult:
    payload = _payload(row)
    path = append_excel_row(payload["operator_id"], payload["file"], payload["cells"])
    return DispatchResult(SENT, provider="openpyxl", provider_message_id=str(path))


# --- LLM_CALL: the out-of-band model draft (§7.7.3, §7.0.9) ----------------------------------
#
# Why this is a transmitter at all: ``POST /pir/{id}/draft/llm`` writes a redacted row and
# returns, because nothing may hold a SQLite write lock across a call that can take a minute.
# Without an entry in ``_TRANSMITTERS`` the drain marked that row DEAD ("no transmitter for
# outbox kind 'LLM_CALL'"), so the lane queued work nobody could do.

LLM_INERT_PROVIDER = "none"  # provider recorded when no model call was made at all
LLM_DRAFT_EFFORT = "low"  # drafting, not reasoning: §5.3.18 keeps Fable off this lane
LLM_DRAFT_MAX_TOKENS = 2048
LLM_DEFAULT_AGENT = "outbox.dispatcher"


class PirDraftText(BaseModel):
    """The five DRAFT text fields §7.7.3 lets a model propose, and nothing else.

    The shape lives beside its only caller rather than in ``llm/outputs.py`` because it is
    the wire contract of ONE outbox purpose: the field list is asserted against
    ``payload["fields"]`` below, so a producer that queues a different field list gets a
    refusal instead of a model quietly answering a question nobody asked. All five are
    required: an answer missing one is unusable output, which the port already reports as
    ``rec.ok=False``.

    Status, reviewer, action items and metrics are absent on purpose — the model drafts
    prose, a named human publishes (§7.7.6).
    """

    summary: str
    root_causes: str
    went_well: str
    went_poorly: str
    got_lucky: str


#: The PIR drafting guardrails. A sibling of ``llm/assist.GUARDRAILS`` and deliberately a
#: separate text: this lane is blameless-first (``services/pir.blameless_violation`` rejects a
#: person's name on the way into a review), and assist's wording does not say that.
PIR_DRAFT_SYSTEM = (
    "You assist a Kenyan telecom NOC writing a blameless post-incident review. Use only the "
    "facts in the JSON you are given; do not invent alarms, causes or timings. Person names "
    "appear as tokens like <PERSON_1>: keep them verbatim and never guess who they are. "
    "Describe what the system allowed, not who did it — prefer role tokens (RNIO, FE, "
    "MSP_POWER). You draft text a named human will review and publish; you do not decide "
    "status, reviewers, action items or metrics."
)

# purpose -> (output shape, system prompt, the agent the llm_calls row is attributed to).
# "pir_draft" is ``services/pir.LLM_DRAFT_PURPOSE`` (quoted, not imported: services/pir.py
# imports this module, so the edge has to point one way).
_LLM_PURPOSES: dict[str, tuple[type[BaseModel], str, str]] = {
    "pir_draft": (PirDraftText, PIR_DRAFT_SYSTEM, "PostIncidentReviewAgent"),
}


def llm_recipient_identity() -> tuple[str, str, str]:
    """``(recipient, recipient_country, residency)`` for the configured model provider.

    Same contract as ``smtp_relay_identity``: the names slug to keys that already exist in
    ``config/operators/<op>/transfers.yaml`` (``"Anthropic API"`` → ``anthropic_api``,
    ``"Ollama local"`` → ``ollama_local``), so the register and the paperwork line up with no
    second mapping table. A loopback or RFC-1918 OpenAI-compatible endpoint is the operator's
    own box in Kenya; anything else reads as abroad, which is the conservative side.
    """
    if llm_client.llm_provider() == llm_client.PROVIDER_OPENAI_COMPAT:
        host = (urlparse(llm_client.openai_compat_base_url()).hostname or "").strip().lower()
        if host in _LOOPBACK_HOSTS or _PRIVATE_HOST.match(host) or host.endswith(".local"):
            return "Ollama local", "KE", "local"
        return f"OpenAI-compatible endpoint ({host or 'unknown'})", "??", "abroad"
    return "Anthropic API", "US", "abroad"


def unredacted_findings(payload: dict) -> dict[str, int]:
    """Counts of e-mail addresses / MSISDNs found in what is about to be sent. ``{}`` is clean.

    The last gate before the bytes leave. ``services/pir.queue_llm_draft`` redacts with
    ``llm/redaction.redact_incident`` BEFORE the row is written, which is the right place —
    the outbox row itself must not hold identifiers. But ``enqueue`` cannot enforce that, so a
    future producer of an ``LLM_CALL`` row could hand this transmitter raw text, and this
    module would be the thing that posted it abroad. Re-checking with redaction's own patterns
    costs one regex pass over a payload we are about to serialise anyway.

    It catches contact identifiers only — there is no NER here, so a person named purely
    inside free text is not detected (the same limitation ``llm/redaction`` documents). It is
    a backstop for a producer that forgot to redact, not a substitute for redacting.

    COUNTS ONLY, never the matched text: ``last_error`` is stored and exportable, and a
    finding that quotes the identifier it found is a second copy of it (§9.5).
    """
    blob = json.dumps(payload, default=str)
    found = {"emails": len(EMAIL_RE.findall(blob)), "msisdns": len(PHONE_RE.findall(blob))}
    return {k: v for k, v in found.items() if v}


def _llm_inert(reason: str, *, detail: str) -> DispatchResult:
    """No model call was made, and that is not a failure.

    SENT, exactly as ``_transmit_sms`` records a channel with no adapter: the row is finished,
    it is not retried, and no ``outbox.failed`` alarm fires — with ``LLM_ENABLED=false`` the
    whole LLM layer's contract is "the deterministic path happens instead", and for a PIR the
    deterministic path is the human writing the review. ``last_error`` still carries the
    reason for everything except the plain off switch, so a spend cap or a missing credential
    is visible on the row rather than silent.
    """
    return DispatchResult(
        SENT,
        last_error=None if reason == "disabled" else f"no model call: {reason}",
        provider=LLM_INERT_PROVIDER,
        delivery={"mode": "inert", "to": [], "detail": detail},
    )


def _transmit_llm_call(row: OutboxRow) -> DispatchResult:
    """One model call for a queued ``LLM_CALL`` row, through the §7.0.9 port.

    Through the port, never a fresh API call: ``get_llm_port()`` is what applies the G13
    subscription guard, the provider choice and the spend circuit, and ``record_llm_call``
    is what puts the tokens and the estimated cost in ``llm_calls`` where the budget reads
    them back. A bespoke HTTP call here would be invisible to all three.

    **It opens its own session.** Every other transmitter is pure I/O because ``drain_once``
    deliberately holds no transaction while an adapter runs. This one has two records of its
    own to write — the reg 41(2) transfer BEFORE the call and the ``llm_calls`` row after —
    and they belong to the call, not to the row's outcome: they must survive even if the
    outcome commit later fails and the lease hands the row to another drainer. A separate
    short-lived session keeps that true without ever opening a transaction on the drain's.

    **The drafted TEXT is deliberately not written anywhere here.** Review text is
    ``services/pir.py``'s business: it is blameless-validated on the way in and published by
    a named human, and a dispatcher that wrote it straight into ``post_incident_reviews``
    would walk around both. Until that lane grows a sink for drafted text (a pir service
    function that applies it under the validator and the DRAFT/IN_REVIEW status guard), this
    transmitter proves the call and records it and its cost, and stops there: what a draft is
    allowed to overwrite in a review is that lane's decision, not the dispatcher's.
    """
    payload = _payload(row)
    purpose = str(payload.get("purpose") or "")
    spec = _LLM_PURPOSES.get(purpose)
    if spec is None:  # an unknown purpose has no output shape: refuse, do not improvise one
        return DispatchResult(DEAD, last_error=f"no output shape for LLM_CALL purpose {purpose!r}")
    output_model, system, agent = spec
    fields = list(payload.get("fields") or [])
    if fields and fields != list(output_model.model_fields):
        return DispatchResult(
            DEAD,
            last_error=f"LLM_CALL {purpose!r} asks for fields {fields} but the {output_model.__name__} shape drafts {list(output_model.model_fields)}",
        )
    body = payload.get("redacted_incident")
    if not isinstance(body, dict) or not body:
        return DispatchResult(DEAD, last_error=f"LLM_CALL {purpose!r} carries no redacted_incident payload")

    # Asked BEFORE any session is opened, so a suite (or a deployment) with the layer off
    # never touches the database for a row that is going to do nothing.
    reason = llm_client.llm_unavailable_reason()
    port = None if reason else llm_client.get_llm_port()
    if port is None:
        reason = reason or "port_unavailable"
        return _llm_inert(reason, detail=f"LLM layer unavailable ({reason}) — {purpose} not drafted")

    leaks = unredacted_findings(body)
    if leaks:
        # Fail closed and loudly: this is a producer bug, and one more attempt would send the
        # same identifiers again, so it is DEAD rather than retried.
        log.error("outbox: LLM_CALL row %s refused — payload still carries %s (see queue_llm_draft's redaction)", row.id, leaks)
        return DispatchResult(DEAD, last_error=f"refused: payload is not redacted ({leaks}); nothing was sent")

    model = str(payload.get("model") or llm_client.MODEL_DRAFTING)
    session = get_session()  # our own session — see the docstring
    try:
        gate = llm_client.spend_gate(session, operator_id=row.operator_id)
        if gate:  # spend_cap | budget_exhausted: the ceiling is a decision, not an error
            return _llm_inert(gate, detail=f"spend gate open ({gate}) — {purpose} not drafted")
        recipient, country, residency = llm_recipient_identity()
        try:
            audit = record_transfer(
                session,
                recipient=recipient,
                recipient_country=country,
                justification=(
                    f"Post-incident review drafting assistance ({purpose}) on a redacted "
                    f"incident record (outbox {row.kind} {row.id}, attempt {row.attempts}). "
                    f"Lawful basis and safeguards: the DPIA/TIA on file for this recipient."
                ),
                data_description=(
                    "Pseudonymised incident record and work notes: network and operational "
                    "fields only, person names replaced by <PERSON_n> tokens, e-mail addresses "
                    "and MSISDNs removed before the row was queued (llm/redaction.py)."
                ),
                actor=TRANSFER_ACTOR,
                actor_role=TRANSFER_ACTOR_ROLE,
                incident_id=row.incident_id,
                residency=residency,
                settings=get_settings(row.operator_id),
                # The gate is ON here, unlike the SMTP relay: §7.0.10 makes the TIA a gating
                # artefact for exactly this case — a hosted model provider. An unfiled DPIA/TIA
                # therefore refuses the call, and no draft is worth being the first unlawful
                # transfer. (NOC_ENV=demo records DEMO-UNFILED and lets it through, as
                # everywhere else, so the demo shows the gap instead of hiding it.)
                enforce_gate=True,
            )
            session.commit()  # durable BEFORE the call: a crash in the gap loses a draft, never a record
        except TransferPaperworkMissing as exc:
            session.rollback()
            log.warning("outbox: LLM_CALL row %s refused by the transfer paperwork gate: %s", row.id, exc)
            return DispatchResult(DEAD, last_error=f"refused: {exc}"[:2000])
        audit_id = audit.id

        parsed, rec = port.draft(
            model=model,
            system=system,
            user=json.dumps(body, default=str),
            output_model=output_model,
            effort=LLM_DRAFT_EFFORT,
            max_tokens=LLM_DRAFT_MAX_TOKENS,
            timeout=llm_client.timeout_s(),
        )
        provider = getattr(port, "provider", llm_client.llm_provider())
        try:
            record_llm_call(
                session,
                operator_id=row.operator_id,
                agent=payload.get("agent") or agent or LLM_DEFAULT_AGENT,
                purpose=purpose,
                provider=provider,
                rec=rec,
                audit_id=audit_id,
                run_id=row.run_id,
                incident_id=row.incident_id,
                validated=parsed is not None,
            )
            session.commit()
        except Exception:  # noqa: BLE001 — the call already happened and is already in the register
            # The llm_calls row is the engineering detail; the reg 41(2) record above is the
            # legal one and is already committed. Losing the outcome of a call that DID happen
            # would mean retrying it, which costs money and sends the data a second time.
            session.rollback()
            log.exception("outbox: llm_calls row for %s (%s) could not be written", row.id, purpose)

        delivery = {"mode": provider, "to": [], "detail": f"{purpose} via {rec.model_used or model}", "model": rec.model_used}
        if rec.ok and parsed is not None:
            return DispatchResult(SENT, provider=provider, provider_message_id=rec.model_used, delivery=delivery)
        if rec.refused:  # a refusal is deterministic: the next attempt refuses too
            return DispatchResult(DEAD, last_error=f"model refused: {rec.error}"[:2000], provider=provider, delivery=delivery)
        return DispatchResult(FAILED, last_error=f"no usable draft: {rec.error}"[:2000], provider=provider, delivery=delivery)
    finally:
        session.close()


_TRANSMITTERS: dict[str, Callable[[OutboxRow], DispatchResult]] = {
    EMAIL: _transmit_email,
    SMS: _transmit_sms,
    EXCEL_ROW: _transmit_excel_row,
    ICS_INVITE: _transmit_ics_invite,
    LLM_CALL: _transmit_llm_call,
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


def _ics_transfer_plan(job: OutboxRow) -> TransferPlan | None:
    """``None`` when this invite will not actually leave the machine.

    Same predicate as the EMAIL plan, with one substitution: an invite has no demo fallback,
    so what decides whether bytes move is the payload's own attendee list, not
    ``demo_recipients()``. Attendee mailbox addresses are personal data and they travel in
    the calendar object as well as the SMTP envelope, which is why §7.5.6 asks for this row.
    """
    payload = _payload(job)
    recipients = [str(a).strip() for a in (payload.get("to") or []) if str(a).strip()]
    if not recipients or not email_smtp.email_configured():
        return None
    recipient, country, residency = smtp_relay_identity(os.getenv("SMTP_HOST") or DEFAULT_SMTP_HOST)
    return TransferPlan(
        recipient=recipient,
        recipient_country=country,
        residency=residency,
        justification=(
            f"Maintenance window invitation ({payload.get('method') or 'REQUEST'}) for window "
            f"{payload.get('window_id') or 'unknown'} to {len(recipients)} attendee mailbox(es), "
            f"transmitted through the configured SMTP relay (outbox {job.kind} {job.id}, "
            f"attempt {job.attempts}). Lawful basis and safeguards: the DPIA/TIA on file for "
            f"this recipient."
        ),
        data_description=(
            "Calendar invitation (iMIP/RFC 5545): maintenance window summary, location, start "
            "and end times and description; organiser and attendee mailbox addresses in the "
            "calendar object and in the SMTP envelope."
        ),
    )


_TRANSFER_PLANS: dict[str, Callable[[OutboxRow], TransferPlan | None]] = {
    EMAIL: _email_transfer_plan,
    ICS_INVITE: _ics_transfer_plan,
}


def transfer_plan(job: OutboxRow) -> TransferPlan | None:
    """The register entry for a row, or ``None`` when nothing crosses a boundary.

    ``EXCEL_ROW`` writes a local workbook; ``SMS`` has no adapter until P3 and is a mock; a
    mock EMAIL transmits nothing. None of those are transfers, so none of them get a row.
    ``LLM_CALL`` genuinely is one, but writes its own record inside ``_transmit_llm_call``
    (it needs the audit row's id for ``llm_calls.audit_id``) — see the module docstring.
    """
    planner = _TRANSFER_PLANS.get(job.kind)
    return planner(job) if planner is not None else None


def _cap_then_dispatch(session: Session, job: OutboxRow, now: datetime) -> DispatchResult:
    """``EMAIL_DAILY_CAP`` around the unchanged record-then-transmit path.

    A held row returns before the transfer register, so it writes no reg 41(2) record for a send
    that did not happen. A note owed on success (80 %, P1 override, unchecked P1) rides on the
    result only if the send really was SENT; ``_record_outcome`` writes it in the outcome's own
    commit, so no note ever describes a send that then failed (review E07).
    """
    held, note_on_sent = _email_cap_gate(session, job, now)
    if held is not None:
        return held
    result = _register_then_dispatch(session, job)
    if note_on_sent and result.status == SENT:
        result = replace(result, cap_note=note_on_sent)
    return result


def _email_cap_gate(session: Session, job: OutboxRow, now: datetime) -> tuple[DispatchResult | None, str | None]:
    """``EMAIL_DAILY_CAP`` (§7.9.1, §10.3 ✱) at the one place mail actually leaves.

    Returns ``(held, note_on_sent)``: ``held`` is the outcome when the row must not go now,
    ``note_on_sent`` the WorkNote owed if it goes and succeeds. The policy — the 80 % note, P1
    goes anyway, everything else waits for the rolling window — is
    ``services/notify.email_cap_decision``, argued there; this only acts on it. It READS only:
    every note is written later, with the outcome, so a failing note can never change what the
    decision said (review E05).

    INERT UNLESS MAIL WOULD REALLY LEAVE. With ``EMAIL_ENABLED`` off or no credentials — the
    demo default and the whole test suite — the adapter only mocks, a mock costs no quota, and
    this returns before touching the database: rows, events and notes are what they were.
    A row the approval gate is about to refuse is left to ``dispatch`` to refuse, so a cap
    decision can never turn REJECTED_UNAPPROVED into a quiet PENDING.
    """
    if job.kind != EMAIL or not email_smtp.email_configured():
        return None, None
    if int(job.requires_hitl or 0) and job.approved_at is None:
        return None, None
    try:
        decision = notify.email_cap_decision(session, job, now=now)
    except Exception as exc:  # noqa: BLE001 — the count could not be read; see _cap_unchecked
        session.rollback()
        return _cap_unchecked(session, job, exc)
    session.rollback()  # reads only, nothing to keep: no transaction stays open while the adapter runs
    if decision.action == notify.CAP_DEFER:
        return DispatchResult(DEFERRED, last_error=decision.reason, retry_at=decision.retry_at, cap_note=decision.note), None
    if decision.action == notify.CAP_REFUSE:
        return DispatchResult(DEAD, last_error=decision.reason), None
    return None, decision.note  # SEND / P1 OVERRIDE: the note only if the send succeeds


def _cap_unchecked(session: Session, job: OutboxRow, exc: BaseException) -> tuple[DispatchResult | None, str | None]:
    """The count could not be read. Fail OPEN for a P1 only, and never silently (review E05).

    A P1 goes — the same reasoning as the P1 override: the cap exists to protect the next P1 —
    and leaves a WorkNote saying it went uncounted, plus an ERROR log. Anything else is NOT sent:
    it is FAILED with the reason, which is the transfer register's precedent for "cannot record
    it, so do not transmit it": retried with backoff up to ``max_attempts``, then terminal with
    ``outbox.failed`` on the wallboard and a ``mode=error`` note. A persistent fault therefore
    cannot quietly switch the cap off for every row, as the previous fail-open could.
    """
    try:
        priority = notify.incident_priority(session, job.incident_id)
    except Exception:  # noqa: BLE001 — most likely the same unreadable database as the count
        # Round-3 C5: when the database refuses reads, this lookup fails exactly as the count did,
        # and a P1 would collapse to fail-closed. So the P1 question falls back to the priority the
        # row was QUEUED with — its own "[P1] INC… |" subject, already in hand, no second read.
        priority = notify.queued_priority(job.payload_json)
    session.rollback()
    if priority in notify.EMAIL_CAP_OVERRIDE_PRIORITIES:
        log.error("outbox: EMAIL_DAILY_CAP unreadable for P1 row %s — sending uncounted", job.id, exc_info=exc)
        return None, notify.cap_unchecked_note(priority, exc)
    log.error("outbox: EMAIL_DAILY_CAP unreadable for row %s — not transmitted, will retry", job.id, exc_info=exc)
    return DispatchResult(FAILED, last_error=notify.cap_unchecked_reason(exc)), None


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
    elif result.status == DEFERRED:
        # EMAIL_DAILY_CAP held it (notify.email_cap_decision). Nothing was transmitted, so the
        # claim's attempts+1 is handed back: three deferrals through a long storm must not turn
        # into FAILED, which would be the refusal the cap policy chose NOT to make. No backoff
        # either — a backoff cannot make a rate limit expire; retry_at is when the window frees.
        values.update(
            status=PENDING,
            claimed_at=None,
            claimed_by=None,
            attempts=max(int(job.attempts or 0) - 1, 0),
            next_attempt_at=result.retry_at or now + notify.EMAIL_CAP_WINDOW,
        )
        report.deferred += 1
        final = None
    else:
        raise ValueError(f"dispatch returned unknown status {result.status!r} for row {job.id}")
    if result.smtp_messages > 0:
        # Whatever the outcome — SENT, a retried partial failure, DEAD — these messages LEFT, so
        # EMAIL_DAILY_CAP must count them (notify.email_budget sums this). Only ever written for
        # a real SMTP acceptance, so a mock send's payload is byte-for-byte what it was.
        values["payload_json"] = notify.record_smtp_accepted(job.payload_json, at=now, messages=result.smtp_messages)
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
    if result.cap_note:  # EMAIL_DAILY_CAP note, in the same commit as the outcome it describes
        notify.record_email_cap_note(session, job, result.cap_note, now=now)
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
    events: list[RealtimeEvent] = []
    if job.kind == EMAIL:
        delivery = result.delivery or {"mode": "error", "to": [], "detail": result.last_error or final}
        events = notify.record_email_outcome(session, job, final_status=final, delivery=delivery, now=now)
    elif job.kind == SMS:
        events = notify.record_sms_outcome(session, job, final_status=final, now=now)
    _producer_outcome(session, job, result, final, now)
    return events


def _regulatory_outcome(
    session: Session, job: OutboxRow, row_id: str, result: DispatchResult, final: str, now: datetime
) -> None:
    """``regulatory_notifications`` (§7.6.1): the notice learns whether it was transmitted."""
    from noc_agents.services import regulatory  # lazy: services.regulatory imports this module

    regulatory.record_dispatch_outcome(
        session, job, notification_id=row_id, final_status=final, now=now,
        error=result.last_error, provider=result.provider,
    )


#: Payload field that names a producer's own row → what to tell that row. A table rather than
#: a chain of ``if job.kind ==``: the outcome belongs to the producer, not to the channel, and
#: two producers on one kind would otherwise start competing inside ``_finalize``.
_PRODUCER_OUTCOMES: dict[str, Callable[[Session, OutboxRow, str, DispatchResult, str, datetime], None]] = {
    "regulatory_notification_id": _regulatory_outcome,
}


def _producer_outcome(session: Session, job: OutboxRow, result: DispatchResult, final: str, now: datetime) -> None:
    """Tell the producer's own row what actually happened to its message.

    WHY THIS EXISTS. A producer that stamps "sent" when it *enqueues* is stamping an
    intention, and the gap between the enqueue and the transmit is where DEAD lives. For
    ``regulatory_notifications`` that gap was a false assurance of a statutory obligation:
    the row read SENT with a ``sent_at`` while ``resolve_recipients`` had refused the whole
    dispatch for want of a CA address. So the outcome flows back, once, when it is terminal —
    ``_record_outcome`` returns early on a retry, so this never sees a row still in flight.

    Deliberately a no-op for every row that names no such producer, which is all of them
    unless a lane is switched on: nothing on the golden path changes and no service module is
    imported that a flag-off process would not otherwise import.
    """
    payload = _payload(job)
    for key, handler in _PRODUCER_OUTCOMES.items():
        row_id = payload.get(key)
        if row_id:
            handler(session, job, str(row_id), result, final, now)


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
