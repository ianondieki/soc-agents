"""The HITL escalation ladder (spec §6.5; CONFORMANCE B-01): what happens when nobody clicks.

Every other gate in this system is a refusal -- a P1/P2 broadcast waits for a human, a
regulator notice waits for a named officer, a maintenance window waits for a supervisor.
This module is what makes refusing safe: when a P1/P2 approval card sits **PENDING and
unclaimed**, internal people are told that a decision is waiting, on a ladder whose rungs
come from the operator profile (``hitl.escalation``, ``config.HitlEscalationConfig``) and
default to the spec's own values:

* **T+5 min** -- an internal, pre-approved-by-policy nudge (``template_key=hitl_nudge``,
  audience ``NOC_SHIFT``, deterministic text, no incident narrative) to the on-duty
  supervisor, as an SMS (through the existing mock SMS path until the P3 adapter exists)
  and as an in-app message (a ``hitl.nudge`` realtime event to the shift's glass);
* **T+15 min** -- the same nudge to the duty manager, plus a ``hitl.escalated`` WS event
  carrying ``incident_number``, ``task_type`` and ``task_id`` (Appendix C);
* **T+30 min** -- the card is marked so the Wallboard shows it in red and the
  ``regulatory_sweep`` notes it on the incident's open CA 24-h card
  (``services/regulatory.note_hitl_escalations``).

THE ONE THING THIS MODULE MUST NEVER DO
---------------------------------------
Release, approve, reject or claim anything. The external broadcast is never auto-released
(D1: "external release is never automatic"). Read the writes: ``HITL_NUDGE`` outbox rows,
the ``escalation`` mark inside ``hitl_tasks.proposed_payload_json``, ``audit_events`` rows
and the buffered ``hitl.escalated`` event. There is no code path here that touches
``hitl_tasks.status``, ``claimed_by``, ``broadcasts`` or a channel-kind outbox row, and
``tests/unit/test_hitl_escalation.py`` proves the whole database says so after the ladder
has run every rung and the dispatcher has drained the nudges.

IDEMPOTENCY -- ONE NUDGE PER TASK PER RUNG, FOR EVER
----------------------------------------------------
Two independent guards, both durable, both in the same transaction:

1. **The outbox key.** A nudge row is keyed ``HITL_NUDGE:<task_id>:<rung minutes>:<channel>``
   -- no uuid, no clock -- and ``outbox.enqueue`` is INSERT OR IGNORE on that UNIQUE key.
   A restart, a re-run, or two schedulers cannot create a second row for the same rung.
2. **The compare-and-set mark.** Each rung a pass fires is recorded in
   ``proposed_payload_json["escalation"]["rungs"]``, written with one conditional UPDATE on
   the column's exact previous text AND ``status='PENDING' AND claimed_at IS NULL``. Only the
   writer whose UPDATE matched commits; a loser rolls back, which discards its outbox rows,
   its audit rows and its buffered event together (``realtime/commit_hook.py``). So the
   event fires once, the audit row exists once, and a card that was claimed or decided
   between the read and the write is never marked.

Work is committed **per task**, as the outbox drain commits per row, so a lost race on
one card cannot roll back another card's nudge.

WHAT "UNCLAIMED" MEANS, AND WHY A CLAIM STOPS THE LADDER
--------------------------------------------------------
``status == 'PENDING' and claimed_at IS NULL``. A claim is "this card is mine to decide"
(main.py ``hitl_claim``), and since round 4 only a role that may decide the card may claim
it -- so a claim means someone who can act is looking, which is exactly the condition the
ladder exists to bring about. Age is ``now - created_at``; a claim that is later released
does not exist in this codebase, so the ladder never restarts.

RECIPIENTS ARE REFS, AND THEY SHIP EMPTY
----------------------------------------
``hitl.recipients.supervisor`` / ``hitl.recipients.duty_manager`` are paths into the
profile's ``notification_recipients`` and both are empty lists in every shipped profile,
for the same reason ``regulatory.recipients.CA`` is: they are the operator's roster, not
public configuration. An SMS nudge with no configured recipient is still written to the
outbox -- the outbox is the ledger of every outbound side effect, refused ones included --
and closed ``SUPPRESSED`` in the same transaction with the reason, so it is never claimed,
never sent to an invented number, and visible. The in-app nudge needs no address (its
audience is the shift's glass) and still goes.

INCIDENT-LESS CARDS
-------------------
The spec's ladder is for "a P1/P2 task". Priority is the incident's; a card with no incident
(a maintenance window) has none and is **not on the ladder** unless its producer stamped a
``priority`` into ``proposed_payload`` -- then it rides the ladder like any other and the
``hitl.escalated`` event carries ``incident_number: null``. Both cases are pinned by tests.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from noc_agents.config import HitlEscalationConfig, OperatorConfig
from noc_agents.db.models import AuditRow, HitlTaskRow, IncidentRow, OutboxRow, get_session, utcnow
from noc_agents.orchestrator import outbox
from noc_agents.realtime.commit_hook import buffer_event
from noc_agents.realtime.hub import RealtimeEvent
from noc_agents.scheduler import JobCard, JobResult
from noc_agents.services.clock import fmt_eat, z_utc
from noc_agents.services.templates import (
    DEFAULT_LANGUAGE,
    HITL_NUDGE_PARAMS,
    HITL_NUDGE_TEMPLATE_KEY,
    TemplateError,
    TemplateRegistry,
)

if TYPE_CHECKING:  # typing only -- keeps the import graph of a flag-off process unchanged
    from noc_agents.config import AppSettings

log = logging.getLogger(__name__)

__all__ = [
    "ESCALATED_EVENT",
    "ESCALATION_KEY",
    "HITL_ESCALATION_ENABLED_ENV",
    "HITL_ESCALATION_JOB",
    "JOB_NAME",
    "LADDER_ACTOR",
    "NUDGE_APPROVER",
    "NUDGE_AUDIENCE",
    "NUDGE_EVENT",
    "NUDGE_INERT_PROVIDER",
    "TARGET_DUTY_MANAGER",
    "TARGET_SUPERVISOR",
    "TARGET_WALLBOARD",
    "EscalationReport",
    "Rung",
    "configured_recipients",
    "due_rungs",
    "escalate_once",
    "escalation_state",
    "hitl_escalation_enabled",
    "hitl_escalation_job",
    "is_unclaimed",
    "is_wallboard_red",
    "ladder",
    "nudge_context",
    "nudge_idempotency_key",
    "nudge_still_wanted",
    "record_nudge_outcome",
    "recorded_rungs",
    "task_priority",
    "task_subject",
    "unclaimed_minutes",
    "wallboard_red_tasks",
]

# --------------------------------------------------------------------------------- the flag

HITL_ESCALATION_ENABLED_ENV = "HITL_ESCALATION_ENABLED"
#: Spelled out again rather than imported, as every other lane does, so a flag-off process
#: does not import another lane to answer a question about this one.
_TRUE = frozenset({"1", "true", "yes", "on"})


def hitl_escalation_enabled() -> bool:
    """``HITL_ESCALATION_ENABLED`` -- default **false**. Only an explicit true arms the ladder.

    Read at call time, never cached: tests flip it with ``monkeypatch.setenv`` and an operator
    flips it in ``.env`` between runs.
    """
    return (os.getenv(HITL_ESCALATION_ENABLED_ENV) or "").strip().lower() in _TRUE


# ----------------------------------------------------------------------------- the vocabulary

JOB_NAME = "hitl_escalation"
#: ``hitl_tasks.proposed_payload_json[ESCALATION_KEY]`` -- the ladder's mark on the card. The
#: payload is already a bag the inbox card renders; the mark rides inside it so the existing
#: ``GET /api/v1/hitl/pending`` route (which returns ``proposed_payload``) is what feeds the
#: Wallboard's red state, with no new route.
ESCALATION_KEY = "escalation"
#: Appendix C: the T+15 event, carrying ``incident_number``, ``task_type`` and ``task_id``.
ESCALATED_EVENT = "hitl.escalated"
#: The in-app rendering's delivery: published by the dispatcher once the ``INAPP`` nudge row
#: is SENT (``orchestrator/outbox._finalize`` -> :func:`record_nudge_outcome`).
NUDGE_EVENT = "hitl.nudge"
#: §6.5: the nudge's audience is always the shift, never an external list.
NUDGE_AUDIENCE = "NOC_SHIFT"
#: "Pre-approved by policy": the approver stamped on every nudge row, in the same
#: principal-shaped convention as ``policy:L2_GUARDED`` on an auto-sent broadcast.
NUDGE_APPROVER = "policy:hitl_escalation"
#: The ladder's actor on audit rows. The SupervisorAgent owns the HITL node (§5.3.1), and this
#: is the HITL node's own follow-through; the agent-shaped string can never equal a human.
LADDER_ACTOR = "agent:SupervisorAgent"
#: ``audit_events.entity_type`` for everything the ladder writes about a card.
ENTITY_TYPE = "hitl_task"
#: ``outbox.provider`` for a nudge row that was dispatched but nudged nobody because the card
#: had been decided or claimed meanwhile -- the same convention as an LLM_CALL with the layer off.
NUDGE_INERT_PROVIDER = "none"

TARGET_SUPERVISOR = "supervisor"
TARGET_DUTY_MANAGER = "duty_manager"
TARGET_WALLBOARD = "wallboard"

#: The only channels a nudge may use (internal). ``HitlEscalationConfig`` refuses others.
NUDGE_CHANNELS: tuple[str, ...] = ("SMS", "INAPP")

#: Audit actions, one per rung.
AUDIT_NUDGED = "hitl.escalation.nudged"
AUDIT_ESCALATED = "hitl.escalated"
AUDIT_WALLBOARD_RED = "hitl.escalation.wallboard_red"
AUDIT_NUDGE_SENT = "hitl.nudge.sent"
AUDIT_NUDGE_INERT = "hitl.nudge.inert"
AUDIT_NUDGE_FAILED = "hitl.nudge.failed"


# ------------------------------------------------------------------------------ the ladder


@dataclass(frozen=True)
class Rung:
    """One step of the ladder, as data. Frozen: the ladder is configuration, not state."""

    minutes: int
    target: str  # supervisor | duty_manager | wallboard
    nudge: bool  # queue HITL_NUDGE rows to ``recipients_ref`` / the shift's glass
    event: bool  # publish ``hitl.escalated``
    wallboard_red: bool  # mark the card red for the Wallboard and the regulatory sweep
    recipients_ref: str | None = None

    @property
    def audit_action(self) -> str:
        if self.wallboard_red:
            return AUDIT_WALLBOARD_RED
        return AUDIT_ESCALATED if self.event else AUDIT_NUDGED


def ladder(cfg: OperatorConfig | HitlEscalationConfig) -> tuple[Rung, ...]:
    """The three §6.5 rungs from the profile, ascending. Pure."""
    esc = cfg.hitl.escalation if isinstance(cfg, OperatorConfig) else cfg
    return (
        Rung(esc.supervisor_minutes, TARGET_SUPERVISOR, nudge=True, event=False, wallboard_red=False, recipients_ref=esc.supervisor_recipients_ref),
        Rung(esc.duty_manager_minutes, TARGET_DUTY_MANAGER, nudge=True, event=True, wallboard_red=False, recipients_ref=esc.duty_manager_recipients_ref),
        Rung(esc.wallboard_minutes, TARGET_WALLBOARD, nudge=False, event=False, wallboard_red=True),
    )


# ------------------------------------------------------------------------ pure predicates


def escalation_state(task: HitlTaskRow | dict[str, Any]) -> dict[str, Any]:
    """The ladder's mark on a card (``{}`` when it has never been on the ladder)."""
    payload = task if isinstance(task, dict) else task.proposed_payload
    state = payload.get(ESCALATION_KEY)
    return dict(state) if isinstance(state, dict) else {}


def recorded_rungs(state: dict[str, Any]) -> set[int]:
    """The rung minutes already fired for this card."""
    out: set[int] = set()
    for key in (state.get("rungs") or {}):
        try:
            out.add(int(key))
        except (TypeError, ValueError):
            continue
    return out


def is_wallboard_red(state: dict[str, Any]) -> bool:
    return bool(state.get("wallboard_red"))


def is_unclaimed(task: HitlTaskRow) -> bool:
    """What the ladder keys on (§6.5 "unclaimed"): still PENDING, nobody's name on it."""
    return task.status == "PENDING" and task.claimed_at is None and not task.claimed_by


def unclaimed_minutes(task: HitlTaskRow, now: datetime) -> int:
    """Whole minutes since the card was created; never negative (a clock that ran backwards
    reads as a brand-new card, which is the side that nudges nobody by mistake)."""
    return max(0, int((now - task.created_at).total_seconds() // 60))


def task_priority(task: HitlTaskRow, inc: IncidentRow | None) -> str | None:
    """The incident's priority, or the priority a producer stamped on an incident-less card."""
    if inc is not None:
        return (inc.priority or "").upper() or None
    declared = task.proposed_payload.get("priority")
    return str(declared).upper() if declared else None


def task_subject(task: HitlTaskRow, inc: IncidentRow | None) -> str:
    """What the card is about, for the nudge text: the incident number, else the entity ref.
    Never a name, never narrative."""
    if inc is not None:
        return inc.incident_number
    return f"{task.entity_type or 'task'} {task.entity_id or task.id[:8]}".strip()


def due_rungs(task: HitlTaskRow, esc: HitlEscalationConfig, now: datetime, *, state: dict[str, Any] | None = None) -> list[Rung]:
    """The rungs this card has crossed and not yet recorded, ascending. Pure.

    A card first seen at T+45 (a long freeze, a scheduler that was off) returns all three:
    the supervisor AND the duty manager are both fetched and the card goes red in one pass.
    Each is still fired exactly once, because each is recorded.
    """
    if not is_unclaimed(task):
        return []
    age = unclaimed_minutes(task, now)
    done = recorded_rungs(state if state is not None else escalation_state(task))
    return [r for r in ladder(esc) if age >= r.minutes and r.minutes not in done]


def nudge_idempotency_key(task_id: str, minutes: int, channel: str) -> str:
    """``HITL_NUDGE:<task>:<rung>:<channel>`` -- deterministic, so a restart cannot double-nudge."""
    return f"{outbox.HITL_NUDGE}:{task_id}:{int(minutes)}:{channel.upper()}"


def nudge_context(task: HitlTaskRow, inc: IncidentRow | None, *, rung: Rung, unclaimed: int, priority: str) -> dict[str, Any]:
    """The render context, keys exactly ``templates.HITL_NUDGE_PARAMS`` (pinned by a test)."""
    return {
        "priority": priority,
        "task_type": task.task_type,
        "subject": task_subject(task, inc),
        "unclaimed_minutes": int(unclaimed),
        "escalation_target": rung.target,
    }


def configured_recipients(cfg: OperatorConfig, ref: str | None) -> list[str]:
    """The values under ``notification_recipients[ref]`` -- counted, never quoted, never
    written to a payload. Empty means "not configured" (the shipped state)."""
    if not ref:
        return []
    return [str(a).strip() for a in (cfg.notification_recipients or {}).get(ref) or [] if str(a).strip()]


# ------------------------------------------------------------------------------ the report


@dataclass
class EscalationReport:
    """What one ``hitl_escalation`` pass did."""

    enabled: bool = True
    checked: int = 0  # candidate cards examined
    not_on_ladder: int = 0  # candidates whose priority is not on the ladder (P3/P4, or none)
    escalated: list[dict[str, Any]] = field(default_factory=list)  # {task_id, rungs}
    nudges_queued: int = 0
    nudges_suppressed: int = 0
    events: int = 0  # hitl.escalated buffered
    lost: int = 0  # compare-and-set lost to a concurrent writer (a claim, a decision, another pass)

    def __str__(self) -> str:
        return (
            f"hitl escalation: checked={self.checked} escalated={len(self.escalated)} nudges_queued={self.nudges_queued} "
            f"nudges_suppressed={self.nudges_suppressed} events={self.events} not_on_ladder={self.not_on_ladder} lost={self.lost}"
        )


# ------------------------------------------------------------------------------- the pass


def _candidate_ids(session: Session, cfg: OperatorConfig, now: datetime) -> list[str]:
    """Cards that could be on the ladder: this operator's, PENDING, unclaimed, at least as old
    as the first rung. Ids only; each card is re-read inside its own unit of work."""
    first = min(r.minutes for r in ladder(cfg))
    return list(
        session.scalars(
            select(HitlTaskRow.id)
            .where(
                HitlTaskRow.operator_id == cfg.operator_id,
                HitlTaskRow.status == "PENDING",
                HitlTaskRow.claimed_at.is_(None),
                HitlTaskRow.created_at <= now - timedelta(minutes=first),
            )
            .order_by(HitlTaskRow.created_at.asc(), HitlTaskRow.id.asc())
        ).all()
    )


def _nudge_registry(session: Session, cfg: OperatorConfig) -> TemplateRegistry | TemplateError:
    """The operator's registry with ``hitl_nudge`` seeded, committed, or the error that stopped it.

    ``services/hitl.template_registry`` seeds only when the table is empty, so a database seeded
    before this template existed would resolve to "no template" for ever. ``sync`` is idempotent
    and versioned (§6.3): on a seeded database it is a pure read, and it never changes an
    existing row's approval.

    On the pass's OWN session, and committed at once, BEFORE any card is read (see
    ``escalate_once``): ``run_job`` has already flushed its run row on this session, so a second
    SQLite writer would wait on our own lock until the busy timeout ("database is locked"); and
    committed on its own so a lost compare-and-set later cannot un-seed the registry.

    No SAVEPOINT, deliberately: on pysqlite a ``begin_nested()`` opens a real deferred
    transaction whose reads hold SHARED while another writer's COMMIT needs EXCLUSIVE -- a
    deadlock the busy handler resolves with "database is locked" (which is why every
    insert-or-ignore in this codebase uses savepoints for other dialects only). Two passes
    racing on the seed are instead handled after the fact: the loser's INSERT fails the UNIQUE
    key once the winner commits, and that ``IntegrityError`` means "already seeded", so the
    session is rolled back and the rendering that follows reads the winner's rows. That path
    is reachable only from a direct call on a fresh session (a test, the demo script): under
    ``run_job`` the run row flushed before the job starts already holds SQLite's RESERVED lock,
    so two scheduled passes are serialized whole and the second one finds the rows seeded.

    A broken seed is returned, not raised: the ladder then records every nudge as SUPPRESSED
    with the reason (the same posture as ``services/hitl.resolve_templates``), because a nudge
    with invented text is worse than a visible gap.
    """
    try:
        registry = TemplateRegistry.for_config(session, cfg)
        if any(registry.latest(ch, HITL_NUDGE_TEMPLATE_KEY, DEFAULT_LANGUAGE) is None for ch in NUDGE_CHANNELS):
            try:
                registry.sync()
                session.commit()
            except IntegrityError:
                session.rollback()
                log.info("hitl escalation: message_templates was seeded by a concurrent pass; using its rows")
        return registry
    except TemplateError as exc:
        session.rollback()
        log.error("hitl escalation: template registry unavailable: %s", exc)
        return exc


def _render_nudge(registry: TemplateRegistry | TemplateError, channel: str, context: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """``(text, template_version, suppress_reason)``. A refused or unrenderable template is a
    reason, never a raise and never a made-up body."""
    if isinstance(registry, TemplateError):
        return None, None, f"no_approved_template: template registry unavailable: {registry}"
    try:
        rendered = registry.render_context(channel, HITL_NUDGE_TEMPLATE_KEY, context, language=DEFAULT_LANGUAGE)
    except TemplateError as exc:
        return None, None, f"no_approved_template: {exc}"[:2000]
    return rendered.body, rendered.template_version, None


def _iso(dt: datetime) -> str:
    stamped = z_utc(dt)
    return stamped.isoformat() if stamped is not None else ""


def _apply_rung(
    session: Session,
    *,
    task: HitlTaskRow,
    inc: IncidentRow | None,
    priority: str,
    rung: Rung,
    esc: HitlEscalationConfig,
    cfg: OperatorConfig,
    registry: TemplateRegistry | TemplateError,
    now: datetime,
    unclaimed: int,
    nudged_before: list[str],
    report: EscalationReport,
) -> dict[str, Any]:
    """Fire one rung for one card inside the caller's transaction; return its mark.

    Writes: the rung's ``HITL_NUDGE`` rows (one per channel; SMS closed SUPPRESSED when the
    ref is unconfigured or the template is not APPROVED), one audit row, and -- for the event
    rung -- the buffered ``hitl.escalated``. Nothing here touches the card's status.
    """
    incident_number = inc.incident_number if inc is not None else None
    record: dict[str, Any] = {
        "target": rung.target,
        "kind": "wallboard_red" if rung.wallboard_red else "nudge",
        "at": _iso(now),
        "at_eat": fmt_eat(now, "%Y-%m-%d %H:%M"),
        "unclaimed_minutes": unclaimed,
    }
    audit_payload: dict[str, Any] = {
        "task_id": task.id,
        "task_type": task.task_type,
        "incident_id": task.incident_id,
        "incident_number": incident_number,
        "priority": priority,
        "rung_minutes": rung.minutes,
        "unclaimed_minutes": unclaimed,
        "target": rung.target,
    }

    if rung.nudge:
        recipients = configured_recipients(cfg, rung.recipients_ref)
        context = nudge_context(task, inc, rung=rung, unclaimed=unclaimed, priority=priority)
        channels: dict[str, Any] = {}
        for channel in esc.channels:
            text, version, refused = _render_nudge(registry, channel, context)
            if refused is None and channel == "SMS" and not recipients:
                refused = (
                    f"no_recipient: notification_recipients.{rung.recipients_ref} is empty in "
                    f"config/operators/{cfg.operator_id}.yaml; nothing was sent and no number was invented"
                )
            key = nudge_idempotency_key(task.id, rung.minutes, channel)
            row = outbox.enqueue(
                session,
                kind=outbox.HITL_NUDGE,
                idempotency_key=key,
                payload={
                    "operator_id": cfg.operator_id,
                    "channel": channel,
                    "audience": NUDGE_AUDIENCE,
                    # A ref, never an address: the SMS half is resolved at dispatch by the adapter
                    # of the day; the in-app half is addressed to the shift's glass.
                    "recipients_ref": rung.recipients_ref if channel == "SMS" else f"audience:{NUDGE_AUDIENCE}",
                    "escalation_target": rung.target,
                    "rung_minutes": rung.minutes,
                    "unclaimed_minutes": unclaimed,
                    "task_id": task.id,
                    "task_type": task.task_type,
                    "incident_id": task.incident_id,
                    "incident_number": incident_number,
                    "priority": priority,
                    "message": text,
                    "template_key": HITL_NUDGE_TEMPLATE_KEY,
                    "template_version": version,
                    "language": DEFAULT_LANGUAGE,
                },
                incident_id=task.incident_id,
                run_id=task.run_id,
                hitl_task_id=task.id,
                requires_hitl=False,  # §6.5: pre-approved by policy; the approver is stamped below
                approved_by=NUDGE_APPROVER,
                approved_at=now,
                operator_id=cfg.operator_id,
            )
            if refused is not None:
                # Written and closed in the same transaction: the ledger keeps the refused
                # nudge, the drain never claims it, and the reason is on the row.
                session.execute(
                    update(OutboxRow)
                    .where(OutboxRow.id == row.id, OutboxRow.status == outbox.PENDING)
                    .values(status=outbox.SUPPRESSED, last_error=refused[:2000], updated_at=now)
                    .execution_options(synchronize_session=False)
                )
                status = outbox.SUPPRESSED
                report.nudges_suppressed += 1
            else:
                status = row.status
                report.nudges_queued += 1
            channels[channel] = {"outbox_id": row.id, "idempotency_key": key, "status": status, "reason": refused}
        record["recipients_ref"] = rung.recipients_ref
        record["recipients_configured"] = len(recipients)
        record["channels"] = channels
        audit_payload["channels"] = {ch: {"status": c["status"], "reason": c["reason"]} for ch, c in channels.items()}
        audit_payload["recipients_configured"] = len(recipients)

    if rung.event:
        nudged = [*nudged_before, rung.target]
        buffer_event(
            session,
            RealtimeEvent(
                type=ESCALATED_EVENT,
                operator_id=cfg.operator_id,
                incident_id=task.incident_id,
                run_id=task.run_id,
                payload={
                    # Appendix C: the three additive keys every hitl.* event carries.
                    "task_id": task.id,
                    "task_type": task.task_type,
                    "incident_number": incident_number,
                    "incident_id": task.incident_id,
                    "priority": priority,
                    "rung_minutes": rung.minutes,
                    "unclaimed_minutes": unclaimed,
                    "escalation_target": rung.target,
                    "nudged": nudged,
                    # ISO strings, never datetimes: the WS frames go through a bare json.dumps.
                    "created_at": _iso(task.created_at),
                    "created_at_eat": fmt_eat(task.created_at, "%Y-%m-%d %H:%M"),
                },
            ),
        )
        record["event"] = ESCALATED_EVENT
        report.events += 1

    if rung.wallboard_red:
        record["wallboard_red"] = True

    rationale = {
        TARGET_SUPERVISOR: f"{priority} {task.task_type} unclaimed {unclaimed} min: on-duty supervisor nudged (T+{rung.minutes})",
        TARGET_DUTY_MANAGER: f"{priority} {task.task_type} unclaimed {unclaimed} min: duty manager nudged and hitl.escalated published (T+{rung.minutes})",
        TARGET_WALLBOARD: f"{priority} {task.task_type} unclaimed {unclaimed} min: red on the Wallboard; regulatory_sweep notes it on the CA 24-h card (T+{rung.minutes})",
    }[rung.target]
    session.add(
        AuditRow(
            operator_id=cfg.operator_id,
            actor=LADDER_ACTOR,
            action=rung.audit_action,
            entity_type=ENTITY_TYPE,
            entity_id=task.id,
            rationale=rationale + "; nothing was released, approved, rejected or claimed",
            payload_json=json.dumps(audit_payload, sort_keys=True, default=str),
        )
    )
    return record


def escalate_once(session: Session, cfg: OperatorConfig, *, now: datetime | None = None) -> EscalationReport:
    """One pass of the ladder over this operator's PENDING, unclaimed cards. Commits per card.

    Writes nothing and publishes nothing when the flag is off. ``now`` is injectable so the
    rungs can be proved without waiting thirty minutes.
    """
    if not hitl_escalation_enabled():
        return EscalationReport(enabled=False)
    now = now or utcnow()
    esc = cfg.hitl.escalation
    report = EscalationReport()
    registry: TemplateRegistry | TemplateError | None = None

    for task_id in _candidate_ids(session, cfg, now):
        # The registry FIRST, before this card is read. Seeding may commit or roll back the
        # session (see _nudge_registry), and either would expire a card already loaded: its
        # ``before`` text would then be re-read fresh while ``due`` had been computed from the
        # stale read -- and a rung another pass had just recorded would fire again, with the
        # compare-and-set happily matching the fresh text. Read the card only once the session
        # is quiescent, so everything below comes from ONE consistent read.
        if registry is None:
            registry = _nudge_registry(session, cfg)
        task = session.get(HitlTaskRow, task_id)  # fresh read: this card's own unit of work
        if task is None or not is_unclaimed(task):
            continue
        report.checked += 1
        inc = session.get(IncidentRow, task.incident_id) if task.incident_id else None
        priority = task_priority(task, inc)
        if priority not in esc.priorities:
            report.not_on_ladder += 1
            continue
        state = escalation_state(task)
        due = due_rungs(task, esc, now, state=state)
        if not due:
            continue

        before = task.proposed_payload_json or "{}"
        data = json.loads(before)
        mark = dict(data.get(ESCALATION_KEY) or {})
        rungs = dict(mark.get("rungs") or {})
        unclaimed = unclaimed_minutes(task, now)
        counters = (report.nudges_queued, report.nudges_suppressed, report.events)  # restored if the CAS loses
        nudged_before = [r["target"] for r in rungs.values() if isinstance(r, dict) and r.get("kind") == "nudge"]
        fired: list[int] = []
        for rung in due:
            rungs[str(rung.minutes)] = _apply_rung(
                session,
                task=task,
                inc=inc,
                priority=priority,
                rung=rung,
                esc=esc,
                cfg=cfg,
                registry=registry,
                now=now,
                unclaimed=unclaimed,
                nudged_before=nudged_before,
                report=report,
            )
            if rung.nudge:
                nudged_before.append(rung.target)
            fired.append(rung.minutes)
        mark.update(
            ladder="hitl.escalation",
            rungs=rungs,
            level_minutes=max(recorded_rungs({"rungs": rungs}) or {0}),
            wallboard_red=any(isinstance(r, dict) and r.get("wallboard_red") for r in rungs.values()),
            updated_at=_iso(now),
        )
        red = next((r for r in rungs.values() if isinstance(r, dict) and r.get("wallboard_red")), None)
        if red is not None:
            mark["red_since"], mark["red_since_eat"] = red.get("at"), red.get("at_eat")
        data[ESCALATION_KEY] = mark
        after = json.dumps(data, default=str)

        # THE GATE. One conditional UPDATE on the exact previous text, still PENDING, still
        # unclaimed. The loser rolls back and its rows, audit and event go with it.
        won = session.execute(
            update(HitlTaskRow)
            .where(
                HitlTaskRow.id == task.id,
                HitlTaskRow.status == "PENDING",
                HitlTaskRow.claimed_at.is_(None),
                HitlTaskRow.proposed_payload_json == before,
            )
            .values(proposed_payload_json=after)
            .execution_options(synchronize_session=False)
        ).rowcount
        if won != 1:
            session.rollback()
            report.lost += 1
            report.nudges_queued, report.nudges_suppressed, report.events = counters  # the rows went with the rollback
            log.info("hitl escalation: card %s changed under the pass (claimed, decided or marked elsewhere); nothing written", task.id)
            continue
        session.commit()  # publishes the buffered hitl.escalated, if any, through the after-commit hook
        report.escalated.append({"task_id": task.id, "task_type": task.task_type, "incident_number": inc.incident_number if inc else None, "rungs": fired})
    return report


# ---------------------------------------------------------------------------- read side


def wallboard_red_tasks(session: Session, *, operator_id: str, incident_id: str | None = None) -> list[HitlTaskRow]:
    """This operator's PENDING, unclaimed cards the ladder has marked red (T+30), oldest first.

    The Wallboard reads the same fact from ``/api/v1/hitl/pending``'s ``proposed_payload``;
    this is the server-side reader for ``services/regulatory.note_hitl_escalations``. Still
    PENDING and unclaimed, on purpose: a card someone has since claimed is being looked at.
    """
    stmt = select(HitlTaskRow).where(
        HitlTaskRow.operator_id == operator_id,
        HitlTaskRow.status == "PENDING",
        HitlTaskRow.claimed_at.is_(None),
    )
    if incident_id is not None:
        stmt = stmt.where(HitlTaskRow.incident_id == incident_id)
    rows = session.scalars(stmt.order_by(HitlTaskRow.created_at.asc(), HitlTaskRow.id.asc())).all()
    return [t for t in rows if is_unclaimed(t) and is_wallboard_red(escalation_state(t))]


# ------------------------------------------------------------------------- dispatch side


def nudge_still_wanted(task_id: str | None, *, operator_id: str) -> tuple[bool, str]:
    """Asked by the ``HITL_NUDGE`` transmitter immediately before it nudges anyone.

    A nudge row can be dispatched long after it was queued (a freeze, a scheduler that was
    off, a dead-letter retry). If the card has been decided or claimed since, telling a
    supervisor that a decision is waiting is false, so the transmitter records the row as
    inert instead. Opens its own short session, as ``_transmit_llm_call`` does, because the
    drain deliberately holds no transaction while a transmitter runs. Never raises.
    """
    if not task_id:
        return False, "row names no hitl_task_id"
    session = get_session()
    try:
        task = session.get(HitlTaskRow, task_id)
        if task is None:
            return False, "task no longer exists"
        if task.operator_id != operator_id:
            return False, "task belongs to another operator"
        if task.status != "PENDING":
            return False, f"task is {task.status}"
        if task.claimed_at is not None or task.claimed_by:
            return False, "task has been claimed"  # no name: last_error is stored and exportable (§9.5)
        return True, ""
    except Exception as exc:  # noqa: BLE001 -- an unreadable database must not turn into a nudge about a decided card
        log.exception("hitl escalation: could not read task %s before nudging", task_id)
        return False, f"task state unreadable ({type(exc).__name__})"
    finally:
        session.close()


def record_nudge_outcome(
    session: Session, row: OutboxRow, *, result: outbox.DispatchResult, final_status: str, now: datetime
) -> list[RealtimeEvent]:
    """The dispatcher's terminal outcome for a ``HITL_NUDGE`` row: one audit row, and for a SENT
    in-app nudge the ``hitl.nudge`` event that IS its delivery to the shift's glass.

    Called from ``orchestrator/outbox._finalize`` inside the row's outcome transaction; the
    events are published by the drain after that commit, like every other channel's.
    """
    payload = json.loads(row.payload_json or "{}")
    mode = (result.delivery or {}).get("mode")
    inert = final_status == outbox.SENT and result.provider == NUDGE_INERT_PROVIDER
    if inert:
        action = AUDIT_NUDGE_INERT
        rationale = f"HITL_NUDGE {payload.get('channel')} row {row.id} dispatched but nudged nobody: {result.last_error}"
    elif final_status == outbox.SENT:
        action = AUDIT_NUDGE_SENT
        rationale = (
            f"HITL_NUDGE {payload.get('channel')} for {payload.get('task_type')} {payload.get('incident_number') or payload.get('task_id')} "
            f"(T+{payload.get('rung_minutes')}, {payload.get('escalation_target')}) delivered via {mode or result.provider}"
        )
    else:
        action = AUDIT_NUDGE_FAILED
        rationale = f"HITL_NUDGE {payload.get('channel')} row {row.id} ended {final_status}: {result.last_error}"
    session.add(
        AuditRow(
            operator_id=row.operator_id,
            actor=outbox.TRANSFER_ACTOR,
            action=action,
            entity_type=ENTITY_TYPE,
            entity_id=str(payload.get("task_id") or row.hitl_task_id or ""),
            rationale=rationale[:2000],
            payload_json=json.dumps(
                {
                    "outbox_id": row.id,
                    "channel": payload.get("channel"),
                    "task_id": payload.get("task_id"),
                    "task_type": payload.get("task_type"),
                    "incident_number": payload.get("incident_number"),
                    "rung_minutes": payload.get("rung_minutes"),
                    "escalation_target": payload.get("escalation_target"),
                    "outbox_status": final_status,
                    "provider": result.provider,
                    "mode": mode,
                    "error": result.last_error,
                },
                sort_keys=True,
                default=str,
            ),
        )
    )
    if final_status == outbox.SENT and not inert and str(payload.get("channel") or "").upper() == "INAPP":
        return [
            RealtimeEvent(
                type=NUDGE_EVENT,
                operator_id=row.operator_id,
                incident_id=row.incident_id,
                run_id=row.run_id,
                payload={
                    "task_id": payload.get("task_id"),
                    "task_type": payload.get("task_type"),
                    "incident_number": payload.get("incident_number"),
                    "incident_id": payload.get("incident_id"),
                    "priority": payload.get("priority"),
                    "rung_minutes": payload.get("rung_minutes"),
                    "unclaimed_minutes": payload.get("unclaimed_minutes"),
                    "escalation_target": payload.get("escalation_target"),
                    "audience": NUDGE_AUDIENCE,
                    "channel": "INAPP",
                    "text": payload.get("message"),
                    "outbox_id": row.id,
                },
            )
        ]
    return []


# ------------------------------------------------------------------------------ the job card


def hitl_escalation_job(session: Session, settings: "AppSettings") -> JobResult:
    """``hitl_escalation`` (every 60 s): one pass of the ladder. Commits per card.

    Re-checks its own flag, like every other job, because ``POST /api/v1/scheduler/run/{job}``
    calls this function directly and bypasses the loop's check (CONFORMANCE A-10). Off must
    say off: zero counts would read as "looked and found nothing", which is a different
    statement from "did not look".
    """
    from noc_agents.scheduler.loop import job_card, job_enabled  # lazy: the loop imports this module's card

    card = job_card(JOB_NAME)
    if card is not None and not job_enabled(card):
        return JobResult(
            summary=f"{JOB_NAME} skipped: {card.enabled_env} is off",
            rationale="The escalation ladder is switched off; no nudge queued, no card marked, no event published",
            tools=({"name": "hitl_escalation.escalate_once", "ok": True, "skipped": True, "reason": f"{card.enabled_env} is false"},),
        )
    report = escalate_once(session, settings.operator)
    return JobResult(
        summary=str(report) if report.enabled else f"{JOB_NAME} skipped: {HITL_ESCALATION_ENABLED_ENV} is off",
        rationale=(
            "P1/P2 approvals sitting PENDING and unclaimed were escalated on the §6.5 ladder: internal nudges as "
            "HITL_NUDGE outbox rows under deterministic keys, hitl.escalated at the duty-manager rung, red for the "
            "Wallboard at the last rung. Nothing was released, approved, rejected or claimed (D1)."
        ),
        tools=(
            {
                "name": "hitl_escalation.escalate_once",
                "ok": True,
                "enabled": report.enabled,
                "checked": report.checked,
                "escalated": len(report.escalated),
                "nudges_queued": report.nudges_queued,
                "nudges_suppressed": report.nudges_suppressed,
                "events": report.events,
                "not_on_ladder": report.not_on_ladder,
                "lost": report.lost,
            },
        ),
    )


#: Registered in ``scheduler/loop.SCHEDULED_JOBS`` through a lazy import (``_hitl_escalation_job``),
#: as every lane's card is. ``default_enabled=False`` so ``/scheduler/status`` reports the job OFF
#: while ``HITL_ESCALATION_ENABLED`` is unset, rather than claiming to run while the pass declines
#: to. Sixty seconds: the rungs are minute-granular, and a five-minute interval would land the
#: "T+5" nudge anywhere up to T+10.
HITL_ESCALATION_JOB = JobCard(
    JOB_NAME,
    60,
    hitl_escalation_job,
    HITL_ESCALATION_ENABLED_ENV,
    "SupervisorAgent",
    "hitl_escalation",
    default_enabled=False,
)

# The seed-time contract and the runtime context must agree; a test pins it too, but a
# mismatch here would fail every nudge under StrictUndefined, so it is asserted at import.
assert set(HITL_NUDGE_PARAMS) == {"priority", "task_type", "subject", "unclaimed_minutes", "escalation_target"}
