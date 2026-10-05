"""Close the loop (docs/CLOSE_THE_LOOP.md): customers hear back, can check on a complaint, and say
when service is still down -- and the floor can see how well the promise is kept.

The desk links an outage complaint to the NOC ticket and promises "we will tell you when service
is restored". This module keeps that promise and measures it:

* :func:`on_incident_restored` -- called by the NOC's restore, mark-restored note and close routes
  (``main.py``), inside their transaction and in a SAVEPOINT of the route's, so a failure here is
  rolled back on its own and never takes the restore or the close with it. It finds every number
  that complained about the incident and has not been told, and either sends one SMS per number
  now or raises one ``APPROVE_CUSTOMER_UPDATE`` card with the SMS ``HELD`` behind it, on the
  floor's autonomy ladder (``policy.yaml`` -> ``customer_updates``).
* :func:`approve_customer_update` / :func:`reject_customer_update` -- the card's two outcomes,
  run inside the HITL route's own transaction (``main.hitl_approve`` / ``hitl_reject``).
* :func:`find_tracked`, :func:`tracked`, :func:`report_still_down` -- the public Track page.
* :func:`loop_metrics`, :func:`outages`, :func:`incident_customers` -- the staff read side.

**What "already told" means.** One SMS per phone number per incident, ever: the outbox key is
``support-restore:{incident_id}:{msisdn_hash}`` and the outbox inserts it at most once. A number
is skipped when the complaint says it was told about this incident (``told_incident_id``) or when
a row under that key already exists in ANY state -- sent, held behind a card, or suppressed by a
rejection. So a second restore, a close after a restore, or two approvals of one card can never
send twice, and a rejected notice is final for the numbers it covered (decision in the contract).

**Where the SMS rows point.** They carry the card's id (``hitl_task_id``) and NOT the incident's:
``outbox.release_held`` and ``services/hitl.suppress_held_outbox`` act on every HELD row of an
incident when its broadcast card is decided, and a customer notice must be released or suppressed
by its own card only (the handover precedent, ``services/handover.release_handover``). The payload
names the complaint by id and reference; the number itself never enters the outbox, the SMS
adapter (a mock today) resolves it from the complaint.

**Customer words only** on the Track page: it is built from the facts the customer already holds
(their own words, our replies, the place they named, the ticket number we told them), never from
the staff trace, the same rule as the public view in :mod:`views`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import statistics
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from noc_agents.config import AppSettings, get_settings
from noc_agents.db.models import HitlTaskRow, IncidentRow, OutboxRow, WorkNoteRow, utcnow
from noc_agents.db.models_support import (
    SupportComplaintRow,
    SupportMessageRow,
    SupportNoticeRow,
    SupportStepRow,
    SupportSurgeRow,
    SupportToolCallRow,
)
from noc_agents.domain.enums import HitlTaskType, IncidentStatus
from noc_agents.orchestrator import outbox
from noc_agents.realtime.commit_hook import buffer_event
from noc_agents.realtime.hub import RealtimeEvent
from noc_agents.services.clock import iso_z
from noc_agents.services.gsm7 import segments_for
from noc_agents.services.hitl import sync_incident_hitl_scalars
from noc_agents.services.lifecycle import (
    RESTORE_SOURCE_ALARM_CLEAR,
    RESTORE_SOURCE_MARK,
    RESTORE_SOURCE_SUPERVISOR,
)
from noc_agents.support import desk
from noc_agents.support.escalation import Escalation, customer_facing, holding_reply
from noc_agents.support.policy import SupportPolicy, load_policy
from noc_agents.support.text import clean, normalise
from noc_agents.support.vocab import HUMAN_QUEUE, OUTCOME_FOR_STATUS

log = logging.getLogger(__name__)

AGENT = "followup"
#: Who raises the loop's cards. An agent, so raiser != approver never stops the supervisor who
#: restored the incident from approving the message about it (decision in the contract).
RAISED_BY = "agent:SupportFollowup"
CUSTOMER_UPDATE_TASK_TYPE = HitlTaskType.APPROVE_CUSTOMER_UPDATE.value

#: Restore sources that are a person (or the NMS) saying service is back. ``VENDOR_NOTE_INFERRED``
#: is a regex over a note -- a guess -- and tells nobody: the outage waits for a confirmed restore
#: or the close.
TELL_SOURCES: frozenset[str] = frozenset({RESTORE_SOURCE_SUPERVISOR, RESTORE_SOURCE_MARK, RESTORE_SOURCE_ALARM_CLEAR})
#: ``restore_source`` on a notice the CLOSE triggered (the incident's own source was a guess or none).
CLOSE_SOURCE = "CLOSED"
CLOSED_INCIDENT = frozenset({IncidentStatus.RESTORED.value, IncidentStatus.CLOSED.value, IncidentStatus.CANCELLED.value})

TOLD_TEXT = {
    "en": "Service is back in {place}. Your complaint {ref} is now closed. Still down? Tell us at {track_url}",
    "sw": "Huduma imerejea {place}. Lalamiko lako {ref} limefungwa. Bado haifanyi kazi? Tuambie hapa {track_url}",
}
BASE_URL_ENV = "SUPPORT_PUBLIC_BASE_URL"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
CARD_SAMPLE_MAX = 10
CLOSURE_SERVICE_RESTORED = "service_restored"
STILL_DOWN_REASON_CODE = "still_down_after_restore"

EVENT_CUSTOMERS_TOLD = "support.customers_told"
EVENT_STILL_DOWN = "support.still_down"

NOT_FOUND = "We could not find a complaint with that reference and number."
MAX_NOTE_CHARS = 1000


class TrackConflict(Exception):
    """A still-down report that the rules do not allow now (the API answers 409 with the sentence)."""


# ------------------------------------------------------------------------------- helpers


def public_base_url() -> str:
    """``SUPPORT_PUBLIC_BASE_URL`` (default ``http://127.0.0.1:8000``), read at call time."""
    return (os.getenv(BASE_URL_ENV) or DEFAULT_BASE_URL).strip().rstrip("/") or DEFAULT_BASE_URL


def track_url(ref: str) -> str:
    return f"{public_base_url()}/track?ref={ref}"


def msisdn_hash(msisdn: str) -> str:
    """A stable token for a number in an outbox key, so the key itself never holds the number."""
    return hashlib.sha256(msisdn.encode("utf-8")).hexdigest()[:16]


def restore_key(incident_id: str, msisdn: str) -> str:
    return f"support-restore:{incident_id}:{msisdn_hash(msisdn)}"


def sms_language(language: str | None) -> str:
    """``sw`` gets Kiswahili; English and mixed get English."""
    return "sw" if language == "sw" else "en"


def place_label(name: str | None) -> str:
    return " ".join(word.capitalize() for word in (name or "").split())


def complaint_place(row: SupportComplaintRow) -> str | None:
    """The place the customer named (normalised). Rows from before schema v11 have no ``place``
    column value; their triage detail still lists what the gazetteer found."""
    if row.place:
        return row.place
    try:
        places = json.loads(row.triage_json or "{}").get("places") or []
    except ValueError:
        return None
    return places[0].get("name") if places and isinstance(places[0], dict) else None


def incident_area(inc: IncidentRow, settings: AppSettings) -> str:
    """The incident's area in the floor's words, for a customer who named no place: the region's
    label ("Nairobi East"), else the county, else the site's name."""
    region = settings.operator.regions.get(inc.region_code or "")
    return (region.label if region else None) or inc.county or inc.site_name or "your area"


def _local(dt: datetime | None, timezone: str) -> str:
    if dt is None:
        return "--:--"
    return f"{dt.replace(tzinfo=ZoneInfo('UTC')).astimezone(ZoneInfo(timezone)):%H:%M}"


def told_text(language: str, *, place: str, ref: str) -> str:
    return TOLD_TEXT[sms_language(language)].format(place=place, ref=ref, track_url=track_url(ref))


def _p90(values: list[float]) -> float:
    """Nearest-rank 90th percentile: the smallest value with at least 90 % of values at or below it."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)]


# --------------------------------------------------------------------------- recipients


@dataclass
class _Recipient:
    """One phone number to tell, with every untold complaint it made about the incident."""

    msisdn: str
    complaints: list[SupportComplaintRow]  # newest first
    language: str = "en"
    place: str = ""
    text: str = ""
    key: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def lead(self) -> SupportComplaintRow:
        """The complaint the SMS names: the number's most recent one."""
        return self.complaints[0]


def _untold(session: Session, inc: IncidentRow) -> list[SupportComplaintRow]:
    return list(session.scalars(
        select(SupportComplaintRow).where(
            SupportComplaintRow.operator_id == inc.operator_id,
            SupportComplaintRow.linked_incident_id == inc.id,
            or_(SupportComplaintRow.told_incident_id.is_(None), SupportComplaintRow.told_incident_id != inc.id),
        ).order_by(SupportComplaintRow.created_at.desc(), SupportComplaintRow.ref.desc())
    ).all())


def _recipients(session: Session, inc: IncidentRow, settings: AppSettings) -> list[_Recipient]:
    """Every number with an untold complaint about ``inc`` and no restore SMS under its key yet."""
    by_number: OrderedDict[str, list[SupportComplaintRow]] = OrderedDict()
    for row in _untold(session, inc):
        by_number.setdefault(row.msisdn, []).append(row)
    if not by_number:
        return []
    keys = {msisdn: restore_key(inc.id, msisdn) for msisdn in by_number}
    taken = set(session.scalars(select(OutboxRow.idempotency_key).where(OutboxRow.idempotency_key.in_(keys.values()))).all())
    out: list[_Recipient] = []
    for msisdn, rows in by_number.items():
        if keys[msisdn] in taken:
            continue  # held behind a card, sent, or suppressed by a rejection: never a second SMS
        rec = _Recipient(msisdn=msisdn, complaints=rows, key=keys[msisdn])
        rec.language = sms_language(rec.lead.language)
        rec.place = place_label(complaint_place(rec.lead)) or incident_area(inc, settings)
        rec.text = told_text(rec.language, place=rec.place, ref=rec.lead.ref)
        out.append(rec)
    return out


def _place_summary(recipients: list[_Recipient]) -> str:
    counts: dict[str, int] = {}
    for rec in recipients:
        counts[rec.place] = counts.get(rec.place, 0) + 1
    names = [name for name, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    if len(names) <= 3:
        return ", ".join(names[:-1]) + (" and " if len(names) > 1 else "") + names[-1]
    return f"{', '.join(names[:3])} and {len(names) - 3} more"


def _sample_text(recipients: list[_Recipient], language: str) -> str:
    """What one customer in ``language`` will read: a real recipient's message when there is one,
    else the template filled with the first recipient's place and reference."""
    for rec in recipients:
        if rec.language == language:
            return rec.text
    first = recipients[0]
    return told_text(language, place=first.place, ref=first.lead.ref)


def _sms_payload(rec: _Recipient, inc: IncidentRow, *, purpose: str, notice_id: str | None = None) -> dict[str, Any]:
    """The outbox row's payload: the complaint by id and reference, never the number."""
    return {
        "operator_id": inc.operator_id,
        "audience": "customer",
        "purpose": purpose,
        "complaint_id": rec.lead.id,
        "complaint_ref": rec.lead.ref,
        "complaint_ids": [row.id for row in rec.complaints],
        "msisdn_masked": rec.lead.msisdn_masked,
        "incident_id": inc.id,
        "incident_number": inc.incident_number,
        "language": rec.language,
        "body": rec.text,
        "segments": segments_for(rec.text),
        "notice_id": notice_id,
    }


# ------------------------------------------------------------------------------ telling


def _supersede_pending_calls(session: Session, row: SupportComplaintRow, now: datetime) -> None:
    for call in session.scalars(select(SupportToolCallRow).where(
            SupportToolCallRow.complaint_id == row.id, SupportToolCallRow.status == "needs_approval")).all():
        call.status, call.decided_at = "rejected", now
        call.policy = "superseded: the complaint was closed when service was restored"


def _mark_told(session: Session, row: SupportComplaintRow, inc: IncidentRow, *, text: str, now: datetime,
               summary: str, detail: dict[str, Any]) -> None:
    """The complaint after telling: closed (service restored), the SMS in its conversation, a step."""
    _supersede_pending_calls(session, row, now)
    row.status = "closed"
    row.outcome = OUTCOME_FOR_STATUS.get(row.status, row.outcome)
    row.closure_reason = CLOSURE_SERVICE_RESTORED
    row.told_restored_at = now
    row.told_incident_id = inc.id
    row.updated_at = now
    session.add(SupportMessageRow(complaint_id=row.id, author="agent", name=desk.AGENT_NAME, body=text, at=now, channel="sms"))
    desk._human_step(session, row, "told_restored", summary, detail, now, agent=AGENT)
    desk._emit(session, desk.EVENT_UPDATED, row, told_restored=True)


def _told_summary(inc: IncidentRow, place: str, timezone: str, *, closed: bool) -> str:
    """"Told the customer service is back in Kayole (INC000004 restored 16:40)", in the floor's time."""
    if closed:
        return f"Told the customer service is back in {place} ({inc.incident_number} closed {_local(inc.closed_at, timezone)})"
    return f"Told the customer service is back in {place} ({inc.incident_number} restored {_local(inc.restored_at, timezone)})"


def _tell_number(session: Session, rec: _Recipient, inc: IncidentRow, *, now: datetime, timezone: str, closed: bool,
                 extra: dict[str, Any] | None = None) -> None:
    for row in rec.complaints:
        _mark_told(session, row, inc, text=rec.text, now=now, summary=_told_summary(inc, rec.place, timezone, closed=closed),
                   detail={"incident_id": inc.id, "incident_number": inc.incident_number, "outbox_key": rec.key,
                           "sms_names": rec.lead.ref, "language": rec.language, **(extra or {})})


def _customers_told_event(session: Session, inc: IncidentRow, count: int) -> None:
    buffer_event(session, RealtimeEvent(type=EVENT_CUSTOMERS_TOLD, operator_id=inc.operator_id, incident_id=inc.id,
                                        payload={"incident_number": inc.incident_number, "count": count}))


def on_incident_restored(
    session: Session,
    inc: IncidentRow,
    *,
    trigger: str,
    actor: str | None = None,
    now: datetime | None = None,
    settings: AppSettings | None = None,
    policy: SupportPolicy | None = None,
) -> SupportNoticeRow | None:
    """Tell the incident's customers service is back, or raise the card that will.

    ``trigger`` is ``"restore"`` (the incident is RESTORED; only a confirmed source tells anyone)
    or ``"close"`` (the incident is CLOSED; tells whoever no notice reached yet). Runs inside the
    caller's transaction and transmits nothing: the caller drains the outbox after its commit
    when the notice was ``sent``. Returns the notice, or None when there was nobody to tell.
    """
    if trigger == "restore":
        if inc.status != IncidentStatus.RESTORED.value or inc.restored_source not in TELL_SOURCES:
            return None  # a guessed restore waits for a confirmed one, or for the close
    elif trigger == "close":
        if inc.status != IncidentStatus.CLOSED.value:
            return None
    else:
        raise ValueError(f"trigger must be 'restore' or 'close', not {trigger!r}")
    settings = settings or get_settings()
    policy = policy or load_policy()
    now = now or utcnow()
    recipients = _recipients(session, inc, settings)
    if not recipients:
        return None
    level = settings.operator.autonomy_level
    waits = policy.customer_updates.waits(level, inc.priority, len(recipients))
    source = inc.restored_source if trigger == "restore" else CLOSE_SOURCE
    languages = {"en": sum(r.language == "en" for r in recipients), "sw": sum(r.language == "sw" for r in recipients)}
    notice = SupportNoticeRow(
        operator_id=inc.operator_id, incident_id=inc.id, state="sent", recipients=len(recipients),
        languages_json=json.dumps(languages), text_en=_sample_text(recipients, "en"), text_sw=_sample_text(recipients, "sw"),
        restore_source=source, created_at=now,
    )
    session.add(notice)
    session.flush()
    if waits:
        card = HitlTaskRow(incident_id=inc.id, operator_id=inc.operator_id, task_type=CUSTOMER_UPDATE_TASK_TYPE,
                           entity_type="support_notice", entity_id=notice.id, created_by=RAISED_BY, status="PENDING")
        card.proposed_payload = {
            "kind": "restore_notice",
            "incident_number": inc.incident_number,
            "place_summary": _place_summary(recipients),
            "recipients": len(recipients),
            "languages": languages,
            "text_en": notice.text_en,
            "text_sw": notice.text_sw,
            "segments_en": segments_for(notice.text_en or ""),
            "segments_sw": segments_for(notice.text_sw or ""),
            "sample": [{"ref": r.lead.ref, "msisdn_masked": r.lead.msisdn_masked, "language": r.language}
                       for r in recipients[:CARD_SAMPLE_MAX]],
            "restore_source": source,
        }
        session.add(card)
        session.flush()
        notice.state, notice.card_id = "awaiting_approval", card.id
        for rec in recipients:
            outbox.enqueue(session, kind=outbox.SMS, idempotency_key=rec.key,
                           payload=_sms_payload(rec, inc, purpose="support_restore", notice_id=notice.id),
                           hitl_task_id=card.id, requires_hitl=True, held=True, operator_id=inc.operator_id)
        sync_incident_hitl_scalars(session, inc)
        buffer_event(session, RealtimeEvent(
            type="hitl.created", operator_id=inc.operator_id, incident_id=inc.id,
            payload={"task_id": card.id, "task_type": CUSTOMER_UPDATE_TASK_TYPE, "incident_number": inc.incident_number,
                     "recipients": len(recipients)}))
        log.info("support: %s restore notice for %d numbers waits on card %s (%s, %s)",
                 inc.incident_number, len(recipients), card.id, level, inc.priority)
        return notice
    notice.sent_at = now
    for rec in recipients:
        outbox.enqueue(session, kind=outbox.SMS, idempotency_key=rec.key,
                       payload=_sms_payload(rec, inc, purpose="support_restore", notice_id=notice.id),
                       operator_id=inc.operator_id)
        _tell_number(session, rec, inc, now=now, timezone=settings.operator.timezone, closed=source == CLOSE_SOURCE,
                     extra={"sent_by": f"policy:{level}", "notice_id": notice.id})
    _customers_told_event(session, inc, len(recipients))
    session.flush()
    return notice


def _notice_for(session: Session, task: HitlTaskRow) -> SupportNoticeRow | None:
    return session.scalar(select(SupportNoticeRow).where(
        SupportNoticeRow.card_id == task.id, SupportNoticeRow.operator_id == task.operator_id))


def approve_customer_update(session: Session, task: HitlTaskRow, *, approved_by: str, approved_at: datetime,
                            settings: AppSettings | None = None) -> int:
    """The card was approved: its HELD SMS go PENDING (approved by the decider) and their complaints
    are marked told. Addressed by the card's id only. Returns the numbers told; the caller drains
    the outbox after its commit. Idempotent: a row that is not HELD is not touched again, a
    complaint already told about the incident is not told twice."""
    settings = settings or get_settings()
    inc = session.get(IncidentRow, task.incident_id) if task.incident_id else None
    if inc is None:
        return 0
    session.execute(
        update(OutboxRow)
        .where(OutboxRow.hitl_task_id == task.id, OutboxRow.status == outbox.HELD)
        .values(status=outbox.PENDING, approved_by=approved_by, approved_at=approved_at, updated_at=utcnow())
    )
    notice = _notice_for(session, task)
    closed = notice is not None and notice.restore_source == CLOSE_SOURCE
    told = 0
    for row in session.scalars(select(OutboxRow).where(OutboxRow.hitl_task_id == task.id, OutboxRow.kind == outbox.SMS)).all():
        if row.status in (outbox.HELD, outbox.SUPPRESSED):
            continue
        payload = json.loads(row.payload_json or "{}")
        complaints = list(session.scalars(select(SupportComplaintRow).where(
            SupportComplaintRow.id.in_(payload.get("complaint_ids") or []),
            SupportComplaintRow.operator_id == inc.operator_id,
            or_(SupportComplaintRow.told_incident_id.is_(None), SupportComplaintRow.told_incident_id != inc.id),
        ).order_by(SupportComplaintRow.created_at.desc())).all())
        if not complaints:
            continue
        rec = _Recipient(msisdn=complaints[0].msisdn, complaints=complaints, language=payload.get("language", "en"),
                         place="", text=payload.get("body", ""), key=row.idempotency_key)
        rec.place = place_label(complaint_place(complaints[0])) or incident_area(inc, settings)
        _tell_number(session, rec, inc, now=approved_at, timezone=settings.operator.timezone, closed=closed,
                     extra={"approved_by": approved_by, "card_id": task.id})
        told += 1
    if notice is not None:
        notice.state, notice.sent_at = "sent", approved_at
        notice.decided_by, notice.decided_at = approved_by, approved_at
    if told:
        _customers_told_event(session, inc, told)
    session.flush()
    return told


def reject_customer_update(session: Session, task: HitlTaskRow, *, rejected_by: str, reason: str, at: datetime) -> int:
    """The card was rejected: its HELD SMS are SUPPRESSED, nobody is told, the reason is kept on the
    notice and in each complaint's trace. Returns the rows suppressed."""
    inc = session.get(IncidentRow, task.incident_id) if task.incident_id else None
    suppressed = session.execute(
        update(OutboxRow)
        .where(OutboxRow.hitl_task_id == task.id, OutboxRow.status == outbox.HELD)
        .values(status=outbox.SUPPRESSED, last_error=f"customer update rejected by {rejected_by}: {reason}"[:2000],
                updated_at=utcnow())
    ).rowcount
    notice = _notice_for(session, task)
    if notice is not None:
        notice.state, notice.decided_by, notice.decided_at, notice.reason = "rejected", rejected_by, at, reason
    if inc is not None:
        ids: list[str] = []
        for row in session.scalars(select(OutboxRow).where(OutboxRow.hitl_task_id == task.id)).all():
            ids.extend(json.loads(row.payload_json or "{}").get("complaint_ids") or [])
        for row in session.scalars(select(SupportComplaintRow).where(
                SupportComplaintRow.id.in_(ids), SupportComplaintRow.operator_id == inc.operator_id)).all():
            desk._human_step(session, row, "restore_notice_rejected",
                             f"Not told service is back in {inc.incident_number}: {rejected_by} rejected the message.",
                             {"card_id": task.id, "reason": reason, "rejected_by": rejected_by}, at, agent=AGENT)
    session.flush()
    return suppressed


# ------------------------------------------------------------------------------ tracking


def find_tracked(session: Session, operator_id: str, ref: str, msisdn: str) -> SupportComplaintRow | None:
    """The complaint whose reference AND number both match, for this operator; None otherwise."""
    return session.scalar(select(SupportComplaintRow).where(
        SupportComplaintRow.operator_id == operator_id,
        SupportComplaintRow.ref == ref,
        SupportComplaintRow.msisdn == msisdn,
    ))


def _incident(session: Session, row: SupportComplaintRow, incident_id: str | None) -> IncidentRow | None:
    if not incident_id:
        return None
    return session.scalar(select(IncidentRow).where(IncidentRow.id == incident_id, IncidentRow.operator_id == row.operator_id))


def _known_outage(session: Session, row: SupportComplaintRow) -> IncidentRow | None:
    """The open incident the complaint is linked to now, when that is not the one we said was fixed
    (a confirmed surge relinked it): the outage is already known, so "still down" adds nothing."""
    if not row.linked_incident_id or row.linked_incident_id == row.told_incident_id:
        return None
    inc = _incident(session, row, row.linked_incident_id)
    return inc if inc is not None and inc.status not in CLOSED_INCIDENT else None


def still_down_refusal(session: Session, row: SupportComplaintRow, policy: SupportPolicy, now: datetime) -> str | None:
    """Why a still-down report is not allowed now, in a plain sentence; None when it is."""
    rule = policy.track
    known = _known_outage(session, row)
    if known is not None:
        return (f"We already know about the outage (ticket {known.incident_number}); "
                "we will tell you by SMS when service is back.")
    if row.told_restored_at is None:
        return ("We have not told you that service is back yet, so there is nothing to report; "
                "we will send you an SMS when it is.")
    if now - row.told_restored_at > timedelta(hours=rule.still_down_within_hours):
        return (f"It is more than {rule.still_down_within_hours} hours since we told you service was back; "
                "please send us a new complaint instead.")
    if row.still_down_at is not None and now - row.still_down_at < timedelta(hours=rule.still_down_cooldown_hours):
        return "You already told us service is still down; a member of our team is checking it."
    return None


def _step_incident_number(step: SupportStepRow) -> str | None:
    try:
        detail = json.loads(step.detail_json or "{}")
    except ValueError:
        return None
    return detail.get("incident_number") or (detail.get("result") or {}).get("incident_number")


def _timeline(session: Session, row: SupportComplaintRow, place: str | None, settings: AppSettings) -> list[dict[str, Any]]:
    """Oldest first, from public facts only: received, sorted, linked, with a person, restored,
    told, still down, confirmed outage, replied. Never a staff name, a policy line or a reason code."""
    items: list[tuple[datetime, str]] = [(row.created_at, "We received your complaint.")]
    where = f" in {place}" if place else ""
    for step in session.scalars(select(SupportStepRow).where(SupportStepRow.complaint_id == row.id).order_by(SupportStepRow.seq)).all():
        number = _step_incident_number(step)
        if step.agent == "triage" and step.action == "classified":
            items.append((step.at, f"Sorted as {row.category.replace('_', ' ')}."))
        elif step.agent == "action" and step.action == "called_tool" and '"link_incident"' in (step.detail_json or "") \
                and number:
            items.append((step.at, f"Linked to the known outage{where} (ticket {number})."))
        elif step.agent == "human" and step.action == "linked_incident" and number:
            items.append((step.at, f"Linked to the known outage{where} (ticket {number})."))
        elif step.agent == "escalation" and step.action == "escalated":
            items.append((step.at, "Passed to a member of our team."))
        elif step.agent == "human" and step.action == "resolved":
            items.append((step.at, "A member of our team replied."))
        elif step.agent == AGENT and step.action == "told_restored":
            inc = _incident(session, row, json.loads(step.detail_json or "{}").get("incident_id"))
            if inc is not None and (inc.restored_at or inc.closed_at):
                items.append((inc.restored_at or inc.closed_at, "Service was restored."))
            items.append((step.at, "We told you by SMS that service is back, and closed your complaint."))
        elif step.agent == AGENT and step.action == "still_down_reported":
            items.append((step.at, "You told us service is still down; a member of our team will check it."))
        elif step.agent == AGENT and step.action == "linked_confirmed_outage" and number:
            items.append((step.at, f"We confirmed an outage{where} (ticket {number})."))
    if row.status == "closed" and row.closure_reason != CLOSURE_SERVICE_RESTORED:
        items.append((row.updated_at, "Your complaint was closed."))
    items.sort(key=lambda item: item[0])
    return [{"at": iso_z(at), "text": text} for at, text in items]


def _messages(session: Session, row: SupportComplaintRow) -> list[dict[str, Any]]:
    rows = session.scalars(select(SupportMessageRow).where(SupportMessageRow.complaint_id == row.id)
                           .order_by(SupportMessageRow.at, SupportMessageRow.id)).all()
    return [{"at": iso_z(m.at), "from": "you" if m.author == "customer" else "us", "body": m.body} for m in rows]


def tracked(session: Session, row: SupportComplaintRow, *, settings: AppSettings | None = None,
            policy: SupportPolicy | None = None, now: datetime | None = None) -> dict[str, Any]:
    """The contract's ``Tracked``: where the complaint is, in the customer's words."""
    settings = settings or get_settings()
    policy = policy or load_policy()
    now = now or utcnow()
    linked = _incident(session, row, row.linked_incident_id)
    told_about = _incident(session, row, row.told_incident_id) if row.told_restored_at else None
    inc = linked or told_about
    named = complaint_place(row)
    place = place_label(named) if named else (incident_area(inc, settings) if inc is not None else None)
    told_here = inc is not None and row.told_restored_at is not None and row.told_incident_id == inc.id
    outage = None
    if inc is not None:
        outage = {"place": place, "ticket": inc.incident_number, "state": "restored" if told_here else "working",
                  "restored_at": iso_z(inc.restored_at or inc.closed_at) if told_here else None}
    can_report = still_down_refusal(session, row, policy, now) is None
    with_person = row.status in HUMAN_QUEUE
    if with_person:
        stage = "with_a_person"
        if row.escalation_reason_code == STILL_DOWN_REASON_CODE:
            headline = "A member of our team is checking why service is still down"
        else:
            headline = "A member of our team is looking at your complaint"
        told = customer_facing(Escalation(row.escalation_reason_code or "", row.escalation_reason or "", "", ""), policy) \
            if row.escalation_reason_code else None
        because = f" because {told.reason}" if told is not None and told.reason else ""
        detail = f"It is with a person{because}. We will get back to you by {desk._due_text(row.sla_due_at, settings.operator.timezone)}."
    elif told_here and row.closure_reason == CLOSURE_SERVICE_RESTORED:
        stage = "restored"
        headline = f"Service is back in {place}"
        detail = "Still down? Tell us below." if can_report else "Your complaint is closed."
    elif inc is not None and not told_here:
        stage = "outage_known"
        headline = f"Engineers are working on the outage in {place}"
        detail = "We will tell you by SMS when service is back."
    elif row.status == "closed":
        stage, headline, detail = "closed", "Your complaint is closed", None
    elif row.status in ("resolved", "action_taken"):
        stage, headline, detail = "fixed", "We have fixed the problem", "Our reply below says what we did."
    elif row.status == "answered":
        stage, headline, detail = "answered", "We have answered your complaint", "Our reply is below."
    else:
        stage, headline, detail = "received", "We have received your complaint", "We will reply shortly."
    return {
        "ref": row.ref,
        "stage": stage,
        "headline": headline,
        "detail": detail,
        "received_at": iso_z(row.created_at),
        "reply_due_at": iso_z(row.sla_due_at) if with_person else None,
        "outage": outage,
        "timeline": _timeline(session, row, place, settings),
        "messages": _messages(session, row),
        "can_report_still_down": can_report,
    }


def report_still_down(session: Session, row: SupportComplaintRow, *, note: str | None = None,
                      ctx: Any | None = None, settings: AppSettings | None = None,
                      now: datetime | None = None) -> SupportComplaintRow:
    """The customer says service is still down after we told them it was back.

    Under the desk's write lock (so two taps make one report): reopen the complaint to a person
    (``still_down_after_restore``), record their words and our holding reply, note it on the
    incident for the NOC, publish ``support.still_down``, and count it towards a surge for the
    place (``origin="still_down"``). Commits. Raises :class:`TrackConflict` with the sentence when
    the rules do not allow a report now.
    """
    from noc_agents.support import surge  # surge reuses this module's helpers; import at call time
    from noc_agents.support.context import default_context

    ctx = ctx or default_context()
    settings = settings or get_settings()
    policy = ctx.policy
    now = now or utcnow()
    text = clean(note)[:MAX_NOTE_CHARS] if note else ""
    try:
        desk._locked(session, row)
        refusal = still_down_refusal(session, row, policy, now)
        if refusal is not None:
            raise TrackConflict(refusal)
        inc = _incident(session, row, row.told_incident_id)
        named = complaint_place(row)
        place = place_label(named) if named else (incident_area(inc, settings) if inc is not None else "your area")
        place_key = named or normalise(place)  # what a surge for this report is keyed on
        reason = policy.track.still_down_reason
        row.status, row.outcome = "escalated", OUTCOME_FOR_STATUS["escalated"]
        row.escalation_reason_code, row.escalation_reason, row.escalated_at = STILL_DOWN_REASON_CODE, reason, now
        row.claimed_by, row.claimed_at = None, None
        row.closure_reason = None
        row.still_down_at = now
        row.updated_at = now
        row.sla_due_at = now + timedelta(hours=policy.sla_hours.get(row.urgency, 24))
        session.add(SupportMessageRow(complaint_id=row.id, author="customer", name=row.customer_name,
                                      body=text or f"Service is still down in {place}.", at=now, channel="web"))
        row.reply = holding_reply(Escalation(STILL_DOWN_REASON_CODE, reason, "", ""), name=desk._greeting(row.customer_name),
                                  ref=row.ref, due=desk._due_text(row.sla_due_at, settings.operator.timezone))
        session.add(SupportMessageRow(complaint_id=row.id, author="agent", name=desk.AGENT_NAME, body=row.reply, at=now,
                                      channel="web"))
        number = inc.incident_number if inc is not None else None
        desk._human_step(session, row, "still_down_reported",
                         f"The customer reports service is still down in {place}"
                         + (f" after {number} was restored" if number else "") + "; back to a person.",
                         {"incident_id": inc.id if inc else None, "incident_number": number, "note": text or None,
                          "place": place_key}, now,
                         agent=AGENT)
        if inc is not None:
            told, down = _told_and_down(session, inc)
            session.add(WorkNoteRow(
                incident_id=inc.id, author=desk.AGENT_NAME, author_role="AGENT", source="support",
                body=f"Customer {row.ref} reports service is still down in {place} after the restore ({down} of {told} told)",
            ))
        buffer_event(session, RealtimeEvent(type=EVENT_STILL_DOWN, operator_id=row.operator_id,
                                            incident_id=inc.id if inc else None,
                                            payload={"ref": row.ref, "incident_number": number, "place": place}))
        desk._emit(session, desk.EVENT_ESCALATED, row, reason_code=STILL_DOWN_REASON_CODE)
        surge.observe_still_down(session, row, parent=inc, ctx=ctx, settings=settings, now=now)
        session.commit()
    except Exception:
        session.rollback()
        raise
    return row


def _told_and_down(session: Session, inc: IncidentRow) -> tuple[int, int]:
    """(numbers told about ``inc``, of those the numbers that reported still down since)."""
    rows = session.execute(select(SupportComplaintRow.msisdn, SupportComplaintRow.still_down_at).where(
        SupportComplaintRow.operator_id == inc.operator_id, SupportComplaintRow.told_incident_id == inc.id,
        SupportComplaintRow.told_restored_at.is_not(None))).all()
    told = {msisdn for msisdn, _ in rows}
    down = {msisdn for msisdn, at in rows if at is not None}
    return len(told), len(down)


# ------------------------------------------------------------------------------ the floor


def _about(session: Session, operator_id: str, incident_ids: list[str] | None = None) -> list[SupportComplaintRow]:
    """Complaints about an incident: linked to it now, or told about it before a relink."""
    stmt = select(SupportComplaintRow).where(
        SupportComplaintRow.operator_id == operator_id,
        or_(SupportComplaintRow.linked_incident_id.is_not(None), SupportComplaintRow.told_incident_id.is_not(None)),
    )
    if incident_ids is not None:
        stmt = stmt.where(or_(SupportComplaintRow.linked_incident_id.in_(incident_ids),
                              SupportComplaintRow.told_incident_id.in_(incident_ids)))
    return list(session.scalars(stmt.order_by(SupportComplaintRow.created_at.desc(), SupportComplaintRow.ref.desc())).all())


def _incidents_of(row: SupportComplaintRow) -> set[str]:
    return {i for i in (row.linked_incident_id, row.told_incident_id) if i}


def _notice_out(session: Session, inc: IncidentRow) -> dict[str, Any]:
    notice = session.scalar(select(SupportNoticeRow).where(
        SupportNoticeRow.operator_id == inc.operator_id, SupportNoticeRow.incident_id == inc.id,
    ).order_by(SupportNoticeRow.created_at.desc(), SupportNoticeRow.id.desc()).limit(1))
    if notice is not None:
        return {"state": notice.state, "card_id": notice.card_id, "recipients": notice.recipients,
                "sent_at": iso_z(notice.sent_at)}
    waiting = inc.status not in CLOSED_INCIDENT or (
        inc.status == IncidentStatus.RESTORED.value and inc.restored_source not in TELL_SOURCES)
    return {"state": "waiting_for_restore" if waiting else "none", "card_id": None, "recipients": 0, "sent_at": None}


def _counts(rows: list[SupportComplaintRow], inc: IncidentRow) -> dict[str, int]:
    """customers / told / waiting / still_down are distinct NUMBERS; repeat contacts are complaints
    beyond each number's first about this incident."""
    numbers = {r.msisdn for r in rows}
    told = {r.msisdn for r in rows if r.told_incident_id == inc.id and r.told_restored_at is not None}
    down = {r.msisdn for r in rows if r.told_incident_id == inc.id and r.still_down_at is not None}
    return {"customers": len(numbers), "told": len(told), "waiting": len(numbers - told), "still_down": len(down),
            "repeat_contacts": len(rows) - len(numbers)}


def incident_customers(session: Session, inc: IncidentRow) -> dict[str, Any]:
    """The incident page's panel: who complained about this incident and whether they were told."""
    rows = [r for r in _about(session, inc.operator_id, [inc.id]) if inc.id in _incidents_of(r)]
    counts = _counts(rows, inc)
    return {
        "customers": counts["customers"],
        "told": counts["told"],
        "waiting": counts["waiting"],
        "still_down": counts["still_down"],
        "notice": _notice_out(session, inc),
        "complaints": [{"id": r.id, "ref": r.ref, "msisdn_masked": r.msisdn_masked, "status": r.status,
                        "told_restored_at": iso_z(r.told_restored_at) if r.told_incident_id == inc.id else None,
                        "still_down_at": iso_z(r.still_down_at) if r.told_incident_id == inc.id else None}
                       for r in rows],
    }


def outages(session: Session, operator_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """One row per incident with complaints about it, newest incident first."""
    rows = _about(session, operator_id)
    by_incident: dict[str, list[SupportComplaintRow]] = {}
    for r in rows:
        for incident_id in _incidents_of(r):
            by_incident.setdefault(incident_id, []).append(r)
    if not by_incident:
        return []
    incidents = session.scalars(select(IncidentRow).where(
        IncidentRow.id.in_(list(by_incident)), IncidentRow.operator_id == operator_id,
    ).order_by(IncidentRow.created_at.desc(), IncidentRow.incident_number.desc()).limit(limit)).all()
    spotted = set(session.scalars(select(SupportSurgeRow.incident_id).where(
        SupportSurgeRow.operator_id == operator_id, SupportSurgeRow.incident_id.is_not(None))).all())
    out = []
    for inc in incidents:
        complaints = by_incident[inc.id]
        places: list[str] = []
        for r in complaints:
            name = place_label(complaint_place(r))
            if name and name not in places:
                places.append(name)
        out.append({
            "incident_id": inc.id, "incident_number": inc.incident_number, "title": inc.title,
            "priority": inc.priority, "status": inc.status, "places": places,
            "restored_at": iso_z(inc.restored_at), "restore_source": inc.restored_source,
            **_counts(complaints, inc),
            "notice": _notice_out(session, inc),
            "from_customer_reports": inc.id in spotted,
        })
    return out


def loop_metrics(session: Session, operator_id: str, *, hours: int = 0, now: datetime | None = None) -> dict[str, Any]:
    """The contract's ``Loop`` over the last ``hours`` (0 = all time).

    Windowed by when the thing happened: ``told`` (and its timings) by the SMS, ``still_down_reports``
    by the report, the repeat and outage counts by the complaint, ``spotted_by_customers`` and the
    settled surges by the decision. ``waiting_to_hear``, ``notices_waiting``, ``recipients_waiting``
    and open surges are the state NOW, whatever the window. People are counted as distinct numbers
    per incident: one SMS goes to a number however often it complained.
    """
    now = now or utcnow()
    since = now - timedelta(hours=hours) if hours > 0 else None

    def in_window(at: datetime | None) -> bool:
        return at is not None and (since is None or at >= since)

    rows = _about(session, operator_id)
    incident_ids = {i for r in rows for i in _incidents_of(r)}
    incidents = {inc.id: inc for inc in session.scalars(select(IncidentRow).where(
        IncidentRow.id.in_(list(incident_ids)), IncidentRow.operator_id == operator_id)).all()} if incident_ids else {}

    waiting_pairs = {(r.linked_incident_id, r.msisdn) for r in rows
                     if r.linked_incident_id in incidents and incidents[r.linked_incident_id].status not in CLOSED_INCIDENT
                     and r.told_incident_id != r.linked_incident_id}
    told_at: dict[tuple[str, str], datetime] = {}
    for r in rows:
        if r.told_incident_id and r.told_restored_at and in_window(r.told_restored_at):
            key = (r.told_incident_id, r.msisdn)
            told_at[key] = min(told_at.get(key, r.told_restored_at), r.told_restored_at)
    minutes = []
    for (incident_id, _msisdn), at in told_at.items():
        inc = incidents.get(incident_id)
        restored = inc.restored_at or inc.closed_at if inc is not None else None
        if restored is not None:
            minutes.append(max(0.0, (at - restored).total_seconds() / 60))

    about_window = [r for r in rows if r.linked_incident_id and in_window(r.created_at)]
    pairs: dict[tuple[str, str], int] = {}
    for r in about_window:
        pairs[(r.linked_incident_id, r.msisdn)] = pairs.get((r.linked_incident_id, r.msisdn), 0) + 1
    repeat = sum(n - 1 for n in pairs.values())
    outages_with = len({incident_id for incident_id, _ in pairs})

    still_down = session.scalar(
        select(func.count()).select_from(SupportStepRow).join(SupportComplaintRow, SupportComplaintRow.id == SupportStepRow.complaint_id)
        .where(SupportComplaintRow.operator_id == operator_id, SupportStepRow.agent == AGENT,
               SupportStepRow.action == "still_down_reported",
               *([SupportStepRow.at >= since] if since is not None else []))
    ) or 0

    cards = session.scalars(select(HitlTaskRow).where(
        HitlTaskRow.operator_id == operator_id, HitlTaskRow.task_type == CUSTOMER_UPDATE_TASK_TYPE,
        HitlTaskRow.status.in_(("PENDING", "CLAIMED")))).all()
    card_ids = [c.id for c in cards]
    recipients_waiting = sum(session.scalars(select(SupportNoticeRow.recipients).where(
        SupportNoticeRow.card_id.in_(card_ids), SupportNoticeRow.operator_id == operator_id)).all()) if card_ids else 0

    surges = session.scalars(select(SupportSurgeRow).where(SupportSurgeRow.operator_id == operator_id)).all()
    open_surges = sum(1 for s in surges if s.status == "open")
    confirmed = [s for s in surges if s.status == "confirmed" and in_window(s.decided_at)]
    dismissed = sum(1 for s in surges if s.status == "dismissed" and in_window(s.decided_at))
    return {
        "waiting_to_hear": len(waiting_pairs),
        "told": len(told_at),
        "told_median_minutes": round(statistics.median(minutes), 1) if minutes else None,
        "told_p90_minutes": round(_p90(minutes), 1) if minutes else None,
        "notices_waiting": len(cards),
        "recipients_waiting": int(recipients_waiting),
        "still_down_reports": int(still_down),
        "repeat_contacts": repeat,
        "outages_with_complaints": outages_with,
        "repeat_contacts_per_outage": round(repeat / outages_with, 2) if outages_with else None,
        "spotted_by_customers": len({s.incident_id for s in confirmed if s.incident_id}),
        "surges": {"open": open_surges, "confirmed": len(confirmed), "dismissed": dismissed},
    }
