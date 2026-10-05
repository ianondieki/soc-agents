"""Complaints as an early outage signal (docs/CLOSE_THE_LOOP.md section 3).

A burst of network complaints about one town with no alarm behind them told the NOC nothing.
:func:`observe` runs after each complaint the API or the demo seeder processes (never inside the
eval runner, whose complaints live in a throwaway database): it looks at network complaints that
named a place the gazetteer knows and did NOT link to an open incident, and when at least
``surge.threshold`` distinct numbers (default 3) name the same place inside ``surge.window_minutes``
(default 30) it opens a surge and one ``CONFIRM_POSSIBLE_OUTAGE`` card for the NOC. Later complaints
about that place join the open surge and refresh the card. A still-down report after a restore
(:func:`observe_still_down`, called by :func:`loop.report_still_down`) counts too, with
``origin="still_down"``; ``surge.still_down_threshold`` distinct numbers (default 2) of those raise
the card on their own.

Approve ("Open a ticket and tell them"): :func:`mark_confirmed` runs inside the HITL route's
transaction, and AFTER that commit :func:`confirm_surge` runs one synthetic alarm through the normal
ingest (``graph.pipeline.process_event`` -- imported from the pipeline, not from ``main``, so there
is no import cycle), links every complaint in the surge to the new ticket and tells each number once,
approved by the person who confirmed. If the ingest fails the surge stays ``confirmed`` with
``error`` set and keeps collecting complaints, and ``POST /surges/{id}/retry`` runs it again
(reusing a ticket an earlier attempt already opened). Reject ("Dismiss"): :func:`dismiss`.

**Counted once.** A complaint is a member of at most one surge (a still-down report is counted by
its time, so a report a day later can count again); complaints a dismissed surge already held do not
count towards the next one, so dismissing does not simply re-open the card on the next complaint.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from noc_agents.config import AppSettings, get_settings
from noc_agents.db.models import HitlTaskRow, IncidentRow, WorkNoteRow, get_session, utcnow
from noc_agents.db.models_support import (
    SupportComplaintRow,
    SupportMessageRow,
    SupportStepRow,
    SupportSurgeMemberRow,
    SupportSurgeRow,
)
from noc_agents.domain.enums import HitlTaskType
from noc_agents.domain.schemas import EventIngest
from noc_agents.orchestrator import outbox
from noc_agents.realtime.commit_hook import buffer_event
from noc_agents.realtime.hub import RealtimeEvent
from noc_agents.services.clock import iso_z
from noc_agents.support import desk
from noc_agents.support.context import SupportContext, default_context
from noc_agents.support.loop import (
    AGENT,
    CLOSED_INCIDENT,
    RAISED_BY,
    complaint_place,
    incident_area,
    msisdn_hash,
    place_label,
    sms_language,
)
from noc_agents.support.text import normalise

log = logging.getLogger(__name__)

SURGE_TASK_TYPE = HitlTaskType.CONFIRM_POSSIBLE_OUTAGE.value
ENTITY_TYPE = "support_surge"
EVENT_SURGE = "support.surge"
ALARM_CODE = "CUSTOMER_REPORTED_OUTAGE"
SOURCE = "customer_reports"
EXCERPTS_MAX = 6
EXCERPT_CHARS = 140
CONFIRMED_TEXT = {
    "en": "We have confirmed an outage in {place} (ticket {ticket}). Engineers are on it; we will tell you when "
          "service is back.",
    "sw": "Tumethibitisha hitilafu ya mtandao {place} (tiketi {ticket}). Wahandisi wanalishughulikia; tutakujulisha "
          "huduma itakaporejea.",
}


class SurgeConflict(Exception):
    """The surge is not in a state the action fits (the API answers 409)."""


def confirmed_text(language: str, *, place: str, ticket: str) -> str:
    return CONFIRMED_TEXT[sms_language(language)].format(place=place, ticket=ticket)


def confirmed_key(incident_id: str, msisdn: str) -> str:
    return f"support-confirmed:{incident_id}:{msisdn_hash(msisdn)}"


def site_id_for(region_code: str | None, place: str) -> str:
    """``CUST-{REGION}-{PLACE}``: ``CUST-NBI_W-ONGATA-RONGAI``."""
    return f"CUST-{(region_code or 'NA').upper()}-{normalise(place).upper().replace(' ', '-')}"


# ------------------------------------------------------------------------------- reading


def _open_surge(session: Session, operator_id: str, place: str) -> SupportSurgeRow | None:
    """The surge still collecting complaints about ``place`` (open, or confirmed with no ticket yet)."""
    return session.scalar(select(SupportSurgeRow).where(
        SupportSurgeRow.operator_id == operator_id, SupportSurgeRow.open_place == place))


def _members(session: Session, surge: SupportSurgeRow) -> list[tuple[SupportSurgeMemberRow, SupportComplaintRow]]:
    return list(session.execute(
        select(SupportSurgeMemberRow, SupportComplaintRow)
        .join(SupportComplaintRow, SupportComplaintRow.id == SupportSurgeMemberRow.complaint_id)
        .where(SupportSurgeMemberRow.surge_id == surge.id, SupportComplaintRow.operator_id == surge.operator_id)
        .order_by(SupportSurgeMemberRow.at, SupportSurgeMemberRow.id)
    ).all())


def _region_for(ctx: SupportContext, place: str, parent: IncidentRow | None) -> str | None:
    """The gazetteer's region for the place; a place in several regions takes the first (sorted)
    one unless the incident it was restored under says which (decision in the contract)."""
    regions = ctx.gazetteer.regions_of(place)
    if parent is not None and parent.region_code in regions:
        return parent.region_code
    if regions:
        return regions[0]
    return parent.region_code if parent is not None else None


def _step_detail(step: SupportStepRow) -> dict[str, Any]:
    try:
        return json.loads(step.detail_json or "{}")
    except ValueError:
        return {}


def _candidates(session: Session, operator_id: str, place: str, at: datetime, ctx: SupportContext,
                ) -> tuple[list[SupportComplaintRow], list[tuple[SupportStepRow, SupportComplaintRow]]]:
    """What would count towards a NEW surge for ``place`` at ``at``: unlinked network complaints
    in the window that no surge holds, and still-down reports in their window no surge holds."""
    rule = ctx.policy.surge
    held = select(SupportSurgeMemberRow.complaint_id).where(SupportSurgeMemberRow.kind == "complaint")
    complaints = list(session.scalars(select(SupportComplaintRow).where(
        SupportComplaintRow.operator_id == operator_id,
        SupportComplaintRow.category == "network",
        SupportComplaintRow.place == place,
        SupportComplaintRow.linked_incident_id.is_(None),
        SupportComplaintRow.created_at >= at - timedelta(minutes=rule.window_minutes),
        SupportComplaintRow.created_at <= at,
        SupportComplaintRow.id.not_in(held),
    )).all())
    since = at - timedelta(minutes=rule.still_down_window_minutes)
    reports = []
    for step, row in session.execute(
        select(SupportStepRow, SupportComplaintRow)
        .join(SupportComplaintRow, SupportComplaintRow.id == SupportStepRow.complaint_id)
        .where(SupportComplaintRow.operator_id == operator_id, SupportStepRow.agent == AGENT,
               SupportStepRow.action == "still_down_reported", SupportStepRow.at >= since, SupportStepRow.at <= at)
    ).all():
        if _step_detail(step).get("place") != place:
            continue
        counted = session.scalar(select(SupportSurgeMemberRow.id).where(
            SupportSurgeMemberRow.complaint_id == row.id, SupportSurgeMemberRow.kind == "still_down",
            SupportSurgeMemberRow.at == step.at))
        if counted is None:
            reports.append((step, row))
    return complaints, reports


# ------------------------------------------------------------------------------- writing


def _excerpt(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= EXCERPT_CHARS else text[: EXCERPT_CHARS - 1].rstrip() + "…"


def _refresh(session: Session, surge: SupportSurgeRow) -> None:
    """Recount the surge from its members and refresh the card's payload while the card is open."""
    members = _members(session, surge)
    surge.complaints = len({row.id for _, row in members})
    surge.numbers = len({row.msisdn for _, row in members})
    if members:
        surge.first_at = min(m.at for m, _ in members)
        surge.last_at = max(m.at for m, _ in members)
    surge.updated_at = utcnow()
    card = session.get(HitlTaskRow, surge.card_id) if surge.card_id else None
    if card is None or card.status not in ("PENDING", "CLAIMED"):
        return
    parent = session.get(IncidentRow, surge.parent_incident_id) if surge.parent_incident_id else None
    excerpts = []
    for member, row in sorted(members, key=lambda mr: mr[0].at, reverse=True)[:EXCERPTS_MAX]:
        text = row.body
        if member.kind == "still_down":
            note = next((m.body for m in session.scalars(select(SupportMessageRow).where(
                SupportMessageRow.complaint_id == row.id, SupportMessageRow.author == "customer",
                SupportMessageRow.at == member.at)).all()), None)
            text = f"Still down after the restore: {note}" if note else "Still down after the restore."
        excerpts.append({"ref": row.ref, "text": _excerpt(text), "at": iso_z(member.at)})
    card.proposed_payload = {
        "kind": "surge",
        "place": place_label(surge.place),
        "region_code": surge.region_code,
        "complaints": surge.complaints,
        "numbers": surge.numbers,
        "first_at": iso_z(surge.first_at),
        "last_at": iso_z(surge.last_at),
        "origin": surge.origin,
        "parent_incident_number": parent.incident_number if parent is not None else None,
        "excerpts": excerpts,
    }


def _surge_event(session: Session, surge: SupportSurgeRow) -> None:
    buffer_event(session, RealtimeEvent(type=EVENT_SURGE, operator_id=surge.operator_id, incident_id=surge.incident_id,
                                        payload={"surge_id": surge.id, "place": place_label(surge.place),
                                                 "complaints": surge.complaints}))


def _add_member(session: Session, surge: SupportSurgeRow, row: SupportComplaintRow, kind: str, at: datetime) -> bool:
    exists = session.scalar(select(SupportSurgeMemberRow.id).where(
        SupportSurgeMemberRow.surge_id == surge.id, SupportSurgeMemberRow.complaint_id == row.id,
        SupportSurgeMemberRow.kind == kind))
    if exists is not None:
        return False
    session.add(SupportSurgeMemberRow(surge_id=surge.id, complaint_id=row.id, kind=kind, at=at))
    session.flush()
    return True


def _join(session: Session, surge: SupportSurgeRow, row: SupportComplaintRow, kind: str, at: datetime) -> SupportSurgeRow:
    if _add_member(session, surge, row, kind, at):
        _refresh(session, surge)
        _surge_event(session, surge)
    return surge


def _maybe_open(session: Session, operator_id: str, place: str, at: datetime, ctx: SupportContext,
                ) -> SupportSurgeRow | None:
    """Open a surge (and its card) for ``place`` when what counts at ``at`` reaches a threshold."""
    rule = ctx.policy.surge
    complaints, reports = _candidates(session, operator_id, place, at, ctx)
    numbers = {row.msisdn for row in complaints}
    down = {row.msisdn for _, row in reports}
    if len(down) < rule.still_down_threshold and len(numbers | down) < rule.threshold:
        return None
    parent = None
    if reports:
        latest = max(reports, key=lambda sr: sr[0].at)
        parent_id = _step_detail(latest[0]).get("incident_id")
        parent = session.scalar(select(IncidentRow).where(IncidentRow.id == parent_id, IncidentRow.operator_id == operator_id)) \
            if parent_id else None
    now = utcnow()
    surge = SupportSurgeRow(
        operator_id=operator_id, place=place, region_code=_region_for(ctx, place, parent), status="open",
        origin="still_down" if reports else "complaints", parent_incident_id=parent.id if parent else None,
        open_place=place, first_at=at, last_at=at, created_at=now, updated_at=now,
    )
    session.add(surge)
    session.flush()  # the unique (operator_id, open_place) key: a racing opener fails here
    for row in complaints:
        session.add(SupportSurgeMemberRow(surge_id=surge.id, complaint_id=row.id, kind="complaint", at=row.created_at))
    for step, row in reports:
        session.add(SupportSurgeMemberRow(surge_id=surge.id, complaint_id=row.id, kind="still_down", at=step.at))
    session.flush()
    card = HitlTaskRow(incident_id=None, operator_id=operator_id, task_type=SURGE_TASK_TYPE, entity_type=ENTITY_TYPE,
                       entity_id=surge.id, created_by=RAISED_BY, status="PENDING")
    card.proposed_payload = {}
    session.add(card)
    session.flush()
    surge.card_id = card.id
    _refresh(session, surge)
    buffer_event(session, RealtimeEvent(type="hitl.created", operator_id=operator_id, incident_id=None,
                                        payload={"task_id": card.id, "task_type": SURGE_TASK_TYPE, "surge_id": surge.id,
                                                 "place": place_label(place), "incident_number": None}))
    _surge_event(session, surge)
    log.info("support: possible outage in %s (%d complaints, %d numbers, origin %s); card %s",
             place, surge.complaints, surge.numbers, surge.origin, card.id)
    return surge


def observe(session: Session, row: SupportComplaintRow, *, ctx: SupportContext | None = None) -> SupportSurgeRow | None:
    """After a complaint is committed (the API's form, the demo seeder; never the eval runner):
    join the place's open surge, or open one when the threshold is reached. Commits its own
    transaction under the desk's write lock. Returns the surge the complaint is in, if any."""
    if row.category != "network" or row.linked_incident_id:
        return None
    place = complaint_place(row)
    if not place:
        return None
    ctx = ctx or default_context()
    for attempt in (1, 2):
        try:
            desk._write_lock(session)
            surge = _open_surge(session, row.operator_id, place)
            if surge is not None:
                _join(session, surge, row, "complaint", row.created_at)
            else:
                surge = _maybe_open(session, row.operator_id, place, row.created_at, ctx)
            session.commit()
            return surge
        except IntegrityError:
            session.rollback()  # another opener won the unique key: join theirs on the second pass
            if attempt == 2:
                raise
        except Exception:
            session.rollback()
            raise
    return None


def observe_still_down(session: Session, row: SupportComplaintRow, *, parent: IncidentRow | None,
                       ctx: SupportContext, settings: AppSettings, now: datetime) -> SupportSurgeRow | None:
    """Count a still-down report (already recorded as a step at ``now``) towards a surge for the
    place. Runs inside the report's transaction; the caller commits."""
    place = complaint_place(row) or (normalise(incident_area(parent, settings)) if parent is not None else None)
    if not place:
        return None
    surge = _open_surge(session, row.operator_id, place)
    if surge is not None:
        return _join(session, surge, row, "still_down", now)
    return _maybe_open(session, row.operator_id, place, now, ctx)


# ------------------------------------------------------------------------------ deciding


def _surge_for(session: Session, task: HitlTaskRow) -> SupportSurgeRow | None:
    if task.entity_type != ENTITY_TYPE or not task.entity_id:
        return None
    return session.scalar(select(SupportSurgeRow).where(
        SupportSurgeRow.id == task.entity_id, SupportSurgeRow.operator_id == task.operator_id))


def mark_confirmed(session: Session, task: HitlTaskRow, *, actor: str, at: datetime) -> str | None:
    """Inside the approval's transaction: the surge is ``confirmed``. Returns its id for
    :func:`confirm_surge` to run after the commit, or None when the card names no open surge."""
    surge = _surge_for(session, task)
    if surge is None or surge.status != "open":
        return None
    surge.status, surge.decided_by, surge.decided_at, surge.updated_at = "confirmed", actor, at, at
    return surge.id


def dismiss(session: Session, task: HitlTaskRow, *, actor: str, reason: str, at: datetime) -> SupportSurgeRow | None:
    """Inside the rejection's transaction: the surge is ``dismissed``; its complaints stay as they were."""
    surge = _surge_for(session, task)
    if surge is None or surge.status != "open":
        return None
    surge.status, surge.open_place = "dismissed", None
    surge.decided_by, surge.decided_at, surge.reason, surge.updated_at = actor, at, reason, at
    return surge


def _local(dt: datetime, timezone: str) -> str:
    return f"{dt.replace(tzinfo=ZoneInfo('UTC')).astimezone(ZoneInfo(timezone)):%H:%M}"


def description_for(surge: SupportSurgeRow, timezone: str) -> str:
    """"4 customers reported no service in Rongai between 14:05 and 14:31; no network alarm"."""
    who = f"{surge.numbers} customer{'s' if surge.numbers != 1 else ''}"
    first, last = _local(surge.first_at, timezone), _local(surge.last_at, timezone)
    when = f"between {first} and {last}" if first != last else f"at {first}"
    return f"{who} reported no service in {place_label(surge.place)} {when}; no network alarm"


def _ticket(session: Session, surge: SupportSurgeRow, settings: AppSettings) -> IncidentRow:
    """The ticket for a confirmed surge: one an earlier attempt already opened, else a new one from
    one synthetic alarm through the normal ingest (which commits)."""
    from noc_agents.graph.pipeline import process_event  # the existing ingest service; main is never imported

    site_id = site_id_for(surge.region_code, surge.place)
    if surge.incident_id:
        existing = session.get(IncidentRow, surge.incident_id)
        if existing is not None:
            return existing
    since = (surge.decided_at or surge.created_at) - timedelta(minutes=1)
    earlier = session.scalar(select(IncidentRow).where(
        IncidentRow.operator_id == surge.operator_id, IncidentRow.site_id == site_id,
        IncidentRow.created_at >= since, IncidentRow.status.not_in(tuple(CLOSED_INCIDENT)),
    ).order_by(IncidentRow.created_at.desc()))
    if earlier is not None:
        return earlier
    event = EventIngest(
        site_id=site_id,
        site_name=f"{place_label(surge.place)} (customer reports)",
        region_code=surge.region_code or "NBI",
        alarm_code=ALARM_CODE,
        failure_domain="UNKNOWN",
        source=SOURCE,
        description=description_for(surge, settings.operator.timezone),
        # Never the restored incident, even for a still-down surge: CORRELATE merges an alarm that
        # names a parent into that parent instead of opening a ticket, and a child ticket is one
        # link_incident skips, so later complaints about the place would miss it and feed another
        # surge. The relation lives on the surge and in a work note on both incidents (decision).
        parent_incident_id=None,
    )
    session.commit()  # end the read transaction: the ingest runs its own
    return process_event(session, settings, event)


def _link_and_tell(session: Session, surge_id: str, inc: IncidentRow, *, actor: str, approved_at: datetime,
                   operator_id: str) -> int:
    """Link every complaint in the surge to ``inc``, step and tell each number once (approved by the
    person who confirmed), settle the surge. One transaction under the desk's write lock."""
    desk._write_lock(session)
    surge = session.scalar(select(SupportSurgeRow).where(SupportSurgeRow.id == surge_id,
                                                         SupportSurgeRow.operator_id == operator_id))
    session.refresh(inc)
    if surge is None:
        return 0
    place = place_label(surge.place)
    parent = session.get(IncidentRow, surge.parent_incident_id) if surge.parent_incident_id else None
    who = f"{surge.numbers} customer{' says' if surge.numbers == 1 else 's say'}"
    if parent is not None and parent.id != inc.id:
        # The ticket stays top-level (no parent_incident_id): the relation to the restored incident is
        # the surge's parent_incident_id and this note, written on BOTH incidents.
        after = f"after {parent.incident_number} was restored; {who} service is still down in {place}"
        session.add(WorkNoteRow(incident_id=inc.id, author=desk.AGENT_NAME, author_role="AGENT", source="support",
                                body=f"Opened from customer reports {after} (confirmed by {actor})."))
        session.add(WorkNoteRow(incident_id=parent.id, author=desk.AGENT_NAME, author_role="AGENT", source="support",
                                body=f"{inc.incident_number} opened from customer reports {after} (confirmed by {actor})."))
    else:
        session.add(WorkNoteRow(incident_id=inc.id, author=desk.AGENT_NAME, author_role="AGENT", source="support",
                                body=f"Opened from customer reports: {surge.complaints} complaint(s) from {surge.numbers} "
                                     f"number(s) about {place}, confirmed by {actor}."))
    by_number: dict[str, list[SupportComplaintRow]] = {}
    seen: set[str] = set()
    for _member, row in _members(session, surge):
        if row.id not in seen:  # a complaint can be in the surge twice (its words, then a still-down report)
            seen.add(row.id)
            by_number.setdefault(row.msisdn, []).append(row)
    told = 0
    for msisdn, rows in by_number.items():
        rows.sort(key=lambda r: r.created_at, reverse=True)
        lead = rows[0]
        language = sms_language(lead.language)
        text = confirmed_text(language, place=place, ticket=inc.incident_number)
        key = confirmed_key(inc.id, msisdn)
        outbox.enqueue(session, kind=outbox.SMS, idempotency_key=key, operator_id=operator_id,
                       requires_hitl=True, approved_by=actor, approved_at=approved_at,
                       payload={"operator_id": operator_id, "audience": "customer", "purpose": "support_confirmed_outage",
                                "complaint_id": lead.id, "complaint_ref": lead.ref, "complaint_ids": [r.id for r in rows],
                                "msisdn_masked": lead.msisdn_masked, "incident_id": inc.id,
                                "incident_number": inc.incident_number, "language": language, "body": text,
                                "surge_id": surge.id})
        for row in rows:
            already = row.linked_incident_id == inc.id
            row.linked_incident_id = inc.id
            row.updated_at = approved_at
            if already:
                continue
            session.add(SupportMessageRow(complaint_id=row.id, author="agent", name=desk.AGENT_NAME, body=text,
                                          at=approved_at, channel="sms"))
            desk._human_step(session, row, "linked_confirmed_outage",
                             f"Linked to {inc.incident_number}, opened from customer reports about {place} "
                             f"(confirmed by {actor}); told the customer.",
                             {"incident_id": inc.id, "incident_number": inc.incident_number, "surge_id": surge.id,
                              "outbox_key": key, "approved_by": actor}, approved_at, agent=AGENT)
            desk._emit(session, desk.EVENT_UPDATED, row, linked_incident=inc.incident_number)
        told += 1
    surge.incident_id, surge.error, surge.open_place, surge.updated_at = inc.id, None, None, utcnow()
    _surge_event(session, surge)
    session.commit()
    return told


def confirm_surge(surge_id: str, *, actor: str, approved_at: datetime, settings: AppSettings | None = None) -> SupportSurgeRow | None:
    """After the approval committed: open the ticket, link and tell. Never raises; a failure is kept
    on the surge (``error``) for the Outages tab's "Try again". Returns the surge as it ended."""
    from noc_agents.graph.pipeline import drain_once, sync_drain_enabled

    settings = settings or get_settings()
    operator_id = settings.operator.operator_id
    session = get_session()
    try:
        try:
            surge = session.scalar(select(SupportSurgeRow).where(SupportSurgeRow.id == surge_id,
                                                                 SupportSurgeRow.operator_id == operator_id))
            if surge is None or surge.status != "confirmed" or surge.incident_id:
                return surge  # nothing to confirm, or already done: never a second ticket or note
            inc = _ticket(session, surge, settings)
            _link_and_tell(session, surge_id, inc, actor=actor, approved_at=approved_at, operator_id=operator_id)
            if sync_drain_enabled():
                drain_once(session)  # after the commit: transmit the confirmed-outage SMS (a mock)
        except Exception as exc:  # noqa: BLE001 -- the approval is durable; the failure is shown, not raised
            session.rollback()
            log.exception("support: confirming surge %s failed; it stays confirmed with the error", surge_id)
            surge = session.scalar(select(SupportSurgeRow).where(SupportSurgeRow.id == surge_id,
                                                                 SupportSurgeRow.operator_id == operator_id))
            if surge is not None:
                surge.error = f"{type(exc).__name__}: {exc}"[:500]
                surge.updated_at = utcnow()
                session.commit()
        return session.scalar(select(SupportSurgeRow).where(SupportSurgeRow.id == surge_id,
                                                            SupportSurgeRow.operator_id == operator_id))
    finally:
        session.expunge_all()
        session.close()


def retry(session: Session, surge_id: str, *, operator_id: str, actor: str) -> str:
    """Check a failed confirm may be re-run; returns the id. The caller runs :func:`confirm_surge`."""
    surge = session.scalar(select(SupportSurgeRow).where(SupportSurgeRow.id == surge_id,
                                                         SupportSurgeRow.operator_id == operator_id))
    if surge is None:
        raise LookupError("surge not found")
    if surge.status != "confirmed" or surge.incident_id:
        raise SurgeConflict(f"only a confirmed surge whose ticket could not be opened can be retried "
                            f"(this one is {surge.status}{', ticket opened' if surge.incident_id else ''})")
    return surge.id


# --------------------------------------------------------------------------------- views


def surge_out(session: Session, surge: SupportSurgeRow) -> dict[str, Any]:
    inc = session.scalar(select(IncidentRow).where(IncidentRow.id == surge.incident_id,
                                                   IncidentRow.operator_id == surge.operator_id)) if surge.incident_id else None
    refs: list[str] = []
    for _member, row in _members(session, surge):
        if row.ref not in refs:
            refs.append(row.ref)
    return {
        "id": surge.id, "place": place_label(surge.place), "region_code": surge.region_code, "status": surge.status,
        "origin": surge.origin, "complaints": surge.complaints, "numbers": surge.numbers,
        "first_at": iso_z(surge.first_at), "last_at": iso_z(surge.last_at), "card_id": surge.card_id,
        "incident_id": surge.incident_id, "incident_number": inc.incident_number if inc is not None else None,
        "error": surge.error, "decided_by": surge.decided_by, "decided_at": iso_z(surge.decided_at),
        "reason": surge.reason, "complaint_refs": refs,
    }


def list_surges(session: Session, operator_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    rows = session.scalars(select(SupportSurgeRow).where(SupportSurgeRow.operator_id == operator_id)
                           .order_by(SupportSurgeRow.created_at.desc(), SupportSurgeRow.id.desc()).limit(limit)).all()
    return [surge_out(session, row) for row in rows]


def region_surges(session: Session, operator_id: str) -> dict[str, dict[str, Any]]:
    """The Regions card's ``complaint_surge`` per region: its open surge (the latest, if several)."""
    out: dict[str, dict[str, Any]] = {}
    for surge in session.scalars(select(SupportSurgeRow).where(
            SupportSurgeRow.operator_id == operator_id, SupportSurgeRow.status == "open",
            SupportSurgeRow.region_code.is_not(None)).order_by(SupportSurgeRow.created_at)).all():
        out[surge.region_code] = {
            "surge_id": surge.id, "place": place_label(surge.place), "complaints": surge.complaints,
            "numbers": surge.numbers, "first_at": iso_z(surge.first_at), "last_at": iso_z(surge.last_at),
            "card_id": surge.card_id,
        }
    return out
