"""WorklogMonitorAgent: chase silent incidents and SLA breaches (spec §5.3.11).

Runs from the scheduler (``monitor_tick``, 60 s) as ``agent_runs(graph_name="monitor",
trigger="SCHEDULE")`` through :func:`tick_job`, and on demand from ``POST /api/v1/monitor/tick``
through :func:`chase_silent_incidents` directly. Both paths share one rule set:

* **silence** — no MSP/FE/RNIO note for at least the priority's ``note_interval`` (× the
  region multiplier) *and* the owner's next expected update (``incidents.next_update_at``)
  is due. ``next_update_at`` is honoured: nothing is overdue before it. Every chase re-arms it
  from ``sla_minutes[P].note_interval``, so the cadence comes from the operator YAML, not a
  hard-coded 15 minutes;
* **SLA breach** — ack SLA passed with no vendor note, or restore SLA passed. A breach is
  chased whether or not ``next_update_at`` is due: the clock, not the cadence, is what breached;
* **one chase note per window** — the note is deduped against the last monitor chase note
  (``work_notes.source == "monitor"``): a second tick inside the same interval writes nothing
  and re-arms nothing. The incident is still *evaluated* (it still appears in the results and
  a P1/P2 restore breach still raises a GENERIC task when none is open), so a supervisor who
  rejected the last escalation gets a fresh one on the next tick — that is existing behaviour
  ``tests/integration/test_hitl_decisions.py`` depends on.

Realtime: ``monitor.chase`` when a note or a task was written, ``hitl.created`` for every
GENERIC task raised here — both published after the commit, never inside it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from noc_agents.config import AppSettings, OperatorConfig
from noc_agents.db.models import HitlTaskRow, IncidentRow, WorkNoteRow, new_id, utcnow
from noc_agents.realtime.hub import RealtimeEvent, hub
from noc_agents.scheduler import JobResult
from noc_agents.services.hitl import sync_incident_hitl_scalars

AUTHOR = "WorklogMonitorAgent"
CHASE_SOURCE = "monitor"  # the MONITOR graph node's "Monitoring started" note is source="agent", not this
ESCALATION_TASK_TYPE = "GENERIC"


@dataclass
class ChaseResult:
    incident_id: str
    incident_number: str
    action: str  # "chased" | "escalated"
    detail: str
    note_written: bool = True  # False: deduped against the last chase note in this window
    task_created: bool = False  # a GENERIC escalation task was raised on this pass


def _last_vendor_or_fe_note(session: Session, incident_id: str) -> WorkNoteRow | None:
    notes = session.scalars(
        select(WorkNoteRow)
        .where(WorkNoteRow.incident_id == incident_id)
        .order_by(WorkNoteRow.created_at.desc())
    ).all()
    for n in notes:
        if n.author_role in ("MSP", "FE", "RNIO") or n.source in ("msp", "fe", "vendor"):
            return n
    return None


def _last_chase_note(session: Session, incident_id: str) -> WorkNoteRow | None:
    return session.scalar(
        select(WorkNoteRow)
        .where(WorkNoteRow.incident_id == incident_id, WorkNoteRow.source == CHASE_SOURCE)
        .order_by(WorkNoteRow.created_at.desc())
        .limit(1)
    )


def _note_interval_minutes(inc: IncidentRow, cfg: OperatorConfig) -> int:
    band = cfg.sla_minutes.get(inc.priority)
    base = band.note_interval if band else 60
    mult = float((cfg.region_sla_note_multiplier or {}).get(inc.region_code, 1.0))
    return max(5, int(base * mult))


def chase_silent_incidents(session: Session, cfg: OperatorConfig) -> list[ChaseResult]:
    """WorklogMonitorAgent logic: flag silence, escalate SLA risk, optional HITL. Commits.

    Called by the scheduler's ``monitor_tick`` job (via :func:`tick_job`) and by
    ``POST /api/v1/monitor/tick``.
    """
    now = utcnow()
    open_rows = session.scalars(
        select(IncidentRow).where(
            IncidentRow.operator_id == cfg.operator_id,
            IncidentRow.status.not_in(["CLOSED", "CANCELLED", "RESTORED"]),
        )
    ).all()
    results: list[ChaseResult] = []
    events: list[RealtimeEvent] = []

    for inc in open_rows:
        interval = _note_interval_minutes(inc, cfg)
        window = timedelta(minutes=interval)
        last = _last_vendor_or_fe_note(session, inc.id)
        reference: datetime = last.created_at if last else inc.created_at
        silent_for = (now - reference).total_seconds() / 60.0
        sla_restore_breach = bool(inc.sla_restore_due and now > inc.sla_restore_due)
        sla_ack_breach = bool(inc.sla_ack_due and now > inc.sla_ack_due and not last)
        # next_update_at honoured: silence is only overdue once the owner's next update was due.
        update_due = inc.next_update_at is None or now >= inc.next_update_at
        silence = silent_for >= interval and update_due

        if not (silence or sla_restore_breach or sla_ack_breach):
            continue

        reasons = []
        if silent_for >= interval:
            reasons.append(f"no MSP/FE note for {int(silent_for)}m (interval {interval}m)")
        if sla_ack_breach:
            reasons.append("ack SLA breached")
        if sla_restore_breach:
            reasons.append("restore SLA breached")
        detail = "; ".join(reasons)

        # One chase note per window: dedupe against the last monitor chase note.
        prior = _last_chase_note(session, inc.id)
        note_written = prior is None or (now - prior.created_at) >= window
        if note_written:
            session.add(
                WorkNoteRow(
                    incident_id=inc.id,
                    author=AUTHOR,
                    author_role="AGENT",
                    body=(
                        f"[{AUTHOR}] SILENCE/SLA CHASE on {inc.incident_number}: {detail}. "
                        f"Owner {inc.assignee_name}. RNIO escalate if no update. "
                        f"Next check in {interval}m."
                    ),
                    source=CHASE_SOURCE,
                )
            )
            inc.updated_at = now
            inc.next_update_at = now + window  # sla_minutes[P].note_interval × region multiplier

        # Escalate: create HITL for P1/P2 on restore breach or long silence (>2x interval).
        # Dedupe only against OPEN tasks: a rejected escalation is re-raised next pass.
        escalate = sla_restore_breach or silent_for >= (interval * 2)
        task_created = False
        if escalate and inc.priority in ("P1", "P2"):
            existing = session.scalar(
                select(HitlTaskRow).where(
                    HitlTaskRow.incident_id == inc.id,
                    HitlTaskRow.task_type == ESCALATION_TASK_TYPE,
                    HitlTaskRow.status.in_(["PENDING", "CLAIMED"]),
                )
            )
            if not existing:
                task = HitlTaskRow(
                    id=new_id(),
                    incident_id=inc.id,
                    task_type=ESCALATION_TASK_TYPE,
                    status="PENDING",
                )
                task.proposed_payload = {
                    "reason": "sla_or_silence_escalation",
                    "detail": detail,
                    "suggested_action": "Call MSP lead / reassign / update exec brief",
                    "assignee": inc.assignee_name,
                }
                session.add(task)
                sync_incident_hitl_scalars(session, inc)
                task_created = True
                events.append(
                    RealtimeEvent(
                        type="hitl.created",
                        operator_id=cfg.operator_id,
                        incident_id=inc.id,
                        payload={
                            "task_id": task.id,
                            "incident_number": inc.incident_number,
                            "task_type": ESCALATION_TASK_TYPE,
                            "reason": detail,
                        },
                    )
                )

        action = "escalated" if escalate else "chased"
        results.append(
            ChaseResult(
                incident_id=inc.id,
                incident_number=inc.incident_number,
                action=action,
                detail=detail,
                note_written=note_written,
                task_created=task_created,
            )
        )
        if note_written or task_created:  # a fully deduped pass changed nothing: nothing to announce
            events.append(
                RealtimeEvent(
                    type="monitor.chase",
                    operator_id=cfg.operator_id,
                    incident_id=inc.id,
                    payload={
                        "incident_number": inc.incident_number,
                        "action": action,
                        "detail": detail,
                    },
                )
            )

    session.commit()
    for ev in events:  # after the commit: the UI is never told about a row the DB did not keep
        hub.publish_sync(ev)
    return results


def tick_job(session: Session, settings: AppSettings) -> JobResult:
    """The scheduler's ``monitor_tick`` job: one chase pass, reported as a run step."""
    results = chase_silent_incidents(session, settings.operator)
    notes = sum(1 for r in results if r.note_written)
    tasks = sum(1 for r in results if r.task_created)
    escalated = sum(1 for r in results if r.action == "escalated")
    return JobResult(
        summary=f"chased={len(results)} notes={notes} escalated={escalated} tasks={tasks}",
        rationale=(
            "Silence and SLA breaches chased at the note interval per priority/region multiplier; "
            "one chase note per window, next_update_at honoured"
        ),
        tools=(
            {
                "name": "flag_sla_watch",
                "ok": True,
                "chased": len(results),
                "notes": notes,
                "escalated": escalated,
                "tasks": tasks,
            },
        ),
    )
