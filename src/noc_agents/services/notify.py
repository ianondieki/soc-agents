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
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from noc_agents.adapters.email_smtp import EmailResult, parse_subject_body, send_email
from noc_agents.config import AppSettings, get_settings
from noc_agents.db.models import BroadcastRow, IncidentRow, OutboxRow, WorkNoteRow, new_id
from noc_agents.realtime.hub import RealtimeEvent

if TYPE_CHECKING:  # typing only: orchestrator.outbox imports this module at runtime
    from noc_agents.orchestrator.outbox import DrainReport

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
      resolve must not silently become the demo mailbox.
    """
    ref = (payload.get("recipients_ref") or "").strip()
    if not ref or ref == DEMO_RECIPIENTS_REF:
        return send_email(subject=payload["subject"], body=payload["body"])
    recipients = resolve_recipients(ref, operator_id=payload.get("operator_id"))
    return send_email(subject=payload["subject"], body=payload["body"], to=recipients)


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
