"""Productivity rollup: what the agents did in a window, and what it would have cost by hand.

Why this exists
---------------
The question a manager asks of a multi-agent system is not "does it run" but "what did it
take off the floor". The lifecycle already records every answer to that question — a run
row per alarm, a step row per agent, the ticket fields it filled, the broadcasts it
drafted, the approvals it asked for, the ledger rows and briefs it wrote — but spread over
seven tables that nobody adds up. ``GET /api/v1/metrics/productivity`` adds them up.

Three rules
-----------
**1. Every count is derived from rows the lifecycle already writes.** ``agent_runs``,
``agent_run_steps``, ``incidents``, ``hitl_tasks``, ``broadcasts``, ``shift_ledger``,
``incident_briefs`` and ``problems``. Nothing is counted that was not recorded, and the
rollup writes nothing.

**2. The minutes are a MODEL, not a measurement.** ``productivity.toil_minutes`` in the
operator profile says how long a NOC analyst spends doing each step by hand; the endpoint
multiplies that by the steps the agents completed and reports the inputs next to every
number it derives (``toil.assumptions``), so a reader can disagree with the inputs instead
of the arithmetic. The defaults below are the floor's own estimates (``docs/MANAGER_DEMO.md``
says how they were arrived at); edit the YAML with the floor, never this file.

**3. One operator's rows.** Every read of an operator-owned table goes through
``api.deps._owned`` — the one place the operator clause is built — exactly as the regions
dashboard does, and for the same reason: an aggregate carries no row id for a reviewer to
notice is foreign, so the other operator's work would simply be *added to ours*.

What a step is worth
--------------------
A step counts as done when it ended ``SUCCEEDED`` or ``WAITING_HITL``: a held broadcast is
still a drafted broadcast — the agent wrote the SMS and the e-mail, the human only reads and
approves them, and that reading is charged back as ``human_minutes.hitl_decision`` per
decided card. ``FAILED`` and still-``STARTED`` steps are worth nothing. A merge or cascade
run (``INGEST`` + ``CORRELATE`` only) is worth those two steps: the duplicate a human would
otherwise have ticketed twice.

Timestamps are strings ending in ``Z`` at seconds precision, the dashboard's spelling.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from noc_agents.api.deps import _owned
from noc_agents.config import OperatorConfig
from noc_agents.db.models import (
    AgentRunRow,
    AgentRunStepRow,
    BroadcastRow,
    HitlTaskRow,
    IncidentBriefRow,
    IncidentRow,
    ProblemRow,
    ShiftLedgerRow,
    utcnow,
)

__all__ = [
    "AGENT_FILLED_FIELDS",
    "DEFAULT_HUMAN_MINUTES",
    "DEFAULT_TOIL_MINUTES",
    "DEFAULT_WINDOW_HOURS",
    "DONE_STEP_STATUSES",
    "LIFECYCLE_GRAPH",
    "MAX_WINDOW_HOURS",
    "TOIL_MODEL_VERSION",
    "productivity",
    "toil_minutes_for",
    "human_minutes_for",
]

#: ``agent_runs.graph_name`` of the 12-node lifecycle (``orchestrator.runner.GRAPH_NAME``;
#: pinned equal by the test so the two literals cannot drift). A local literal because the
#: runner imports every agent and a service must not pull that tree in at import time.
LIFECYCLE_GRAPH = "incident_lifecycle"

#: Bump when the meaning of a derived number changes, so a report from last month is not
#: compared with one from this month by accident.
TOIL_MODEL_VERSION = 1

DEFAULT_WINDOW_HOURS = 24
MAX_WINDOW_HOURS = 24 * 366  # a year; ``0`` means "everything on record"

#: Minutes a NOC analyst spends doing each lifecycle step by hand, per alarm. ESTIMATES —
#: see the module docstring. A profile's ``productivity.toil_minutes`` overrides any key.
DEFAULT_TOIL_MINUTES: dict[str, float] = {
    "INGEST": 1.0,  # read the alarm; work out the site, the domain and the technology
    "CORRELATE": 3.0,  # look for an open ticket or a parent HUB before raising a duplicate
    "ENRICH": 4.0,  # CMDB lookup, region and RNIO, estimate the subscribers affected
    "SEVERITY": 2.0,  # apply the P1–P4 thresholds and the HUB/CORE floors
    "TICKET": 8.0,  # allocate the number and fill the TT fields and narrative in the ticket UI
    "ASSIGN": 3.0,  # the region × domain MSP matrix, the FE on call, the escalation stamp
    "HITL": 0.0,  # the decision stays human and is charged back below, not saved
    "BROADCAST": 6.0,  # draft and address the RNIO / FE / MSP SMS and e-mail
    "EXEC_BRIEF": 10.0,  # write the status brief that stops the phone calls into the NOC
    "LEDGER": 3.0,  # the Excel shift ledger row
    "RECURRENCE": 5.0,  # check the site's history and open or update a problem record
    "MONITOR": 2.0,  # set the note-chase and SLA reminders
}

#: Minutes a human still spends per action the agents hand back. Charged against the saving.
DEFAULT_HUMAN_MINUTES: dict[str, float] = {
    "hitl_decision": 2.0,  # read both renderings and the facts on an approval card, decide
}

#: Step statuses that count as work done (see "What a step is worth").
DONE_STEP_STATUSES: frozenset[str] = frozenset({"SUCCEEDED", "WAITING_HITL"})

#: ``incidents`` columns the agents fill that a NOC analyst would otherwise type into the
#: ticketing UI. Counted non-empty per incident created in the window.
AGENT_FILLED_FIELDS: tuple[str, ...] = (
    "incident_number",
    "title",
    "narrative",
    "root_cause_hypothesis",
    "impact_summary",
    "priority",
    "severity_rationale",
    "users_affected",
    "tt_category",
    "tt_category_label",
    "symptom_code",
    "site_class",
    "technology",
    "network_element",
    "sla_ack_due",
    "sla_restore_due",
    "expected_resolution_at",
    "assignee_name",
    "responsible_msp",
    "fe_name",
    "rnio_name",
    "assignment_rationale",
    "escalated_at",
    "failure_time",
)

_HITL_DECIDED = ("APPROVED", "REJECTED")
_HITL_PENDING = ("PENDING", "CLAIMED")
_BROADCAST_HELD = "PENDING_HITL"


# ----------------------------------------------------------------------------- config


def toil_minutes_for(cfg: OperatorConfig) -> dict[str, float]:
    """The toil model in force: the defaults, overridden key by key by the profile."""
    model = dict(DEFAULT_TOIL_MINUTES)
    for key, value in cfg.productivity.toil_minutes.items():
        model[str(key).upper()] = float(value)
    return model


def human_minutes_for(cfg: OperatorConfig) -> dict[str, float]:
    model = dict(DEFAULT_HUMAN_MINUTES)
    for key, value in cfg.productivity.human_minutes.items():
        model[str(key)] = float(value)
    return model


# ----------------------------------------------------------------------------- helpers


def _z(dt: datetime | None) -> str | None:
    return None if dt is None else dt.replace(microsecond=0).isoformat() + "Z"


def _ms(a: datetime | None, b: datetime | None) -> int | None:
    if a is None or b is None:
        return None
    return max(0, int((b - a).total_seconds() * 1000))


def _percentile(values: Sequence[int | float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * pct)))
    return float(ordered[idx])


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def _filled(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    return True


# ----------------------------------------------------------------------------- rollup


def productivity(
    session: Session,
    cfg: OperatorConfig,
    *,
    now: datetime | None = None,
    window_hours: int = DEFAULT_WINDOW_HOURS,
) -> dict[str, Any]:
    """The rollup for ``window_hours`` back from ``now`` (``0`` = everything on record).

    Read-only. The response shape is pinned by ``tests/unit/test_productivity.py``: adding a
    key is fine, renaming or removing one is a breaking change for the Showcase page.
    """
    from noc_agents.orchestrator.registry import AGENT_PROFILES, NODE_CARDS  # lazy: pulls in every agent

    at = now or utcnow()
    hours = max(0, min(int(window_hours), MAX_WINDOW_HOURS))
    since = at - timedelta(hours=hours) if hours > 0 else None

    def windowed(stmt, column):
        return stmt if since is None else stmt.where(column >= since)

    toil = toil_minutes_for(cfg)
    human = human_minutes_for(cfg)

    # --- runs and steps: the lifecycle graph only, so an assist or monitor run is not an alarm
    runs = session.scalars(
        windowed(_owned(AgentRunRow).where(AgentRunRow.graph_name == LIFECYCLE_GRAPH), AgentRunRow.started_at)
    ).all()
    run_ids = [r.id for r in runs]
    steps: list[AgentRunStepRow] = []
    if run_ids:
        steps = session.scalars(
            select(AgentRunStepRow).where(AgentRunStepRow.run_id.in_(run_ids)).order_by(AgentRunStepRow.seq)
        ).all()
    steps_by_run: dict[str, list[AgentRunStepRow]] = {}
    for s in steps:
        steps_by_run.setdefault(s.run_id, []).append(s)

    created_runs = 0  # runs that opened a ticket
    absorbed_runs = 0  # merge / cascade short-circuits
    failed_runs = 0
    pipeline_ms: list[int] = []
    for r in runs:
        nodes = {s.node_name for s in steps_by_run.get(r.id, ())}
        if r.status == "FAILED":
            failed_runs += 1
            continue
        if "TICKET" in nodes:
            created_runs += 1
            ms = _ms(r.started_at, r.finished_at)
            if ms is not None:
                pipeline_ms.append(ms)
        else:
            absorbed_runs += 1
    alarms = len(runs)
    noise_pct = _round(100.0 * absorbed_runs / alarms, 1) if alarms else None

    # --- per node (registry order) and per agent (catalog order)
    by_node: list[dict[str, Any]] = []
    per_node: dict[str, dict[str, Any]] = {}
    for card in NODE_CARDS:
        entry = {
            "node": card.node_id,
            "label": card.label,
            "agent": card.agent,
            "steps": 0,
            "succeeded": 0,
            "waiting_hitl": 0,
            "failed": 0,
            "durations": [],
            "toil_minutes_each": float(toil.get(card.node_id, 0.0)),
        }
        per_node[card.node_id] = entry
        by_node.append(entry)
    per_agent: dict[str, dict[str, Any]] = {}
    for p in AGENT_PROFILES:
        per_agent[p.name] = {
            "name": p.name,
            "mission": p.mission,
            "nodes": [c.node_id for c in NODE_CARDS if c.agent == p.name],
            "steps": 0,
            "succeeded": 0,
            "failed": 0,
            "durations": [],
            "last_step_at": None,
        }

    total = succeeded = waiting = failed = 0
    minutes_saved = 0.0
    for s in steps:
        total += 1
        node = per_node.get(s.node_name)
        agent = per_agent.get(s.agent_name)
        done = s.status in DONE_STEP_STATUSES
        if s.status == "SUCCEEDED":
            succeeded += 1
        elif s.status == "WAITING_HITL":
            waiting += 1
        elif s.status == "FAILED":
            failed += 1
        if node is not None:
            node["steps"] += 1
            if s.status == "SUCCEEDED":
                node["succeeded"] += 1
            elif s.status == "WAITING_HITL":
                node["waiting_hitl"] += 1
            elif s.status == "FAILED":
                node["failed"] += 1
            if s.duration_ms is not None:
                node["durations"].append(s.duration_ms)
            if done:
                minutes_saved += node["toil_minutes_each"]
        if agent is not None:
            agent["steps"] += 1
            if done:
                agent["succeeded"] += 1
            elif s.status == "FAILED":
                agent["failed"] += 1
            if s.duration_ms is not None:
                agent["durations"].append(s.duration_ms)
            finished = s.finished_at or s.started_at
            if finished is not None and (agent["last_step_at"] is None or finished > agent["last_step_at"]):
                agent["last_step_at"] = finished

    for entry in by_node:
        durations = entry.pop("durations")
        entry["avg_ms"] = _round(sum(durations) / len(durations), 1) if durations else None
        entry["minutes_saved"] = _round((entry["succeeded"] + entry["waiting_hitl"]) * entry["toil_minutes_each"], 1)
    agents_out: list[dict[str, Any]] = []
    for p in AGENT_PROFILES:
        entry = per_agent[p.name]
        durations = entry.pop("durations")
        entry["avg_ms"] = _round(sum(durations) / len(durations), 1) if durations else None
        entry["max_ms"] = max(durations) if durations else None
        entry["last_step_at"] = _z(entry["last_step_at"])
        agents_out.append(entry)

    # --- incidents created in the window and the records that hang off them
    incidents = session.scalars(windowed(_owned(IncidentRow), IncidentRow.created_at)).all()
    inc_ids = [i.id for i in incidents]
    by_priority = {"P1": 0, "P2": 0, "P3": 0, "P4": 0}
    open_count = closed_count = 0
    fields_filled = 0
    for i in incidents:
        by_priority[i.priority] = by_priority.get(i.priority, 0) + 1
        if i.status in ("CLOSED", "CANCELLED"):
            closed_count += 1
        else:
            open_count += 1
        fields_filled += sum(1 for f in AGENT_FILLED_FIELDS if _filled(getattr(i, f, None)))

    broadcasts: list[BroadcastRow] = []
    briefs = 0
    if inc_ids:
        broadcasts = session.scalars(select(BroadcastRow).where(BroadcastRow.incident_id.in_(inc_ids))).all()
        briefs = len(session.scalars(_owned(IncidentBriefRow).where(IncidentBriefRow.incident_id.in_(inc_ids))).all())
    bc_by_status: dict[str, int] = {}
    bc_by_channel: dict[str, int] = {}
    for b in broadcasts:
        bc_by_status[b.status] = bc_by_status.get(b.status, 0) + 1
        bc_by_channel[b.channel] = bc_by_channel.get(b.channel, 0) + 1

    ledger_rows = len(session.scalars(windowed(_owned(ShiftLedgerRow), ShiftLedgerRow.row_written_at)).all())
    problems_opened = len(session.scalars(windowed(_owned(ProblemRow), ProblemRow.first_seen)).all())

    # --- the approvals the agents asked for, and how long humans took
    tasks = session.scalars(windowed(_owned(HitlTaskRow), HitlTaskRow.created_at)).all()
    approved = sum(1 for t in tasks if t.status == "APPROVED")
    rejected = sum(1 for t in tasks if t.status == "REJECTED")
    pending = sum(1 for t in tasks if t.status in _HITL_PENDING)
    decision_minutes = [
        (t.resolved_at - t.created_at).total_seconds() / 60.0
        for t in tasks
        if t.status in _HITL_DECIDED and t.resolved_at is not None and t.created_at is not None
    ]
    human_minutes = (approved + rejected) * float(human.get("hitl_decision", 0.0))

    processed = created_runs + absorbed_runs
    return {
        "generated_at": _z(at),
        "operator_id": cfg.operator_id,
        "window_hours": hours,
        "since": _z(since),
        "alarms": {
            "processed": alarms,
            "incidents_created": created_runs,
            "absorbed": absorbed_runs,
            "failed_runs": failed_runs,
            "noise_reduction_pct": noise_pct,
        },
        "incidents": {
            "created": len(incidents),
            "open": open_count,
            "closed": closed_count,
            "by_priority": by_priority,
        },
        "steps": {
            "total": total,
            "succeeded": succeeded,
            "waiting_hitl": waiting,
            "failed": failed,
            "by_node": by_node,
        },
        "pipeline_ms": {
            "runs_measured": len(pipeline_ms),
            "median": _round(median(pipeline_ms), 1) if pipeline_ms else None,
            "p95": _percentile(pipeline_ms, 0.95),
            "max": max(pipeline_ms) if pipeline_ms else None,
        },
        "hitl": {
            "raised": len(tasks),
            "pending": pending,
            "approved": approved,
            "rejected": rejected,
            "median_decision_minutes": _round(median(decision_minutes), 1) if decision_minutes else None,
        },
        "broadcasts": {
            "drafted": len(broadcasts),
            "sent": bc_by_status.get("SENT", 0),
            "held_for_approval": bc_by_status.get(_BROADCAST_HELD, 0),
            "queued": bc_by_status.get("QUEUED", 0),
            "suppressed": bc_by_status.get("SUPPRESSED", 0),
            "failed": bc_by_status.get("FAILED", 0),
            "by_channel": dict(sorted(bc_by_channel.items())),
        },
        "ticket_fields": {
            "auto_filled": fields_filled,
            "per_incident": _round(fields_filled / len(incidents), 1) if incidents else None,
            "tracked": list(AGENT_FILLED_FIELDS),
        },
        "records": {
            "ledger_rows": ledger_rows,
            "exec_briefs": briefs,
            "problems_opened": problems_opened,
        },
        "toil": {
            "model_version": TOIL_MODEL_VERSION,
            "minutes_saved": _round(minutes_saved, 1),
            "hours_saved": _round(minutes_saved / 60.0, 2),
            "human_minutes_spent": _round(human_minutes, 1),
            "net_minutes_saved": _round(minutes_saved - human_minutes, 1),
            "minutes_saved_per_alarm": _round(minutes_saved / processed, 1) if processed else None,
            "assumptions": {
                "toil_minutes": {c.node_id: float(toil.get(c.node_id, 0.0)) for c in NODE_CARDS},
                "human_minutes": dict(human),
                "note": (
                    "Minutes are the operator profile's estimate of a NOC analyst doing each step by "
                    "hand (productivity.toil_minutes), multiplied by the steps the agents completed; "
                    "they are a model to be corrected with the floor, not a measurement."
                ),
            },
        },
        "agents": agents_out,
    }
