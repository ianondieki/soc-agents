"""Close the loop (docs/CLOSE_THE_LOOP.md): customers hear back, can check on a complaint, and say
when service is still down -- and the floor can see how well the promise is kept.

The desk links an outage complaint to the NOC ticket and promises "we will tell you when service
is restored". This module keeps that promise and measures it:

* :func:`on_incident_restored` -- called by the NOC's restore, mark-restored note and close routes
  (``main.py``), inside their transaction and in a SAVEPOINT of the route's, so a failure here is
  rolled back on its own and never takes the restore or the close with it; and by
  :func:`raise_customer_update`, a person raising the update again (7.1). It finds every number that
  complained about the incident and has not been told, and either sends one SMS per number now or
  raises one ``APPROVE_CUSTOMER_UPDATE`` card with the SMS ``HELD`` behind it, on the floor's
  autonomy ladder (``policy.yaml`` -> ``customer_updates``).
* :func:`approve_customer_update` / :func:`reject_customer_update` -- the card's two outcomes, run
  inside the HITL route's own transaction. A reject is "Not now" (7.1): the notice is ``held_back``,
  its SMS suppressed, and the customers stay waiting to hear.
* :func:`late_link` -- a new top-level incident adopts recent unlinked complaints it covers (7.3).
* :func:`find_tracked`, :func:`tracked`, :func:`report_still_down` -- the public Track page.
* :func:`loop_metrics`, :func:`outages`, :func:`incident_customers` -- the staff read side.

**Told once.** One SMS per phone number per incident, ever: a number is skipped when the tell
history (the ``followup/told_restored`` steps, which a relink never erases) says it was told about
this incident, and while an SMS for it waits behind a pending card. Outbox keys carry the notice
attempt (``support-restore:{notice_id}:{msisdn_hash}``) so a held-back update can be raised again,
and every number hash is an HMAC keyed by ``SUPPORT_HASH_KEY``.

**Where the SMS rows point.** They carry the card's id (``hitl_task_id``) and NOT the incident's:
``outbox.release_held`` and ``services/hitl.suppress_held_outbox`` act on every HELD row of an
incident when its broadcast card is decided, and a customer notice must be released or suppressed
by its own card only (the handover precedent). The payload names the complaint by id and reference;
the number itself never enters the outbox.

**One accepted risk, bounded.** The public form cannot prove the caller owns the number typed, so
at most ``customer_updates.max_sms_per_number_per_day`` loop SMS go to one number in 24 hours, and
before a real SMS adapter is switched on, numbers must be verified (startup warns until then).

**Customer words only** on the Track page: it is built from the facts the customer already holds,
and every message passes the echo-only rule (``text.redact_untyped``): a transaction code or an
amount the caller did not type is never shown, and a staff member's reply is never shown verbatim.
"""

from __future__ import annotations

import hashlib
import hmac
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
from noc_agents.db.models import HitlTaskRow, IncidentRow, OutboxRow, WorkNoteRow, new_id, utcnow
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
from noc_agents.support.escalation import Escalation, customer_facing
from noc_agents.support.policy import SupportPolicy, load_policy
from noc_agents.support.text import clean, mask_msisdn_staff, normalise, redact_untyped
from noc_agents.support.tools import STRONG_LINKS, best_link
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
TERMINAL_INCIDENT = frozenset({IncidentStatus.CLOSED.value, IncidentStatus.CANCELLED.value})
#: The ways an update is raised: a confirmed restore, the close, or a person (7.1).
TRIGGERS = ("restore", "close", "manual")

TOLD_TEXT = {
    "en": "Service is back in {place}. Your complaint {ref} is now closed. Still down? Tell us at {track_url}",
    "sw": "Huduma imerejea {place}. Lalamiko lako {ref} limefungwa. Bado haifanyi kazi? Tuambie hapa {track_url}",
}
BASE_URL_ENV = "SUPPORT_PUBLIC_BASE_URL"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
CARD_SAMPLE_MAX = 10
RESTORE_NOTE_CARD_CHARS = 200
CLOSURE_SERVICE_RESTORED = "service_restored"
STILL_DOWN_REASON_CODE = "still_down_after_restore"
#: Channels where the person who filed typed the complaint themselves (the echo-only rule). On the
#: call-centre channel a member of staff typed it, so it may hold what the caller never said.
SELF_SERVICE_CHANNELS = frozenset({"web", "app", "sms", "social"})
STAFF_REPLY_ON_TRACK = "A member of our team replied to your complaint. If you did not receive the reply, contact us with your reference."

HASH_KEY_ENV = "SUPPORT_HASH_KEY"
#: Development only: a fixed key so tests and the demo are repeatable. Startup warns while in use.
DEV_HASH_KEY = "noc-agents-support-dev-only-hash-key"
#: The public form cannot prove the caller owns the number typed (no OTP). Flip only when it can.
NUMBER_VERIFICATION_EXISTS = False

EVENT_CUSTOMERS_TOLD = "support.customers_told"
EVENT_STILL_DOWN = "support.still_down"

NOT_FOUND = "We could not find a complaint with that reference and number."
MAX_NOTE_CHARS = 1000
_SENT_LIKE = (outbox.PENDING, outbox.CLAIMED, outbox.SENT, outbox.DELIVERED)


class TrackConflict(Exception):
    """A still-down report that the rules do not allow now (the API answers 409 with the sentence)."""


class UpdateConflict(Exception):
    """A customer update that cannot be raised now (the API answers 409 with the sentence)."""


# ------------------------------------------------------------------------------- helpers


def public_base_url() -> str:
    """``SUPPORT_PUBLIC_BASE_URL`` (default ``http://127.0.0.1:8000``), read at call time."""
    return (os.getenv(BASE_URL_ENV) or DEFAULT_BASE_URL).strip().rstrip("/") or DEFAULT_BASE_URL


def track_url(ref: str) -> str:
    return f"{public_base_url()}/track?ref={ref}"


def _hash_key() -> bytes:
    return (os.getenv(HASH_KEY_ENV) or DEV_HASH_KEY).encode("utf-8")


def msisdn_hash(msisdn: str) -> str:
    """HMAC-SHA256 of the number, keyed by ``SUPPORT_HASH_KEY``: a stable token for outbox keys and
    payloads that a reader of the outbox cannot reverse by hashing every Kenyan number."""
    return hmac.new(_hash_key(), msisdn.encode("utf-8"), hashlib.sha256).hexdigest()[:24]


def restore_key(notice_id: str, msisdn: str) -> str:
    """One key per number per notice ATTEMPT (7.1): a held-back update can be raised again."""
    return f"support-restore:{notice_id}:{msisdn_hash(msisdn)}"


def startup_warnings() -> list[str]:
    """What a deployment must hear at startup (main's lifespan logs each as a warning)."""
    out = []
    if not (os.getenv(HASH_KEY_ENV) or "").strip():
        out.append(f"{HASH_KEY_ENV} is not set: the support desk hashes customer numbers with the built-in "
                   "development key. Set a secret value before real customer numbers are stored.")
    sms_on = (os.getenv("SMS_ENABLED") or "").strip().lower() in ("1", "true", "yes", "on")
    provider = (os.getenv("SMS_PROVIDER") or "africastalking").strip().lower()
    if sms_on and provider != "mock" and not NUMBER_VERIFICATION_EXISTS:
        out.append(f"SMS_ENABLED=true with provider {provider!r}, but numbers typed on the public complaint form "
                   "are not verified (no OTP): the close-the-loop SMS could reach a number the complainant does "
                   "not own. Verify numbers before switching a real SMS adapter on (docs/CLOSE_THE_LOOP.md).")
    return out


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


def complaint_regions(row: SupportComplaintRow, ctx: Any) -> tuple[str, ...]:
    place = complaint_place(row)
    regions = ctx.gazetteer.regions_of(place) if place else ()
    if regions:
        return tuple(regions)
    try:
        places = json.loads(row.triage_json or "{}").get("places") or []
    except ValueError:
        return ()
    return tuple(places[0].get("regions") or ()) if places and isinstance(places[0], dict) else ()


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


def _detail(step: SupportStepRow) -> dict[str, Any]:
    try:
        return json.loads(step.detail_json or "{}")
    except ValueError:
        return {}


# ---------------------------------------------------------------------------- history


@dataclass(frozen=True)
class _Event:
    """One tell or one still-down report, from the steps (which nothing ever rewrites)."""

    incident_id: str | None
    complaint_id: str
    msisdn: str
    at: datetime


def _events(session: Session, operator_id: str, action: str) -> list[_Event]:
    """Every ``followup/{action}`` step of the operator's complaints: the tell history
    (``told_restored``) and the still-down history (``still_down_reported``)."""
    rows = session.execute(
        select(SupportStepRow, SupportComplaintRow.msisdn)
        .join(SupportComplaintRow, SupportComplaintRow.id == SupportStepRow.complaint_id)
        .where(SupportComplaintRow.operator_id == operator_id, SupportStepRow.agent == AGENT,
               SupportStepRow.action == action)
    ).all()
    return [_Event(_detail(step).get("incident_id"), step.complaint_id, msisdn, step.at) for step, msisdn in rows]


def _told_numbers(session: Session, inc: IncidentRow) -> set[str]:
    """Numbers told about ``inc``: the tell history, plus what the complaints say now."""
    told = {e.msisdn for e in _events(session, inc.operator_id, "told_restored") if e.incident_id == inc.id}
    told |= set(session.scalars(select(SupportComplaintRow.msisdn).where(
        SupportComplaintRow.operator_id == inc.operator_id, SupportComplaintRow.told_incident_id == inc.id,
        SupportComplaintRow.told_restored_at.is_not(None))).all())
    return told


def sms_sent_today(session: Session, operator_id: str, msisdn: str, now: datetime) -> int:
    """Close-the-loop SMS to this number on their way or sent in the last 24 hours."""
    pattern = f'%"msisdn_hash": "{msisdn_hash(msisdn)}"%'
    return int(session.scalar(
        select(func.count()).select_from(OutboxRow).where(
            OutboxRow.operator_id == operator_id, OutboxRow.kind == outbox.SMS,
            OutboxRow.idempotency_key.like("support-%"), OutboxRow.payload_json.like(pattern),
            OutboxRow.status.in_(_SENT_LIKE), OutboxRow.updated_at >= now - timedelta(hours=24))
    ) or 0)


def sms_capped(session: Session, operator_id: str, msisdn: str, policy: SupportPolicy, now: datetime) -> bool:
    return sms_sent_today(session, operator_id, msisdn, now) >= policy.customer_updates.max_sms_per_number_per_day


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


def _numbers_behind(session: Session, inc: IncidentRow, states: tuple[str, ...]) -> set[str]:
    """Numbers whose SMS for this incident sit behind a notice in one of ``states``."""
    cards = [c for c in session.scalars(select(SupportNoticeRow.card_id).where(
        SupportNoticeRow.operator_id == inc.operator_id, SupportNoticeRow.incident_id == inc.id,
        SupportNoticeRow.state.in_(states), SupportNoticeRow.card_id.is_not(None))).all() if c]
    if not cards:
        return set()
    ids: list[str] = []
    for payload in session.scalars(select(OutboxRow.payload_json).where(OutboxRow.hitl_task_id.in_(cards))).all():
        ids.extend(json.loads(payload or "{}").get("complaint_ids") or [])
    return set(session.scalars(select(SupportComplaintRow.msisdn).where(
        SupportComplaintRow.id.in_(ids), SupportComplaintRow.operator_id == inc.operator_id)).all())


def _recipients(session: Session, inc: IncidentRow, settings: AppSettings, policy: SupportPolicy, *,
                include_held_back: bool, now: datetime) -> tuple[list[_Recipient], list[_Recipient]]:
    """``(to tell, over the daily cap)``: every number with an untold complaint about ``inc`` that was
    never told about it, has no SMS waiting behind a pending card, and -- on a re-restore -- was not
    held back (a held-back update is raised again by the close or by a person, 7.1)."""
    by_number: OrderedDict[str, list[SupportComplaintRow]] = OrderedDict()
    for row in _untold(session, inc):
        by_number.setdefault(row.msisdn, []).append(row)
    if not by_number:
        return [], []
    skip = _told_numbers(session, inc) | _numbers_behind(session, inc, ("awaiting_approval",))
    if not include_held_back:
        skip |= _numbers_behind(session, inc, ("held_back",))
    out: list[_Recipient] = []
    capped: list[_Recipient] = []
    for msisdn, rows in by_number.items():
        if msisdn in skip:
            continue
        rec = _Recipient(msisdn=msisdn, complaints=rows)
        rec.language = sms_language(rec.lead.language)
        rec.place = place_label(complaint_place(rec.lead)) or incident_area(inc, settings)
        rec.text = told_text(rec.language, place=rec.place, ref=rec.lead.ref)
        (capped if sms_capped(session, inc.operator_id, msisdn, policy, now) else out).append(rec)
    return out, capped


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
        "msisdn_masked": mask_msisdn_staff(rec.lead.msisdn),
        "msisdn_hash": msisdn_hash(rec.msisdn),
        "incident_id": inc.id,
        "incident_number": inc.incident_number,
        "language": rec.language,
        "body": rec.text,
        "segments": segments_for(rec.text),
        "notice_id": notice_id,
    }


def _restore_note(session: Session, inc: IncidentRow, note: str | None) -> str | None:
    """The restorer's own words: the note given, else the latest restore note, else the summary."""
    if note and note.strip():
        return note.strip()[:500]
    latest = session.scalar(select(WorkNoteRow.body).where(
        WorkNoteRow.incident_id == inc.id, WorkNoteRow.source == "restore").order_by(WorkNoteRow.created_at.desc()).limit(1))
    return (latest or inc.resolution_summary or None) and str(latest or inc.resolution_summary)[:500]


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


def _capped_step(session: Session, rows: list[SupportComplaintRow], inc: IncidentRow, now: datetime, cap: int) -> None:
    for row in rows:
        desk._human_step(session, row, "sms_capped",
                         f"Not told about {inc.incident_number} yet: this number already had {cap} of our messages "
                         "in the last 24 hours; the customer stays waiting.",
                         {"incident_id": inc.id, "incident_number": inc.incident_number, "cap": cap}, now, agent=AGENT)


def _customers_told_event(session: Session, inc: IncidentRow, count: int) -> None:
    buffer_event(session, RealtimeEvent(type=EVENT_CUSTOMERS_TOLD, operator_id=inc.operator_id, incident_id=inc.id,
                                        payload={"incident_number": inc.incident_number, "count": count}))


def on_incident_restored(
    session: Session,
    inc: IncidentRow,
    *,
    trigger: str,
    actor: str | None = None,
    note: str | None = None,
    now: datetime | None = None,
    settings: AppSettings | None = None,
    policy: SupportPolicy | None = None,
) -> SupportNoticeRow | None:
    """Tell the incident's customers service is back, or raise the card that will.

    ``trigger``: ``"restore"`` (the incident is RESTORED; only a confirmed source tells anyone, and a
    number whose update was held back is not raised again by a re-restore), ``"close"`` (the
    incident is CLOSED: tells whoever is untold, held-back numbers included), or ``"manual"`` (a
    person raises it again, RESTORED or CLOSED). Runs inside the caller's transaction and transmits
    nothing: the caller drains the outbox after its commit when the notice was ``sent``. Returns
    the notice, or None when nobody is left to tell.
    """
    if trigger not in TRIGGERS:
        raise ValueError(f"trigger must be one of {TRIGGERS}, not {trigger!r}")
    if trigger == "restore" and (inc.status != IncidentStatus.RESTORED.value or inc.restored_source not in TELL_SOURCES):
        return None  # a guessed restore waits for a confirmed one, or for the close
    if trigger == "close" and inc.status != IncidentStatus.CLOSED.value:
        return None
    if trigger == "manual" and inc.status not in (IncidentStatus.RESTORED.value, IncidentStatus.CLOSED.value):
        return None
    settings = settings or get_settings()
    policy = policy or load_policy()
    now = now or utcnow()
    recipients, capped = _recipients(session, inc, settings, policy, include_held_back=trigger != "restore", now=now)
    if capped:
        _capped_step(session, [row for rec in capped for row in rec.complaints], inc, now,
                     policy.customer_updates.max_sms_per_number_per_day)
    if not recipients:
        session.flush()
        return None
    level = settings.operator.autonomy_level
    waits = policy.customer_updates.waits(level, inc.priority, len(recipients))
    if trigger == "restore" or (trigger == "manual" and inc.status == IncidentStatus.RESTORED.value):
        source = inc.restored_source or CLOSE_SOURCE
    else:
        source = CLOSE_SOURCE
    languages = {"en": sum(r.language == "en" for r in recipients), "sw": sum(r.language == "sw" for r in recipients)}
    notice = SupportNoticeRow(
        id=new_id(), operator_id=inc.operator_id, incident_id=inc.id, state="sent", recipients=len(recipients),
        languages_json=json.dumps(languages), text_en=_sample_text(recipients, "en"), text_sw=_sample_text(recipients, "sw"),
        restore_source=source, restore_note=_restore_note(session, inc, note), created_at=now,
    )
    session.add(notice)
    session.flush()
    for rec in recipients:
        rec.key = restore_key(notice.id, rec.msisdn)
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
            "sample": [{"ref": r.lead.ref, "msisdn_masked": mask_msisdn_staff(r.msisdn), "language": r.language}
                       for r in recipients[:CARD_SAMPLE_MAX]],
            "restore_source": source,
            # 7.2: the evidence that service is back, beside what goes out.
            "restored_at": iso_z(inc.restored_at or inc.closed_at),
            "restored_by": inc.restored_by or (actor if trigger != "restore" else None),
            "restore_note": (notice.restore_note or "")[:RESTORE_NOTE_CARD_CHARS] or None,
            "incident_status": inc.status,
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
        log.info("support: %s update for %d numbers waits on card %s (%s, %s, %s)",
                 inc.incident_number, len(recipients), card.id, trigger, level, inc.priority)
        return notice
    notice.sent_at = now
    for rec in recipients:
        outbox.enqueue(session, kind=outbox.SMS, idempotency_key=rec.key,
                       payload=_sms_payload(rec, inc, purpose="support_restore", notice_id=notice.id),
                       operator_id=inc.operator_id)
        _tell_number(session, rec, inc, now=now, timezone=settings.operator.timezone, closed=source == CLOSE_SOURCE,
                     extra={"sent_by": f"policy:{level}", "notice_id": notice.id, "trigger": trigger})
    _customers_told_event(session, inc, len(recipients))
    session.flush()
    return notice


def raise_customer_update(session: Session, inc: IncidentRow, *, actor: str, now: datetime | None = None,
                          settings: AppSettings | None = None, policy: SupportPolicy | None = None) -> SupportNoticeRow:
    """A person raises the update again (``POST /incidents/{id}/customer-update``, 7.1): allowed when the
    incident is RESTORED or CLOSED, someone is still untold and no card is pending. Same ladder:
    it sends now or raises a fresh card. Raises :class:`UpdateConflict` with the reason otherwise.
    Commits under the desk's write lock; the caller drains the outbox when it was ``sent``."""
    settings = settings or get_settings()
    policy = policy or load_policy()
    now = now or utcnow()
    try:
        desk._write_lock(session)
        session.refresh(inc)
        if inc.status not in (IncidentStatus.RESTORED.value, IncidentStatus.CLOSED.value):
            raise UpdateConflict(f"{inc.incident_number} is {inc.status}: an update can be sent once it is restored or closed")
        pending = session.scalar(select(SupportNoticeRow.id).where(
            SupportNoticeRow.operator_id == inc.operator_id, SupportNoticeRow.incident_id == inc.id,
            SupportNoticeRow.state == "awaiting_approval"))
        if pending is not None:
            raise UpdateConflict("an update for this incident is already waiting for approval")
        notice = on_incident_restored(session, inc, trigger="manual", actor=actor, now=now, settings=settings, policy=policy)
        if notice is None:
            raise UpdateConflict("everyone who complained about this incident has been told, or cannot be messaged today")
        session.commit()
        return notice
    except Exception:
        session.rollback()
        raise


def _notice_for(session: Session, task: HitlTaskRow) -> SupportNoticeRow | None:
    return session.scalar(select(SupportNoticeRow).where(
        SupportNoticeRow.card_id == task.id, SupportNoticeRow.operator_id == task.operator_id))


def approve_customer_update(session: Session, task: HitlTaskRow, *, approved_by: str, approved_at: datetime,
                            settings: AppSettings | None = None, policy: SupportPolicy | None = None) -> int:
    """The card was approved: each of ITS HELD SMS goes PENDING (approved by the decider) and its
    complaints are marked told -- unless the number was told meanwhile or reached the daily cap, when
    the row is suppressed with the reason. Addressed by the card's id only; nothing else in the
    outbox moves. Returns the numbers told; the caller drains the outbox after its commit."""
    settings = settings or get_settings()
    policy = policy or load_policy()
    inc = session.get(IncidentRow, task.incident_id) if task.incident_id else None
    if inc is None:
        return 0
    notice = _notice_for(session, task)
    closed = notice is not None and notice.restore_source == CLOSE_SOURCE
    told_numbers = _told_numbers(session, inc)
    told = 0
    for row in session.scalars(select(OutboxRow).where(OutboxRow.hitl_task_id == task.id, OutboxRow.kind == outbox.SMS,
                                                       OutboxRow.status == outbox.HELD)).all():
        payload = json.loads(row.payload_json or "{}")
        complaints = list(session.scalars(select(SupportComplaintRow).where(
            SupportComplaintRow.id.in_(payload.get("complaint_ids") or []),
            SupportComplaintRow.operator_id == inc.operator_id,
            or_(SupportComplaintRow.told_incident_id.is_(None), SupportComplaintRow.told_incident_id != inc.id),
        ).order_by(SupportComplaintRow.created_at.desc())).all())
        if not complaints or complaints[0].msisdn in told_numbers:
            row.status, row.last_error, row.updated_at = outbox.SUPPRESSED, "not sent: the customer was already told", utcnow()
            continue
        if sms_capped(session, inc.operator_id, complaints[0].msisdn, policy, approved_at):
            row.status, row.updated_at = outbox.SUPPRESSED, utcnow()
            row.last_error = "not sent: the daily message cap for this number was reached; the customer stays waiting"
            _capped_step(session, complaints, inc, approved_at, policy.customer_updates.max_sms_per_number_per_day)
            continue
        row.status, row.approved_by, row.approved_at, row.updated_at = outbox.PENDING, approved_by, approved_at, utcnow()
        rec = _Recipient(msisdn=complaints[0].msisdn, complaints=complaints, language=payload.get("language", "en"),
                         place="", text=payload.get("body", ""), key=row.idempotency_key)
        rec.place = place_label(complaint_place(complaints[0])) or incident_area(inc, settings)
        _tell_number(session, rec, inc, now=approved_at, timezone=settings.operator.timezone, closed=closed,
                     extra={"approved_by": approved_by, "card_id": task.id})
        told_numbers.add(rec.msisdn)
        told += 1
    if notice is not None:
        notice.state, notice.sent_at = "sent", approved_at
        notice.decided_by, notice.decided_at = approved_by, approved_at
    if told:
        _customers_told_event(session, inc, told)
    session.flush()
    return told


def reject_customer_update(session: Session, task: HitlTaskRow, *, rejected_by: str, reason: str, at: datetime) -> int:
    """The card was rejected: "Not now" (7.1). Its HELD SMS are SUPPRESSED, nobody is told, the
    customers stay waiting to hear, and the notice is ``held_back`` with the reason, who and when.
    The update can be raised again: by the close, or by a person. Returns the rows suppressed."""
    inc = session.get(IncidentRow, task.incident_id) if task.incident_id else None
    suppressed = session.execute(
        update(OutboxRow)
        .where(OutboxRow.hitl_task_id == task.id, OutboxRow.status == outbox.HELD)
        .values(status=outbox.SUPPRESSED, last_error=f"held back by {rejected_by}: {reason}"[:2000], updated_at=utcnow())
    ).rowcount
    notice = _notice_for(session, task)
    if notice is not None:
        notice.state, notice.decided_by, notice.decided_at, notice.reason = "held_back", rejected_by, at, reason
    if inc is not None:
        ids: list[str] = []
        for payload in session.scalars(select(OutboxRow.payload_json).where(OutboxRow.hitl_task_id == task.id)).all():
            ids.extend(json.loads(payload or "{}").get("complaint_ids") or [])
        for row in session.scalars(select(SupportComplaintRow).where(
                SupportComplaintRow.id.in_(ids), SupportComplaintRow.operator_id == inc.operator_id)).all():
            desk._human_step(session, row, "restore_notice_held_back",
                             f"Not told yet that service is back ({inc.incident_number}): {rejected_by} held the update back.",
                             {"card_id": task.id, "reason": reason, "held_by": rejected_by,
                              "incident_id": inc.id, "incident_number": inc.incident_number}, at, agent=AGENT)
    session.flush()
    return suppressed


# --------------------------------------------------------------------------- late linking


def late_link(session: Session, inc: IncidentRow, *, ctx: Any | None = None, now: datetime | None = None) -> int:
    """A new top-level incident adopts the recent unlinked network complaints it covers (7.3): the
    last ``linking.late_link_hours``, this operator, a place for which this incident is now the best
    STRONG match (``tools.best_link``, the same matching as ``link_incident``). Each gets a
    ``followup/linked_late`` step; no SMS (the restore notice reaches them later). Commits under the
    desk's write lock; returns how many were linked."""
    from noc_agents.support.context import default_context

    if inc.parent_incident_id or inc.status in CLOSED_INCIDENT:
        return 0
    ctx = ctx or default_context()
    now = now or utcnow()
    since = now - timedelta(hours=ctx.policy.linking.late_link_hours)
    linked = 0
    try:
        desk._write_lock(session)
        rows = session.scalars(select(SupportComplaintRow).where(
            SupportComplaintRow.operator_id == inc.operator_id, SupportComplaintRow.category == "network",
            SupportComplaintRow.linked_incident_id.is_(None), SupportComplaintRow.place.is_not(None),
            SupportComplaintRow.created_at >= since, SupportComplaintRow.status != "closed",
        ).order_by(SupportComplaintRow.created_at)).all()
        for row in rows:
            strength, best = best_link(session, inc.operator_id, row.place or "", complaint_regions(row, ctx))
            if best is None or best.id != inc.id or strength not in STRONG_LINKS:
                continue
            row.linked_incident_id, row.link_strength, row.updated_at = inc.id, strength, now
            desk._human_step(session, row, "linked_late",
                             f"Linked to {inc.incident_number} ({inc.site_name}) when it opened: the place named matches its "
                             f"{strength.replace('_', ' ')}.",
                             {"incident_id": inc.id, "incident_number": inc.incident_number, "link_strength": strength}, now,
                             agent=AGENT)
            desk._emit(session, desk.EVENT_UPDATED, row, linked_incident=inc.incident_number)
            linked += 1
        session.commit()
    except Exception:
        session.rollback()
        raise
    if linked:
        log.info("support: %d recent complaint(s) linked late to %s", linked, inc.incident_number)
    return linked


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


@dataclass
class _Where:
    """Where a complaint stands, for the Track page and the still-down rules."""

    inc: IncidentRow | None  # the incident shown: the linked one, else the one we told them about
    told_here: bool  # we told this customer service is back, about ``inc``
    terminal_untold: bool  # ``inc`` is closed or cancelled and this customer was never told: no SMS will come
    place: str | None
    place_key: str | None


def _where(session: Session, row: SupportComplaintRow, settings: AppSettings) -> _Where:
    linked = _incident(session, row, row.linked_incident_id)
    told_about = _incident(session, row, row.told_incident_id) if row.told_restored_at else None
    inc = linked or told_about
    named = complaint_place(row)
    place = place_label(named) if named else (incident_area(inc, settings) if inc is not None else None)
    told_here = inc is not None and row.told_restored_at is not None and row.told_incident_id == inc.id
    terminal_untold = inc is not None and inc.status in TERMINAL_INCIDENT and not told_here
    return _Where(inc, told_here, terminal_untold, place, named or (normalise(place) if place else None))


def still_down_refusal(session: Session, row: SupportComplaintRow, policy: SupportPolicy, now: datetime,
                       settings: AppSettings | None = None) -> str | None:
    """Why a still-down report is not allowed now, in a plain sentence; None when it is. A refusal
    never promises a message that will not come (MINOR 3): a complaint whose ticket closed without
    telling them may always say it is still down."""
    settings = settings or get_settings()
    rule = policy.track
    where = _where(session, row, settings)
    inc = where.inc
    if where.told_here or where.terminal_untold:
        since = row.told_restored_at if where.told_here else (inc.closed_at or inc.updated_at)
        if since is not None and now - since > timedelta(hours=rule.still_down_within_hours):
            return (f"It is more than {rule.still_down_within_hours} hours since the ticket for your area was closed; "
                    "please send us a new complaint instead.")
        if row.still_down_at is not None and now - row.still_down_at < timedelta(hours=rule.still_down_cooldown_hours):
            return "You already told us service is still down; a member of our team is checking it."
        return None
    if inc is None:
        return ("We have not linked your complaint to an outage. If we find one in your area, we will link your "
                "complaint to it and tell you when it is fixed.")
    if inc.status == IncidentStatus.RESTORED.value:
        return "We are checking that service is back in your area; we will tell you as soon as it is confirmed."
    return f"We already know about the outage (ticket {inc.incident_number}); we will tell you when service is back."


def _typed(session: Session, row: SupportComplaintRow, messages: list[SupportMessageRow]) -> str:
    """Everything the caller typed themselves: the complaint on a self-service channel, and their
    own words on the Track page. What the echo-only rule lets a reply repeat back to them."""
    parts = [row.body] if row.channel in SELF_SERVICE_CHANNELS else []
    parts += [m.body for m in messages if m.author == "customer" and m.channel == "web"]
    return " ".join(parts)


def _messages(session: Session, row: SupportComplaintRow) -> list[dict[str, Any]]:
    """The conversation as the PUBLIC may read it: a staff reply is never shown verbatim, and no
    message shows a transaction code or an amount the caller did not type (``redact_untyped``)."""
    rows = list(session.scalars(select(SupportMessageRow).where(SupportMessageRow.complaint_id == row.id)
                                .order_by(SupportMessageRow.at, SupportMessageRow.id)).all())
    typed = _typed(session, row, rows)
    out = []
    for m in rows:
        if m.author == "staff":
            body = STAFF_REPLY_ON_TRACK
        elif m.author == "customer":
            own = row.channel in SELF_SERVICE_CHANNELS or m.channel == "web"
            body = m.body if own else redact_untyped(m.body, typed, bare=True)
        else:
            body = redact_untyped(m.body, typed)
        out.append({"at": iso_z(m.at), "from": "you" if m.author == "customer" else "us", "body": body})
    return out


def _timeline(session: Session, row: SupportComplaintRow, place: str | None) -> list[dict[str, Any]]:
    """Oldest first, written for the customer (7.3), from public facts only: never a staff name, a
    policy line or a reason code."""
    items: list[tuple[datetime, str]] = [(row.created_at, "We received your complaint.")]
    where = f" in {place}" if place else ""
    for step in session.scalars(select(SupportStepRow).where(SupportStepRow.complaint_id == row.id).order_by(SupportStepRow.seq)).all():
        detail = _detail(step)
        number = detail.get("incident_number") or (detail.get("result") or {}).get("incident_number")
        if step.agent == "triage" and step.action == "classified":
            items.append((step.at, "We read your complaint and passed it to the network team." if row.category == "network"
                          else "We read your complaint."))
        elif step.agent == "action" and step.action == "called_tool" and detail.get("tool") == "link_incident" and \
                (detail.get("result") or {}).get("found") and number:
            items.append((step.at, f"Linked to the known outage{where} (ticket {number})."))
        elif step.agent in ("human", AGENT) and step.action in ("linked_incident", "linked_late") and number:
            items.append((step.at, f"Linked to the outage{where} (ticket {number})."))
        elif step.agent == "escalation" and step.action == "escalated":
            items.append((step.at, "Passed to a member of our team."))
        elif step.agent == "human" and step.action == "resolved":
            items.append((step.at, "A member of our team replied."))
        elif step.agent == AGENT and step.action == "told_restored":
            inc = _incident(session, row, detail.get("incident_id"))
            if inc is not None and (inc.restored_at or inc.closed_at):
                items.append((inc.restored_at or inc.closed_at, "Service was restored."))
            items.append((step.at, "We told you service is back and closed your complaint."))
        elif step.agent == AGENT and step.action == "still_down_reported":
            items.append((step.at, "You told us service is still down; a member of our team will check it."))
        elif step.agent == AGENT and step.action == "linked_confirmed_outage" and number:
            items.append((step.at, f"We confirmed an outage{where} (ticket {number})."))
    if row.status == "closed" and row.closure_reason != CLOSURE_SERVICE_RESTORED:
        items.append((row.updated_at, "Your complaint was closed."))
    items.sort(key=lambda item: item[0])
    return [{"at": iso_z(at), "text": text} for at, text in items]


def tracked(session: Session, row: SupportComplaintRow, *, settings: AppSettings | None = None,
            policy: SupportPolicy | None = None, now: datetime | None = None) -> dict[str, Any]:
    """The contract's ``Tracked``: where the complaint is, in the customer's words (7.3: never
    something untrue, and no mention of a message the customer never received)."""
    settings = settings or get_settings()
    policy = policy or load_policy()
    now = now or utcnow()
    where = _where(session, row, settings)
    inc, place = where.inc, where.place
    still_down_here = where.told_here and row.still_down_at is not None
    outage = None
    if inc is not None:
        state = "still_down" if still_down_here else ("restored" if where.told_here else "working")
        outage = {"place": place, "ticket": inc.incident_number, "state": state,
                  "restored_at": iso_z(inc.restored_at or inc.closed_at) if where.told_here else None}
    can_report = still_down_refusal(session, row, policy, now, settings) is None
    with_person = row.status in HUMAN_QUEUE
    due = desk._due_text(row.sla_due_at, settings.operator.timezone)
    if with_person:
        stage = "with_a_person"
        if row.escalation_reason_code == STILL_DOWN_REASON_CODE:
            headline = "A member of our team is checking why service is still down"
            detail = f"You told us service is still down. A member of our team will check and reply by {due}."
        else:
            headline = "A member of our team is looking at your complaint"
            told = customer_facing(Escalation(row.escalation_reason_code or "", row.escalation_reason or "", "", ""), policy) \
                if row.escalation_reason_code else None
            because = f" because {told.reason}" if told is not None and told.reason else ""
            detail = f"It is with a person{because}. We will get back to you by {due}."
    elif where.told_here and row.closure_reason == CLOSURE_SERVICE_RESTORED:
        stage = "restored"
        headline = f"Service is back in {place}"
        detail = "Still down? Tell us below." if can_report else "Your complaint is closed."
    elif where.terminal_untold:
        stage = "closed"
        headline = f"The outage ticket for {place} is closed"
        detail = "If service is still down for you, tell us below." if can_report else "Your complaint is closed."
    elif inc is not None and inc.status == IncidentStatus.RESTORED.value:
        stage = "outage_known"
        headline = f"We are checking that service is back in {place}"
        detail = "We will tell you as soon as it is confirmed."
    elif inc is not None:
        stage = "outage_known"
        headline = f"Engineers are working on the outage in {place}"
        detail = "We will tell you when service is back."
    elif row.status == "closed":
        stage, headline, detail = "closed", "Your complaint is closed", None
    elif row.status in ("resolved", "action_taken"):
        stage, headline, detail = "fixed", "We have fixed the problem", "Our reply below says what we did."
    elif row.status == "answered" and row.category == "network":
        stage = "answered"
        headline = "We have passed your report to our network team"
        detail = "If we find an outage in your area, we will link your complaint to it and tell you when it is fixed."
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
        "timeline": _timeline(session, row, place),
        "messages": _messages(session, row),
        "can_report_still_down": can_report,
    }


def report_still_down(session: Session, row: SupportComplaintRow, *, note: str | None = None,
                      ctx: Any | None = None, settings: AppSettings | None = None,
                      now: datetime | None = None) -> SupportComplaintRow:
    """The customer says service is still down after we told them it was back (or after their
    ticket closed without a word).

    Under the desk's write lock (so two taps make one report): reopen the complaint to a person
    (``still_down_after_restore``, a reply due within ``track.still_down_reply_hours``), record their
    words and our reply, note it on the incident for the NOC, publish ``support.still_down``, and
    count it towards a surge for the place (``origin="still_down"``) -- in a savepoint of its own, so
    a surge failure is logged and never loses the report. Commits. Raises :class:`TrackConflict`
    with the sentence when the rules do not allow a report now.
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
        refusal = still_down_refusal(session, row, policy, now, settings)
        if refusal is not None:
            raise TrackConflict(refusal)
        where = _where(session, row, settings)
        inc = where.inc
        place = where.place or "your area"
        reason = policy.track.still_down_reason
        row.status, row.outcome = "escalated", OUTCOME_FOR_STATUS["escalated"]
        row.escalation_reason_code, row.escalation_reason, row.escalated_at = STILL_DOWN_REASON_CODE, reason, now
        row.claimed_by, row.claimed_at = None, None
        row.closure_reason = None
        row.still_down_at = now
        row.updated_at = now
        row.sla_due_at = now + timedelta(hours=policy.track.still_down_reply_hours)
        session.add(SupportMessageRow(complaint_id=row.id, author="customer", name=row.customer_name,
                                      body=text or f"Service is still down in {place}.", at=now, channel="web"))
        row.reply = (f"You told us service is still down. A member of our team will check and reply by "
                     f"{desk._due_text(row.sla_due_at, settings.operator.timezone)}.")
        session.add(SupportMessageRow(complaint_id=row.id, author="agent", name=desk.AGENT_NAME, body=row.reply, at=now,
                                      channel="web"))
        number = inc.incident_number if inc is not None else None
        after = "after the ticket closed" if where.terminal_untold else "after the restore"
        desk._human_step(session, row, "still_down_reported",
                         f"The customer reports service is still down in {place}"
                         + (f" {after} ({number})" if number else "") + "; back to a person.",
                         {"incident_id": inc.id if inc else None, "incident_number": number, "note": text or None,
                          "place": where.place_key, "told": where.told_here}, now, agent=AGENT)
        if inc is not None:
            told, down = _told_and_down(session, inc)
            session.add(WorkNoteRow(
                incident_id=inc.id, author=desk.AGENT_NAME, author_role="AGENT", source="support",
                body=f"Customer {row.ref} reports service is still down in {place} {after} ({down} of {told} told)",
            ))
        buffer_event(session, RealtimeEvent(type=EVENT_STILL_DOWN, operator_id=row.operator_id,
                                            incident_id=inc.id if inc else None,
                                            payload={"ref": row.ref, "incident_number": number, "place": place}))
        desk._emit(session, desk.EVENT_ESCALATED, row, reason_code=STILL_DOWN_REASON_CODE)
        surge.observe_still_down_safely(session, row, parent=inc, ctx=ctx, settings=settings, now=now)
        session.commit()
    except Exception:
        session.rollback()
        raise
    return row


def _told_and_down(session: Session, inc: IncidentRow) -> tuple[int, int]:
    """(numbers told about ``inc``, numbers that reported still down about it since), from the history."""
    ledger = _ledger(session, inc.operator_id)
    return len(set(ledger.told.get(inc.id, {}))), len(ledger.down.get(inc.id, set()))


# ------------------------------------------------------------------------------ the floor


@dataclass
class _Ledger:
    """Who complained about which incident, who was told and who said still down -- from the
    complaints AND the history, so a relink never makes an earlier tell disappear (MINOR 10)."""

    complaints: dict[str, list[SupportComplaintRow]]  # incident id -> complaints about it
    told: dict[str, dict[str, datetime]]  # incident id -> number -> first tell
    told_at: dict[tuple[str, str], datetime]  # (incident id, complaint id) -> latest tell
    down: dict[str, set[str]]  # incident id -> numbers that said still down
    down_at: dict[tuple[str, str], datetime]  # (incident id, complaint id) -> latest report


def _ledger(session: Session, operator_id: str) -> _Ledger:
    tells = _events(session, operator_id, "told_restored")
    downs = _events(session, operator_id, "still_down_reported")
    ids = {e.complaint_id for e in tells + downs}
    rows = list(session.scalars(select(SupportComplaintRow).where(
        SupportComplaintRow.operator_id == operator_id,
        or_(SupportComplaintRow.linked_incident_id.is_not(None), SupportComplaintRow.told_incident_id.is_not(None),
            SupportComplaintRow.id.in_(ids)),
    ).order_by(SupportComplaintRow.created_at.desc(), SupportComplaintRow.ref.desc())).all())
    by_id = {r.id: r for r in rows}
    # What the complaints say now joins the history (rows written before the history was read, and
    # any tell whose step was lost): the union, never the latest alone.
    tells = tells + [_Event(r.told_incident_id, r.id, r.msisdn, r.told_restored_at) for r in rows
                     if r.told_incident_id and r.told_restored_at]
    complaints: dict[str, list[SupportComplaintRow]] = {}

    def about(incident_id: str | None, row: SupportComplaintRow | None) -> None:
        if incident_id and row is not None and row not in complaints.setdefault(incident_id, []):
            complaints[incident_id].append(row)

    for r in rows:
        about(r.linked_incident_id, r)
        about(r.told_incident_id, r)
    told: dict[str, dict[str, datetime]] = {}
    told_at: dict[tuple[str, str], datetime] = {}
    for e in tells:
        about(e.incident_id, by_id.get(e.complaint_id))
        if e.incident_id:
            first = told.setdefault(e.incident_id, {})
            first[e.msisdn] = min(first.get(e.msisdn, e.at), e.at)
            told_at[(e.incident_id, e.complaint_id)] = max(told_at.get((e.incident_id, e.complaint_id), e.at), e.at)
    down: dict[str, set[str]] = {}
    down_at: dict[tuple[str, str], datetime] = {}
    for e in downs:
        about(e.incident_id, by_id.get(e.complaint_id))
        if e.incident_id:
            down.setdefault(e.incident_id, set()).add(e.msisdn)
            down_at[(e.incident_id, e.complaint_id)] = max(down_at.get((e.incident_id, e.complaint_id), e.at), e.at)
    for rows_ in complaints.values():
        rows_.sort(key=lambda r: (r.created_at, r.ref), reverse=True)
    return _Ledger(complaints, told, told_at, down, down_at)


def _notice_out(session: Session, inc: IncidentRow) -> dict[str, Any]:
    notice = session.scalar(select(SupportNoticeRow).where(
        SupportNoticeRow.operator_id == inc.operator_id, SupportNoticeRow.incident_id == inc.id,
    ).order_by(SupportNoticeRow.created_at.desc(), SupportNoticeRow.id.desc()).limit(1))
    if notice is not None:
        held = notice.state == "held_back"
        return {"state": notice.state, "card_id": notice.card_id, "recipients": notice.recipients,
                "sent_at": iso_z(notice.sent_at), "reason": notice.reason if held else None,
                "held_by": notice.decided_by if held else None, "held_at": iso_z(notice.decided_at) if held else None}
    waiting = inc.status not in CLOSED_INCIDENT or (
        inc.status == IncidentStatus.RESTORED.value and inc.restored_source not in TELL_SOURCES)
    return {"state": "waiting_for_restore" if waiting else "none", "card_id": None, "recipients": 0, "sent_at": None,
            "reason": None, "held_by": None, "held_at": None}


def _counts(ledger: _Ledger, inc_id: str) -> dict[str, int]:
    """customers / told / waiting / still_down are distinct NUMBERS; repeat contacts are complaints
    beyond each number's first about this incident."""
    rows = ledger.complaints.get(inc_id, [])
    numbers = {r.msisdn for r in rows}
    told = set(ledger.told.get(inc_id, {}))
    return {"customers": len(numbers), "told": len(told & numbers), "waiting": len(numbers - told),
            "still_down": len(ledger.down.get(inc_id, set()) & numbers), "repeat_contacts": len(rows) - len(numbers)}


def _follow_up(session: Session, inc: IncidentRow) -> dict[str, Any] | None:
    """The ticket opened from this incident's still-down reports (7.4), if any."""
    surge = session.scalar(select(SupportSurgeRow).where(
        SupportSurgeRow.operator_id == inc.operator_id, SupportSurgeRow.parent_incident_id == inc.id,
        SupportSurgeRow.incident_id.is_not(None)).order_by(SupportSurgeRow.decided_at.desc()).limit(1))
    if surge is None:
        return None
    follow = session.scalar(select(IncidentRow).where(IncidentRow.id == surge.incident_id,
                                                      IncidentRow.operator_id == inc.operator_id))
    if follow is None:
        return None
    return {"incident_id": follow.id, "incident_number": follow.incident_number, "status": follow.status}


def incident_customers(session: Session, inc: IncidentRow) -> dict[str, Any]:
    """The incident page's panel: who complained about this incident and whether they were told."""
    ledger = _ledger(session, inc.operator_id)
    rows = ledger.complaints.get(inc.id, [])
    counts = _counts(ledger, inc.id)
    return {
        "customers": counts["customers"],
        "told": counts["told"],
        "waiting": counts["waiting"],
        "still_down": counts["still_down"],
        "notice": _notice_out(session, inc),
        "complaints": [{"id": r.id, "ref": r.ref, "msisdn_masked": mask_msisdn_staff(r.msisdn), "status": r.status,
                        "told_restored_at": iso_z(ledger.told_at.get((inc.id, r.id))),
                        "still_down_at": iso_z(ledger.down_at.get((inc.id, r.id))),
                        "closure_reason": r.closure_reason}
                       for r in rows],
        "follow_up": _follow_up(session, inc),
    }


def outages(session: Session, operator_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """One row per incident with complaints about it, newest incident first."""
    ledger = _ledger(session, operator_id)
    if not ledger.complaints:
        return []
    incidents = session.scalars(select(IncidentRow).where(
        IncidentRow.id.in_(list(ledger.complaints)), IncidentRow.operator_id == operator_id,
    ).order_by(IncidentRow.created_at.desc(), IncidentRow.incident_number.desc()).limit(limit)).all()
    opened = set(session.scalars(select(SupportSurgeRow.incident_id).where(
        SupportSurgeRow.operator_id == operator_id, SupportSurgeRow.incident_id.is_not(None),
        or_(SupportSurgeRow.outcome.is_(None), SupportSurgeRow.outcome != "linked_existing"))).all())
    out = []
    for inc in incidents:
        places: list[str] = []
        for r in ledger.complaints[inc.id]:
            name = place_label(complaint_place(r))
            if name and name not in places:
                places.append(name)
        out.append({
            "incident_id": inc.id, "incident_number": inc.incident_number, "title": inc.title,
            "priority": inc.priority, "status": inc.status, "places": places,
            "restored_at": iso_z(inc.restored_at), "restore_source": inc.restored_source,
            **_counts(ledger, inc.id),
            "notice": _notice_out(session, inc),
            "from_customer_reports": inc.id in opened,
        })
    return out


def loop_metrics(session: Session, operator_id: str, *, hours: int = 0, now: datetime | None = None) -> dict[str, Any]:
    """The contract's ``Loop`` over the last ``hours`` (0 = all time).

    Windowed by when the thing happened: ``told`` (and its timings) by the first SMS to that number
    about that incident, ``still_down_reports`` by the report, the repeat and outage counts by the
    complaint, ``spotted_by_customers`` and settled surges by the decision. ``waiting_to_hear``,
    ``notices_waiting``, ``recipients_waiting`` and open surges are the state NOW. People are
    distinct numbers per incident, and the tell history comes from the steps, so a later relink
    never takes an earlier tell away.
    """
    now = now or utcnow()
    since = now - timedelta(hours=hours) if hours > 0 else None

    def in_window(at: datetime | None) -> bool:
        return at is not None and (since is None or at >= since)

    ledger = _ledger(session, operator_id)
    incident_ids = set(ledger.complaints) | set(ledger.told)
    incidents = {inc.id: inc for inc in session.scalars(select(IncidentRow).where(
        IncidentRow.id.in_(list(incident_ids)), IncidentRow.operator_id == operator_id)).all()} if incident_ids else {}
    linked_rows = [r for rows in ledger.complaints.values() for r in rows]
    waiting_pairs = {(r.linked_incident_id, r.msisdn) for r in linked_rows
                     if r.linked_incident_id in incidents and incidents[r.linked_incident_id].status not in CLOSED_INCIDENT
                     and r.msisdn not in ledger.told.get(r.linked_incident_id, {})}
    minutes = []
    told = 0
    for incident_id, numbers in ledger.told.items():
        inc = incidents.get(incident_id)
        for at in numbers.values():
            if not in_window(at):
                continue
            told += 1
            restored = (inc.restored_at or inc.closed_at) if inc is not None else None
            if restored is not None:
                minutes.append(max(0.0, (at - restored).total_seconds() / 60))

    pairs: dict[tuple[str, str], int] = {}
    seen: set[str] = set()
    for r in linked_rows:
        if r.id in seen or not r.linked_incident_id or not in_window(r.created_at):
            continue
        seen.add(r.id)
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
    confirmed = [s for s in surges if s.status in ("confirmed", "ingesting") and in_window(s.decided_at)]
    dismissed = sum(1 for s in surges if s.status == "dismissed" and in_window(s.decided_at))
    spotted = {s.incident_id for s in confirmed if s.incident_id and s.outcome != "linked_existing"}
    return {
        "waiting_to_hear": len(waiting_pairs),
        "told": told,
        "told_median_minutes": round(statistics.median(minutes), 1) if minutes else None,
        "told_p90_minutes": round(_p90(minutes), 1) if minutes else None,
        "notices_waiting": len(cards),
        "recipients_waiting": int(recipients_waiting),
        "still_down_reports": int(still_down),
        "repeat_contacts": repeat,
        "outages_with_complaints": outages_with,
        "repeat_contacts_per_outage": round(repeat / outages_with, 2) if outages_with else None,
        "spotted_by_customers": len(spotted),
        "surges": {"open": open_surges, "confirmed": len(confirmed), "dismissed": dismissed},
    }
