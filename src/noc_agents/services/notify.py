"""Notification producers, and the email/SMS half of the outbox dispatcher.

Producer side — called INSIDE the incident transaction by the BROADCAST node, the HITL
release and the handover route. Each function renders a channel payload and ``enqueue``s
it as an outbox row (spec §7.0.2). Nothing is transmitted here; the row commits with the
incident and a rollback takes it away. ``dispatch_incident_email`` keeps its historical
name: the BROADCAST node still calls it, and raising from it still fails the node closed.

Dispatcher side — called by ``orchestrator.outbox.drain_once`` AFTER commit, never inside
a transaction. ``transmit_email`` resolves the payload's ``recipients_ref`` (see
``resolve_recipients``: a ref that does not resolve refuses the send rather than falling
back to the demo mailbox) and sends through the SMTP adapter; ``record_email_outcome``
and ``record_sms_outcome`` flip the ``BroadcastRow`` drafts, write the
``("BroadcastCommsAgent", "email")`` WorkNote and return the ``email.sent`` /
``email.failed`` event for the dispatcher to publish once the outcome has committed.

Email volume (§7.9.1, §10.3 ✱). Two limits the SMTP relay imposes, both enforced here on the
dispatcher side because that is where recipients become real:

* **≤ 100 recipients per message.** ``transmit_email`` splits a resolved audience into
  ``RECIPIENTS_PER_MESSAGE`` batches, one SMTP message each (Bcc). An audience of 100 or fewer
  is one message, exactly as before;
* **``EMAIL_DAILY_CAP`` messages per rolling 24 h** (default 400 = 80 % of a free Gmail
  account's 500, whose breach is a 24-hour send suspension). ``email_cap_decision`` answers
  "may this row go now?" from the SMTP messages the ``outbox`` rows record as accepted
  (``email_budget``); the dispatcher (``orchestrator/outbox._email_cap_gate``) acts on the
  answer. See ``email_cap_decision`` for the 80 % note, and for why a P1 goes anyway while
  everything else waits for the window.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from noc_agents.adapters.email_smtp import RECIPIENTS_PER_MESSAGE, EmailResult, parse_subject_body, send_email
from noc_agents.config import AppSettings, get_settings
from noc_agents.db.models import BroadcastRow, IncidentRow, OutboxRow, WorkNoteRow, new_id
from noc_agents.realtime.hub import RealtimeEvent
from noc_agents.services.clock import fmt_eat

if TYPE_CHECKING:  # typing only: orchestrator.outbox imports this module at runtime
    from noc_agents.orchestrator.outbox import DrainReport

log = logging.getLogger(__name__)

EMAIL_NOTE_AUTHOR = "BroadcastCommsAgent"
QUEUED = "QUEUED"  # BroadcastRow status between enqueue and dispatch (6 chars: fits String(16))

#: The one ref that is NOT looked up: it means "whatever DEMO_EMAIL_TO / GMAIL_ADDRESS says",
#: which is the adapter's own default and the only recipient the demo has ever had.
DEMO_RECIPIENTS_REF = "DEMO_EMAIL_TO"

#: A resolved recipient must look like an address before it reaches SMTP. Deliberately strict,
#: because the ``recipients_ref`` vocabulary also contains ROLE tokens
#: (``regions.NBI_W.rnio`` → ``"RNIO-NBI-W"``, ``audience:MANAGEMENT``): handing one of those
#: to ``send_email`` is either an SMTP error at best or, in the shape this module had before,
#: a silent fallback to the demo mailbox.
_EMAIL_RE = re.compile(r"^[^@\s,;:<>\"]+@[^@\s,;:<>\"]+\.[A-Za-z]{2,}$")


# --- producer side: render + enqueue ------------------------------------------------------


def render_email_payload(
    inc: IncidentRow, composed_email: str, *, audience: str, broadcast_ids: list[str] | tuple[str, ...] = ()
) -> dict:
    """The EMAIL outbox payload: subject/body plus refs. Recipients are resolved at dispatch."""
    subject, body = parse_subject_body(composed_email)
    if inc.incident_number not in subject:  # the subject always carries the INC for inbox search
        subject = f"[{inc.priority}] {inc.incident_number} | {subject}"
    return {
        "operator_id": inc.operator_id,
        "incident_number": inc.incident_number,
        "audience": audience,
        "subject": subject,
        "body": body,
        "recipients_ref": "DEMO_EMAIL_TO",  # adapters/email_smtp.demo_recipients() at dispatch; never an address here
        "broadcast_ids": list(broadcast_ids),
    }


def render_sms_payload(inc: IncidentRow, message: str, *, audience: str, broadcast_id: str | None = None) -> dict:
    return {
        "operator_id": inc.operator_id,
        "incident_number": inc.incident_number,
        "audience": audience,
        "message": message,
        "recipients_ref": f"audience:{audience}",
        "broadcast_ids": [broadcast_id] if broadcast_id else [],
    }


def dispatch_incident_email(
    session: Session,
    inc: IncidentRow,
    composed_email: str,
    *,
    audience: str = "DEMO",
    run_id: str | None = None,
    hitl_task_id: str | None = None,
    requires_hitl: bool = False,
    approved_by: str | None = None,
    approved_at: datetime | None = None,
    held: bool = False,
    broadcast_ids: list[str] | tuple[str, ...] = (),
) -> dict:
    """Queue the incident email in the transactional outbox (one per incident and audience).

    INSERT OR IGNORE on ``EMAIL:<incident id>:<audience>``. Nothing is sent here: after the
    caller commits, ``outbox.drain_once`` transmits, writes the email WorkNote and publishes
    ``email.sent`` / ``email.failed``. Returns the same keys the old send returned
    (``ok``, ``mode``, ``detail``, ``to``, ``status``, ``sent_at``) plus ``outbox_id``.
    """
    from noc_agents.orchestrator.outbox import enqueue  # lazy: outbox imports this module's dispatcher side

    payload = render_email_payload(inc, composed_email, audience=audience, broadcast_ids=broadcast_ids)
    row = enqueue(
        session,
        kind="EMAIL",
        idempotency_key=f"EMAIL:{inc.id}:{audience}",
        payload=payload,
        incident_id=inc.id,
        run_id=run_id,
        hitl_task_id=hitl_task_id,
        requires_hitl=requires_hitl,
        approved_by=approved_by,
        approved_at=approved_at,
        held=held,
        operator_id=inc.operator_id,
    )
    return _queued(row)


def dispatch_incident_sms(
    session: Session,
    inc: IncidentRow,
    *,
    drafts: list[tuple[str, str, str | None]],
    run_id: str | None = None,
    hitl_task_id: str | None = None,
    requires_hitl: bool = False,
    approved_by: str | None = None,
    approved_at: datetime | None = None,
    held: bool = False,
) -> list[OutboxRow]:
    """Queue one SMS outbox row per ``(audience, message, broadcast_id)`` draft."""
    from noc_agents.orchestrator.outbox import enqueue  # lazy, see dispatch_incident_email

    rows = []
    for audience, message, broadcast_id in drafts:
        rows.append(
            enqueue(
                session,
                kind="SMS",
                idempotency_key=f"SMS:{inc.id}:{audience}",
                payload=render_sms_payload(inc, message, audience=audience, broadcast_id=broadcast_id),
                incident_id=inc.id,
                run_id=run_id,
                hitl_task_id=hitl_task_id,
                requires_hitl=requires_hitl,
                approved_by=approved_by,
                approved_at=approved_at,
                held=held,
                operator_id=inc.operator_id,
            )
        )
    return rows


def dispatch_handover_email(session: Session, subject: str, body: str, *, operator_id: str, shift_id: str) -> dict:
    """Queue the shift-handover email. Each POST is a deliberate send (as before the outbox),
    so the key carries a fresh id rather than de-duplicating on the shift."""
    from noc_agents.orchestrator.outbox import enqueue  # lazy, see dispatch_incident_email

    row = enqueue(
        session,
        kind="EMAIL",
        idempotency_key=f"EMAIL:handover:{operator_id}:{shift_id}:{new_id()}",
        payload={
            "operator_id": operator_id,
            "incident_number": None,
            "audience": "HANDOVER",
            "subject": subject,
            "body": body,
            "recipients_ref": "DEMO_EMAIL_TO",
            "broadcast_ids": [],
        },
        operator_id=operator_id,
    )
    return _queued(row)


def _queued(row: OutboxRow) -> dict:
    return {
        "ok": True,
        "mode": "outbox",
        "status": row.status,
        "outbox_id": row.id,
        "to": [],
        "detail": f"queued outbox row {row.id} ({row.status}); transmitted after commit",
        "sent_at": None,
    }


def handover_email_response(session: Session, outbox_id: str, report: DrainReport | None) -> dict:
    """The ``email`` block of the handover response: the adapter's own result when this
    call drained the row, otherwise the row's queue state."""
    row = session.get(OutboxRow, outbox_id)
    outcome = report.outcomes.get(outbox_id) if report is not None else None
    if row is not None and outcome is not None and outcome.delivery:
        return {
            "ok": row.status == "SENT",
            "mode": outcome.delivery["mode"],
            "detail": outcome.delivery["detail"],
            "to": outcome.delivery["to"],
            "status": row.status,
            "outbox_id": outbox_id,
        }
    status = row.status if row is not None else "UNKNOWN"
    return {
        "ok": status in ("SENT", "DELIVERED"),
        "mode": (row.provider if row is not None and row.provider else "outbox"),
        "detail": (row.last_error if row is not None and row.last_error else f"{status.lower()} in outbox"),
        "to": [],
        "status": status,
        "outbox_id": outbox_id,
    }


# --- dispatcher side: transmit + record the outcome ----------------------------------------


class UnresolvedRecipients(RuntimeError):
    """A ``recipients_ref`` could not be turned into a list of addresses — so nothing is sent.

    Raised out of ``transmit_email`` on purpose rather than returning a failed ``EmailResult``:
    ``outbox.dispatch`` classifies any non-transient exception as **DEAD**, which is the
    outcome this case needs. DEAD is terminal (no retry — a missing config entry will not
    appear during a 3-attempt backoff), it records the ref in ``outbox.last_error``, it
    publishes ``outbox.failed`` to the wallboard, and ``record_email_outcome`` still runs, so
    the incident gets the ``[BroadcastCommsAgent] EMAIL → … | mode=error | to=[(none)]`` work
    note. A human therefore sees an unsent notification instead of a silently misdirected one.
    """


def resolve_recipients(ref: str, *, operator_id: str | None, settings: AppSettings | None = None) -> list[str]:
    """``recipients_ref`` → real addresses from the operator profile, or raise.

    WHY THIS FAILS CLOSED (Phase 4 blocker 1). Until this existed, ``transmit_email`` called
    ``send_email(subject=…, body=…)`` with no ``to``, so the adapter resolved EVERY message —
    including the Communications Authority notice the regulatory lane queues with
    ``recipients_ref="regulatory.recipients.CA"`` — to ``DEMO_EMAIL_TO``. With
    ``EMAIL_ENABLED`` and ``REGULATORY_ENABLED`` both on, that is two failures at once: the
    statutory notification never reaches the regulator, and an incident disclosure marked
    ``scope=RESTRICTED`` lands in a demo inbox.

    So an unresolvable ref REFUSES the dispatch. Falling back to the demo mailbox is not an
    option — it is the bug. Sending to *some* address is not the conservative choice when the
    right address is unknown: an unsent notice is one visible, fixable problem, while a notice
    sent to the wrong mailbox is a disclosure that cannot be taken back and a regulatory
    obligation that looks discharged. The operator fills the address in (it comes from their
    own licence correspondence, not from this repo), and the refusal is what tells them to.

    A ref resolves only when the operator profile's ``notification_recipients`` names it AND
    every value under it looks like an e-mail address. ``settings`` is an override for tests
    and for callers that already hold the resolved profile.
    """
    key = (ref or "").strip()
    if not key:
        raise UnresolvedRecipients("empty recipients_ref: nothing to resolve, refusing to send")
    if settings is None:
        if not operator_id:
            raise UnresolvedRecipients(f"recipients_ref {key!r} carries no operator_id; cannot resolve a profile")
        try:
            settings = get_settings(operator_id)
        except Exception as exc:  # noqa: BLE001 — an unloadable profile is an unresolvable ref
            raise UnresolvedRecipients(f"recipients_ref {key!r}: operator profile {operator_id!r} did not load ({type(exc).__name__})") from exc
    op = settings.operator.operator_id
    register = settings.operator.notification_recipients or {}
    if key not in register:
        raise UnresolvedRecipients(
            f"recipients_ref {key!r} is not in notification_recipients for operator {op!r} "
            f"(config/operators/{op}.yaml); refusing to send rather than falling back to {DEMO_RECIPIENTS_REF}"
        )
    addresses = [str(a).strip() for a in (register.get(key) or []) if str(a).strip()]
    if not addresses:
        raise UnresolvedRecipients(
            f"recipients_ref {key!r} is declared but empty in config/operators/{op}.yaml; "
            f"fill in the real recipient before this lane is switched on"
        )
    # Counted, never quoted: the ref and how many values failed is enough to fix the YAML, and
    # ``last_error`` is a stored, exportable field (§9.5 — an audit record does not repeat its
    # own finding). A role token or a phone number here is a config mistake, not a recipient.
    bad = [a for a in addresses if not _EMAIL_RE.match(a)]
    if bad:
        raise UnresolvedRecipients(
            f"recipients_ref {key!r}: {len(bad)} of {len(addresses)} configured value(s) are not "
            f"e-mail addresses (config/operators/{op}.yaml); refusing to send"
        )
    return addresses


def transmit_email(payload: dict) -> EmailResult:
    """The one SMTP call. Runs in the dispatcher only, after commit.

    ``recipients_ref`` decides where it goes (§7.0.2: a payload carries a REF, never an
    address, so a queued row cannot pin a mailbox that has since changed hands):

    * absent, empty or ``DEMO_EMAIL_TO`` → the historical demo path, byte-for-byte: no ``to``
      argument, so ``adapters/email_smtp`` resolves ``DEMO_EMAIL_TO`` / ``GMAIL_ADDRESS`` and
      still returns ``mode="mock"`` when neither is set. Every incident, handover and HITL
      release email goes this way, and none of them change behaviour;
    * anything else → resolved from the operator profile, or the dispatch is refused (see
      ``resolve_recipients``). There is no third branch on purpose: a ref this process cannot
      resolve must not silently become the demo mailbox. A resolved audience goes out in
      ``RECIPIENTS_PER_MESSAGE`` (100) Bcc batches, one ``send_email`` call each, with the
      payload's header hints (``email_headers``); 100 or fewer is the one call it always was.
    """
    ref = (payload.get("recipients_ref") or "").strip()
    if not ref or ref == DEMO_RECIPIENTS_REF:
        # The demo mailbox, reached through the adapter's NAMED default: omitting ``to`` means
        # ``to=DEMO_MAILBOX`` (adapters/email_smtp), never "no recipients, guess one". The call
        # stays argument-for-argument what it was because tests/unit/test_recipients_ref.py
        # pins it as the no-regression check on the Phase 4 mis-delivery fix. One message, so
        # there is nothing to batch; the demo path carries no header hints.
        return send_email(subject=payload["subject"], body=payload["body"])
    recipients = resolve_recipients(ref, operator_id=payload.get("operator_id"))
    headers = email_headers(payload)
    results: list[EmailResult] = []
    for batch in batch_recipients(recipients):
        kwargs: dict = {"subject": payload["subject"], "body": payload["body"], "to": batch}
        # ``headers`` only when there is one to send, so an audience with no hints makes the
        # same call as before this parameter existed (the spy in test_recipients_ref.py checks
        # that call; a present-but-empty dict would also read as header intent that isn't there).
        if headers:
            kwargs["headers"] = headers
        # Every batch is attempted even after one fails: a 550 on one mailbox in batch 2 must not
        # leave batch 3 unsent. The combined result is a failure if ANY batch failed, so the
        # outbox retries (4xx) or kills (5xx) the row as a whole — and a retry re-sends the
        # batches that DID go. That is the chosen side of the trade: the row is the unit of
        # idempotency, and a duplicated outage notice is recoverable where a silent gap is not.
        results.append(send_email(**kwargs))
    return results[0] if len(results) == 1 else _combine_batches(results)


# --- email volume: batching, header hints, EMAIL_DAILY_CAP (§7.9.1, §10.3 ✱) ----------------

#: ``EMAIL_DAILY_CAP`` — SMTP messages per rolling 24 h. 400 = 80 % of a free @gmail.com
#: account's 500/day; a Workspace sender is 2,000/day, so 1,600 there. ``0`` switches the cap off
#: (a production relay on the operator's own domain has no per-day quota). A value that does not
#: parse is NOT "off": it falls back to the default, because a typo must never remove the guard.
EMAIL_DAILY_CAP_ENV = "EMAIL_DAILY_CAP"
EMAIL_DAILY_CAP_DEFAULT = 400
EMAIL_CAP_WARN_FRACTION = 0.8  # the §7.9.1 fail-soft WorkNote
EMAIL_CAP_WINDOW = timedelta(hours=24)

#: Outbox kinds that spend the same SMTP quota. ICS invites go through the same relay account
#: (§7.5.5: "Gmail SMTP caps apply to invites"), so they are COUNTED; only EMAIL rows are gated
#: here. Literals rather than ``orchestrator.outbox`` constants: that module imports this one.
EMAIL_QUOTA_KINDS = ("EMAIL", "ICS_INVITE")
#: ``outbox.provider`` of a row the relay actually accepted. The dispatcher stores the adapter's
#: ``mode`` there, so a mock send (``EMAIL_ENABLED=false``, no recipient) is ``"mock"`` and
#: correctly costs nothing: it never reached Google.
SMTP_PROVIDER = "smtp"

#: Priorities the cap does not hold back. See ``email_cap_decision``.
EMAIL_CAP_OVERRIDE_PRIORITIES = frozenset({"P1"})

CAP_SEND, CAP_OVERRIDE, CAP_DEFER, CAP_REFUSE = "SEND", "OVERRIDE", "DEFER", "REFUSE"
#: ``outbox.last_error`` prefix on a deferred row. It is how a re-claimed row knows it has been
#: deferred before, so the incident gets ONE "held" note per row, not one per drain tick.
EMAIL_CAP_DEFER_PREFIX = "deferred: EMAIL_DAILY_CAP"
#: The 80 % WorkNote's opening words — also its dedupe key: at most one per rolling window.
EMAIL_CAP_WARNING_PREFIX = f"[{EMAIL_NOTE_AUTHOR}] EMAIL volume warning"

#: Payload field on an EMAIL outbox row: ``[{"at": <naive-UTC ISO>, "messages": n}, ...]``, one
#: entry per attempt in which the relay accepted at least one message. Written by
#: ``orchestrator/outbox._record_outcome`` in the outcome's own commit (``record_smtp_accepted``)
#: and SUMMED by ``email_budget``. A payload field, not a column: no schema change on the golden
#: path, and a mock send never writes it, so every non-SMTP row is byte-for-byte what it was.
SMTP_ACCEPTED_KEY = "smtp_accepted"

#: The renderer's ``provider_params["list_unsubscribe"]`` is a boolean hint (§6.2: the header
#: only for external audiences). What it points at is deployment config: a ``mailto:`` or
#: ``https:`` URI. Unset means no header — an invented unsubscribe target on a regulator notice
#: is a dead link, the same reasoning as ``resolve_recipients`` refusing to guess an address.
LIST_UNSUBSCRIBE_ENV = "EMAIL_LIST_UNSUBSCRIBE"


def batch_recipients(recipients: list[str], size: int = RECIPIENTS_PER_MESSAGE) -> list[list[str]]:
    """Consecutive batches of at most ``size`` (Gmail SMTP: 100), order kept. One batch for ≤ size."""
    if size < 1:
        raise ValueError(f"batch size must be positive, got {size}")
    return [recipients[i : i + size] for i in range(0, len(recipients), size)] or [[]]


def email_headers(payload: dict) -> dict[str, str]:
    """Extra header fields for an EMAIL payload: ``payload["headers"]`` plus renderer hints.

    Carries ``ChannelPayload.provider_params`` through under the same key: a payload that holds
    ``{"provider_params": {"list_unsubscribe": True}}`` gets ``List-Unsubscribe: <uri>`` when
    ``EMAIL_LIST_UNSUBSCRIBE`` is set. An explicit ``payload["headers"]`` entry wins over a hint.
    """
    headers = {str(k): str(v) for k, v in (payload.get("headers") or {}).items() if str(v).strip()}
    hints = payload.get("provider_params") or {}
    if hints.get("list_unsubscribe") and not any(k.lower() == "list-unsubscribe" for k in headers):
        target = (os.getenv(LIST_UNSUBSCRIBE_ENV) or "").strip()
        if target:
            headers["List-Unsubscribe"] = target if target.startswith("<") else f"<{target}>"
    return headers


def _combine_batches(results: list[EmailResult]) -> EmailResult:
    """One ``EmailResult`` for a batched send: ok only if every batch was, failures quoted.

    The failing batches' own details are kept verbatim because the dispatcher reads the SMTP
    reply code out of ``detail`` to decide retry (4xx) or DEAD (5xx). ``accepted`` is the number
    of batches the relay DID take — including on a partial failure, where the row is retried or
    killed but those messages have already left and must be counted against the cap.
    """
    total = sum(len(r.to) for r in results)
    failed = [(i, r) for i, r in enumerate(results, start=1) if not r.ok]
    modes = {r.mode for r in results}
    head = f"{total} recipients in {len(results)} messages of ≤{RECIPIENTS_PER_MESSAGE}"
    if failed:
        detail = f"{head}: {len(failed)} failed — " + "; ".join(f"batch {i}: {r.detail}" for i, r in failed)
        mode = "error"
    else:
        detail = f"{head}: all accepted ({results[0].detail})" if len(modes) == 1 else f"{head}: all accepted"
        mode = modes.pop() if len(modes) == 1 else results[0].mode
    return EmailResult(
        ok=not failed,
        mode=mode,
        detail=detail,
        to=[a for r in results for a in r.to],
        accepted=sum(smtp_messages_accepted(r) for r in results),
    )


def smtp_messages_accepted(result: EmailResult) -> int:
    """Messages the relay accepted for one ``EmailResult``: what the adapter reported, else 1 for
    an ``ok`` SMTP result, else 0 — a mock reached no relay.

    Read with ``getattr``: the transmitter treats its result by shape (``ok``/``mode``/``detail``/
    ``to``), and a result object that predates ``accepted`` — tests/unit/
    test_regulatory_dispatch_outcome.py patches ``transmit_email`` with exactly such a double —
    must not turn a successful send into DEAD by an AttributeError.
    """
    accepted = getattr(result, "accepted", None)
    if accepted is not None:
        return max(int(accepted), 0)
    return 1 if result.ok and result.mode == SMTP_PROVIDER else 0


def email_daily_cap() -> int:
    """``EMAIL_DAILY_CAP`` from the environment; ``0`` = off, unparseable or negative = the default."""
    raw = (os.getenv(EMAIL_DAILY_CAP_ENV) or "").strip()
    if not raw:
        return EMAIL_DAILY_CAP_DEFAULT
    try:
        cap = int(raw)
    except ValueError:
        log.warning("%s=%r is not an integer; enforcing the default %d", EMAIL_DAILY_CAP_ENV, raw, EMAIL_DAILY_CAP_DEFAULT)
        return EMAIL_DAILY_CAP_DEFAULT
    return cap if cap >= 0 else EMAIL_DAILY_CAP_DEFAULT


def email_message_count(payload: dict) -> int:
    """How many SMTP messages this EMAIL payload becomes: 1 for the demo mailbox, one per
    ≤ 100-recipient batch for a resolved ref, and 0 for a ref that will not resolve — that row is
    refused by ``transmit_email`` without a send, so it must not wait for quota it will not use."""
    ref = (payload.get("recipients_ref") or "").strip()
    if not ref or ref == DEMO_RECIPIENTS_REF:
        return 1
    try:
        recipients = resolve_recipients(ref, operator_id=payload.get("operator_id"))
    except UnresolvedRecipients:
        return 0
    return len(batch_recipients(recipients))


@dataclass(frozen=True)
class EmailBudget:
    """The rolling-24 h SMTP count at the moment one row asks to send ``requested`` messages."""

    cap: int
    sent: int  # SMTP MESSAGES the relay accepted in (now - 24 h, now] — not rows
    requested: int
    oldest_sent_at: datetime | None  # naive UTC; when it leaves the window a slot frees
    now: datetime

    @property
    def enabled(self) -> bool:
        return self.cap > 0

    @property
    def warn_at(self) -> int:
        return int(self.cap * EMAIL_CAP_WARN_FRACTION)

    @property
    def after(self) -> int:
        return self.sent + self.requested

    @property
    def exhausted(self) -> bool:
        """True when sending would take the window past the cap (the cap-th message itself is allowed)."""
        return self.enabled and self.after > self.cap

    @property
    def reaches_warning(self) -> bool:
        """True when this send leaves the window at or above 80 %. Level, not edge: whether the
        note is still owed is decided by looking for one (``_warning_written``), so a retried
        send, a second drainer or an ICS invite that moved the count cannot skip or repeat it."""
        return self.enabled and self.requested > 0 and self.after >= self.warn_at

    @property
    def frees_at(self) -> datetime:
        """When the oldest counted send leaves the rolling window — the earliest a slot opens."""
        return (self.oldest_sent_at or self.now) + EMAIL_CAP_WINDOW


def record_smtp_accepted(payload_json: str | None, *, at: datetime, messages: int) -> str:
    """``payload_json`` with one more ``smtp_accepted`` entry. Every other field, and their order,
    is kept exactly (same ``json.dumps`` as ``outbox.enqueue``)."""
    payload = json.loads(payload_json or "{}")
    entries = list(payload.get(SMTP_ACCEPTED_KEY) or [])
    entries.append({"at": at.replace(tzinfo=None).isoformat(), "messages": int(messages)})
    payload[SMTP_ACCEPTED_KEY] = entries
    return json.dumps(payload, default=str)


def _accepted_entries(payload_json: str | None) -> list[tuple[datetime, int]] | None:
    """The row's recorded ``(at, messages)`` pairs; ``None`` when it never recorded any."""
    try:
        raw = json.loads(payload_json or "{}").get(SMTP_ACCEPTED_KEY)
    except (ValueError, AttributeError):
        return None
    if raw is None:
        return None
    entries = []
    for item in raw if isinstance(raw, list) else []:
        try:
            entries.append((datetime.fromisoformat(str(item["at"])).replace(tzinfo=None), int(item["messages"])))
        except (KeyError, TypeError, ValueError):
            continue  # a malformed entry is skipped, never allowed to break the count
    return entries


def email_budget(session: Session, *, now: datetime, requested: int = 1) -> EmailBudget:
    """Count the SMTP MESSAGES the relay accepted in the rolling 24 h, from ``outbox``.

    WHY ``outbox`` AND NOT ``delivery_receipts`` OR THE AUDIT LOG. ``outbox`` is the only table
    that records every send at the moment it left, in the same commit as the outcome, and it
    survives a restart — an in-process counter would reset to 0 on every deploy mid-storm.
    ``delivery_receipts`` has no writer (CONFORMANCE B-11) and SMTP gives no receipt anyway; no
    ``outbox.*`` audit rows exist; the ``external.call`` transfer row is written BEFORE the send
    and so counts attempts, not messages. The count is global, not per operator: the quota
    belongs to the one sender account in ``SMTP_USER`` / ``GMAIL_ADDRESS``.

    MESSAGES, NOT ROWS (review E01). An EMAIL row records what the relay accepted, per attempt,
    in ``payload[SMTP_ACCEPTED_KEY]``, and this SUMS the entries inside the window: a row
    batched to 250 recipients is 3 messages, a row whose batch 2 of 3 failed still counts the
    two that left, and a retry that re-sends them counts them again — because they were sent
    again. A row with no entries (an ICS invite, whose transmitter reports no count, or a row
    sent before entries existed) counts 1 for an SMTP ``sent_at`` in the window, as before.

    CONCURRENT DRAINERS (review E06) — documented, not reserved. Drainers that each claimed a
    DIFFERENT row read the same count, and all may send the last slot: the window can end at most
    ``(concurrent drainers − 1) × requested`` over the cap. The number of drainers is NOT fixed:
    besides the scheduler tick, every synchronous ingest request is one — ``POST /api/v1/events``
    and ``/events/batch`` call ``graph.pipeline.process_event``, which calls ``drain_once`` directly
    on the request's own session, in the request thread, once the lifecycle has committed, while
    ``OUTBOX_SYNC_DRAIN`` is on (the default) — and so is every HITL approval
    (``POST /api/v1/hitl/{id}/approve`` arms ``graph.pipeline.drain_after_commit``, whose
    after-commit listener runs ``drain_once`` in a fresh session, still in that request's thread).
    A storm, which is when the cap is near, is also when there are most of them (a replay with 4
    drainers ended 3 over). What bounds them per process is the threadpool that runs sync handlers — AnyIO's
    default limiter, 40 threads, which this app does not change — so ≤ 40 extra one-message sends
    per uvicorn worker, still inside the 100-message headroom below the provider's limit for one
    worker; each additional worker process adds its own. Counting other drainers' live CLAIMED
    rows as reserved was considered and rejected: ``drain_once`` claims up to 50 rows at once, most
    of which will themselves be deferred, so reserving them would hold back real notices near the
    cap to prevent an overshoot that is bounded as above.
    """
    window_start = now - EMAIL_CAP_WINDOW
    rows = session.execute(
        select(OutboxRow.payload_json, OutboxRow.provider, OutboxRow.sent_at).where(
            OutboxRow.kind.in_(EMAIL_QUOTA_KINDS),
            # A superset: ``_record_outcome`` stamps updated_at with the same ``now`` it records
            # an entry at, so a row with an entry in the window was updated in the window.
            or_(OutboxRow.sent_at > window_start, OutboxRow.updated_at > window_start),
        )
    ).all()
    sent, oldest = 0, None
    for payload_json, provider, sent_at in rows:
        entries = _accepted_entries(payload_json)
        if entries is None:
            entries = [(sent_at, 1)] if provider == SMTP_PROVIDER and sent_at is not None else []
        for at, messages in entries:
            if at > window_start and messages > 0:
                sent += messages
                oldest = at if oldest is None or at < oldest else oldest
    return EmailBudget(cap=email_daily_cap(), sent=sent, requested=requested, oldest_sent_at=oldest, now=now)


def _warning_written(session: Session, now: datetime) -> bool:
    """Whether an 80 % note already exists in the current window, on any incident (the quota is global)."""
    return (
        session.scalar(
            select(WorkNoteRow.id)
            .where(
                WorkNoteRow.author == EMAIL_NOTE_AUTHOR,
                WorkNoteRow.source == "email",
                WorkNoteRow.created_at > now - EMAIL_CAP_WINDOW,
                WorkNoteRow.body.startswith(EMAIL_CAP_WARNING_PREFIX, autoescape=True),
            )
            .limit(1)
        )
        is not None
    )


@dataclass(frozen=True)
class EmailCapDecision:
    """What the dispatcher should do with one EMAIL row. Pure data; ``outbox`` acts on it."""

    action: str  # SEND | OVERRIDE | DEFER | REFUSE
    budget: EmailBudget
    priority: str | None = None
    # WorkNote body for the row's incident, or None. Written by the dispatcher in the OUTCOME's
    # commit and only when the outcome matches: the 80 % and P1-override notes on SENT, the held
    # note on DEFERRED — never ahead of a send that then fails (review E07).
    note: str | None = None
    reason: str | None = None  # outbox.last_error for DEFER / REFUSE
    retry_at: datetime | None = None  # DEFER only


def email_cap_decision(session: Session, row: OutboxRow, *, now: datetime) -> EmailCapDecision:
    """Apply ``EMAIL_DAILY_CAP`` to one EMAIL row that is about to go over SMTP.

    * **below 80 %** → SEND, nothing written;
    * **at or above 80 %, and no 80 % note yet in this window** → SEND plus the §7.9.1 fail-soft
      WorkNote, written once the send has succeeded. Looking for an existing note (rather than
      firing on the one send that crosses the line) is what makes it exactly one per window:
      a crossing send that fails and retries does not write a second, and a crossing that
      happened on an ICS invite or a handover mail (no incident to annotate) is still noted by
      the next incident mail. Drainers racing on the same crossing can each write one, so a
      window holds at most one note per concurrent drainer — the same race, and the same
      unfixed-number-of-drainers caveat, as the count itself (see ``email_budget``);
    * **past the cap, P1** → OVERRIDE: sent anyway, with a WorkNote saying so. This includes a
      P1 whose audience alone needs more messages than the whole cap: the P1 rule is decided
      before any refusal;
    * **past the cap, anything else** → DEFER: back to PENDING until the rolling window frees a
      slot, with one WorkNote the first time;
    * **below P1, an audience that needs more messages than the whole cap** → REFUSE (DEAD): it
      can never fit, so waiting would hold it forever.

    WHY A P1 IS NEVER HELD. The cap is a guard rail set BELOW the provider's cliff (400 of
    Gmail's 500) to protect the account from a 24-hour suspension, and the reason that
    suspension matters is that it would stop the next P1 notice. Holding a P1 to protect the
    thing that exists to carry P1s inverts the priority. P1 volume is small (a handful of
    incidents a shift, not hundreds), and the 100-message headroom between the cap and the
    cliff is there to be spent on exactly this; every override is written on the incident so
    the spending is visible. If P1 traffic alone approaches the provider limit, the answer is
    a relay on the operator's domain (§7.9.1), not a held outage notice.

    WHY DEFER AND NOT DEAD FOR THE REST. DEAD is for a condition a retry cannot change — the
    unresolvable ``recipients_ref``, which is why that case refuses. A daily cap is the
    opposite: it clears on a clock, and ``frees_at`` says exactly when. A refused P3/P4 notice
    is a notification nobody will ever re-send; a deferred one arrives late, and "site still
    down, FE assigned" is still true hours later. Refusing would also put one ``outbox.failed``
    per message on the wallboard — in a storm, hundreds of red rows burying the 80 % note that
    is the actual signal. The deferral is not silent: the row stays PENDING with the reason in
    ``last_error`` and the time in ``next_attempt_at``, the incident gets a note, and it does
    not burn an attempt (``orchestrator/outbox._record_outcome``), so a long storm cannot turn
    deferral into a quiet FAILED.

    Reads only. The caller ends the transaction; the dispatcher writes ``note`` with the outcome.
    """
    payload = json.loads(row.payload_json or "{}")
    budget = email_budget(session, now=now, requested=email_message_count(payload))
    if not budget.enabled or budget.requested == 0:
        return EmailCapDecision(CAP_SEND, budget)
    if not budget.exhausted:
        note = None
        if budget.reaches_warning and not _warning_written(session, now):
            note = (
                f"{EMAIL_CAP_WARNING_PREFIX}: {budget.after} of {EMAIL_DAILY_CAP_ENV}="
                f"{budget.cap} SMTP messages in the last 24 h ({budget.after * 100 // budget.cap} %). "
                f"Past {budget.cap}, notices below P1 wait for the rolling window; P1 still sends. "
                f"A free Gmail sender is suspended for 24 h at 500."
            )
        return EmailCapDecision(CAP_SEND, budget, note=note)

    priority = incident_priority(session, row.incident_id)
    state = f"{EMAIL_DAILY_CAP_ENV}={budget.cap} reached ({budget.sent} SMTP messages in the last 24 h)"
    # The P1 override comes FIRST, before any refusal (round-3 C4). Checked after the "can never
    # fit" refusal, it let the cap kill a P1 DEAD — an audience that alone needs more messages
    # than the cap, even in an empty window — which is exactly what "a P1 is never held" rules
    # out. Such a P1 goes with the override note, like a P1 in a full window; the note names the
    # message count, so an audience too big for the relay is visible to whoever fixes the config.
    if priority in EMAIL_CAP_OVERRIDE_PRIORITIES:
        # Three different things put a P1 here, and the note says which (rounds 4 and 5). Only the
        # first is "cap reached": in the other two the window still had room when this row was read.
        if budget.requested > budget.cap:  # bigger than a whole day's allowance on its own
            note = (
                f"[{EMAIL_NOTE_AUTHOR}] This {priority} notice alone needs {budget.requested} messages, more than "
                f"{EMAIL_DAILY_CAP_ENV}={budget.cap} allows in a day ({budget.sent} already sent in the last 24 h); "
                f"it was SENT anyway because the volume cap never holds a {priority}."
            )
        elif budget.sent >= budget.cap:  # the window was already full before this row
            note = (
                f"[{EMAIL_NOTE_AUTHOR}] {state}; this {priority} notice was SENT anyway "
                f"({budget.requested} message(s)): the volume cap never holds a {priority}."
            )
        else:  # it is THIS send that crosses the cap
            note = (
                f"[{EMAIL_NOTE_AUTHOR}] This {priority} notice takes the window over {EMAIL_DAILY_CAP_ENV}="
                f"{budget.cap} ({budget.sent} sent in the last 24 h + {budget.requested} message(s)); "
                f"it was SENT anyway because the volume cap never holds a {priority}."
            )
        return EmailCapDecision(CAP_OVERRIDE, budget, priority=priority, note=note)
    if budget.requested > budget.cap:
        reason = (
            f"refused: this notice needs {budget.requested} messages, more than {EMAIL_DAILY_CAP_ENV}="
            f"{budget.cap} allows in a whole day; it can never fit through this relay"
        )
        # No cap note: DEAD goes through ``record_email_outcome``, which already writes the
        # ``mode=error`` note quoting this reason, and publishes ``outbox.failed``.
        return EmailCapDecision(CAP_REFUSE, budget, priority=priority, reason=reason)

    retry_at = budget.frees_at
    reason = f"{EMAIL_CAP_DEFER_PREFIX}={budget.cap} reached ({budget.sent} in 24 h); retry at {retry_at:%Y-%m-%dT%H:%M:%S}Z"
    first = not (row.last_error or "").startswith(EMAIL_CAP_DEFER_PREFIX)
    note = (
        f"[{EMAIL_NOTE_AUTHOR}] {state}; this {priority or 'non-incident'} notice is HELD, not dropped: "
        f"it stays queued and goes at {fmt_eat(retry_at, '%Y-%m-%d %H:%M')}, when the rolling window frees a slot."
        if first
        else None
    )
    return EmailCapDecision(CAP_DEFER, budget, priority=priority, note=note, reason=reason, retry_at=retry_at)


def record_email_cap_note(session: Session, row: OutboxRow, note: str | None, *, now: datetime) -> bool:
    """Add a cap WorkNote to the row's incident, stamped at the drain's ``now`` (the window the
    80 % dedupe looks in). The caller commits — the dispatcher does, with the outcome.

    A row with no incident (the handover mail) has nowhere to put a note, so the finding goes to
    the log instead. Returns whether a note was added.
    """
    if not note:
        return False
    if row.incident_id is None:
        log.warning("outbox: %s (row %s, no incident to annotate)", note, row.id)
        return False
    session.add(
        WorkNoteRow(
            incident_id=row.incident_id,
            author=EMAIL_NOTE_AUTHOR,
            author_role="AGENT",
            body=note,
            source="email",
            created_at=now,
        )
    )
    return True


def cap_unchecked_note(priority: str | None, error: BaseException) -> str:
    """The trace a fail-open P1 send leaves on its incident (review E05)."""
    return (
        f"[{EMAIL_NOTE_AUTHOR}] {EMAIL_DAILY_CAP_ENV} could not be checked ({type(error).__name__}); "
        f"this {priority or 'P1'} notice was SENT without counting it against the cap."
    )


def cap_unchecked_reason(error: BaseException) -> str:
    """``last_error`` for a non-P1 row held because the count could not be read (review E05)."""
    return f"{EMAIL_DAILY_CAP_ENV} could not be checked ({type(error).__name__}); nothing was transmitted"


#: ``"[P1] INC000123 | …"`` — the subject every incident producer writes (``compose_email`` /
#: ``alerts.email_subject``, ``render_email_payload``'s own prefix, ``regulatory.notice_text``).
_SUBJECT_PRIORITY_RE = re.compile(r"^\s*\[(P[1-4])\]", re.IGNORECASE)


def queued_priority(payload: dict | str | None) -> str | None:
    """The priority an EMAIL row was QUEUED with, read from its own subject — no database.

    Only the fallback for the one moment the live lookup (``incident_priority``) cannot run:
    when the cap count failed and the same unreadable database fails the priority read too
    (round-3 C5). The live priority is preferred everywhere else, because an override or a
    re-evaluation after enqueue changes it. Accepts the payload dict or its JSON; anything that
    does not parse, or a subject without the ``[Px]`` prefix (the handover mail), is ``None``.
    """
    if isinstance(payload, str) or payload is None:
        try:
            payload = json.loads(payload or "{}")
        except ValueError:
            return None
    match = _SUBJECT_PRIORITY_RE.match(str((payload or {}).get("subject") or "")) if isinstance(payload, dict) else None
    return match.group(1).upper() if match else None


def incident_priority(session: Session, incident_id: str | None) -> str | None:
    """The incident's CURRENT priority (SEVERITY's, never a payload field or a subject line)."""
    if not incident_id:
        return None
    inc = session.get(IncidentRow, incident_id)
    if inc is None:
        return None
    return (inc.priority or "").upper() or None


def record_email_outcome(
    session: Session, row: OutboxRow, *, final_status: str, delivery: dict, now: datetime
) -> list[RealtimeEvent]:
    """Flip the EMAIL drafts, write the audit note, return the event to publish after commit.

    ``delivery`` carries the adapter's ``mode`` / ``to`` / ``detail`` so the note body and the
    ``email.sent`` payload are byte-for-byte what the in-transaction send used to produce.
    """
    payload = json.loads(row.payload_json or "{}")
    ok = final_status == "SENT"
    status = "SENT" if ok else "FAILED"
    _flip_broadcasts(session, payload.get("broadcast_ids", []), status, now)
    if row.incident_id is None:  # handover mail: no incident to annotate
        return []
    note = (
        f"[{EMAIL_NOTE_AUTHOR}] EMAIL → {payload.get('audience')} | mode={delivery['mode']} | "
        f"to={delivery['to'] or ['(none)']} | {delivery['detail']}"
    )
    session.add(
        WorkNoteRow(
            incident_id=row.incident_id,
            author=EMAIL_NOTE_AUTHOR,
            author_role="AGENT",
            body=note,
            source="email",
        )
    )
    return [
        RealtimeEvent(
            type="email.sent" if ok else "email.failed",
            operator_id=row.operator_id,
            incident_id=row.incident_id,
            payload={
                "incident_number": payload.get("incident_number"),
                "mode": delivery["mode"],
                "to": delivery["to"],
                "detail": delivery["detail"],
                "status": status,
            },
        )
    ]


def record_sms_outcome(session: Session, row: OutboxRow, *, final_status: str, now: datetime) -> list[RealtimeEvent]:
    payload = json.loads(row.payload_json or "{}")
    _flip_broadcasts(session, payload.get("broadcast_ids", []), "SENT" if final_status == "SENT" else "FAILED", now)
    return []


def _flip_broadcasts(session: Session, ids: list[str], status: str, now: datetime) -> None:
    if not ids:
        return
    for b in session.scalars(select(BroadcastRow).where(BroadcastRow.id.in_(ids))):
        b.status = status
        b.sent_at = now if status == "SENT" else None
