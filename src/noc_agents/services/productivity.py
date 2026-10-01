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
of the arithmetic. A profile key that names no lifecycle node is listed under
``assumptions.ignored_keys`` and logged, never silently dropped. The defaults below are the
floor's own estimates (``docs/MANAGER_DEMO.md`` says how they were arrived at); edit the
YAML with the floor, never this file.

**3. One operator's rows.** Every read of an operator-owned table goes through
``api.deps._operator_scoped`` — the one place the operator clause is built — exactly as the
regions dashboard does, and for the same reason: an aggregate carries no row id for a
reviewer to notice is foreign, so the other operator's work would simply be *added to ours*.

How it scales
-------------
Everything is aggregated in SQL: one row per run (to classify it), one row per
(node, agent, status) for the step counts and timings, one row per (priority, status) for
the incidents, and plain counts for the rest. No step row is loaded into Python and no
``IN (...)`` list is built, so a pilot database with a year of alarms costs the same handful
of queries as the demo's.

What a step is worth
--------------------
A step counts as done when it ended ``SUCCEEDED`` or ``WAITING_HITL``: a held broadcast is
still a drafted broadcast — the agent wrote the SMS and the e-mail, the human only reads and
approves them, and that reading is charged back as ``human_minutes.hitl_decision`` per
decided card. ``FAILED`` and still-``STARTED`` steps are worth nothing. A merge or cascade
run (``INGEST`` + ``CORRELATE`` only) is worth those two steps: the duplicate a human would
otherwise have ticketed twice.

How a run is classified
-----------------------
A run with a ``TICKET`` step **created** a ticket. A run that finished (``SUCCEEDED`` or
``WAITING_HITL``) without one was **absorbed** — a merge or a cascade child. A run that is
still ``RUNNING`` has decided nothing yet and is reported as ``in_flight``, never as a
duplicate. ``FAILED`` runs are counted on their own (the runner rolls back everything but
the run row and the failed step, so one never carries a ticket).

Timestamps are strings ending in ``Z`` at seconds precision, the dashboard's spelling
(``services.clock.iso_z``).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from functools import reduce
from operator import add
from statistics import median
from typing import Any, Sequence

from sqlalchemy import String, Text, and_, case, func, select
from sqlalchemy.orm import Session

from noc_agents.api.deps import _operator_scoped
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
from noc_agents.services.clock import iso_z
from noc_agents.services.hitl import OPEN_TASK_STATUSES

__all__ = [
    "AGENT_FILLED_FIELDS",
    "DEFAULT_HUMAN_MINUTES",
    "DEFAULT_TOIL_MINUTES",
    "DEFAULT_WINDOW_HOURS",
    "DONE_STEP_STATUSES",
    "FINISHED_RUN_STATUSES",
    "HUMAN_ACTIONS",
    "LIFECYCLE_GRAPH",
    "MAX_WINDOW_HOURS",
    "TOIL_MODEL_VERSION",
    "productivity",
    "toil_minutes_for",
    "human_minutes_for",
]

log = logging.getLogger("noc_agents.services.productivity")

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

#: The actions a human still performs that the model charges back, per occurrence.
HUMAN_ACTIONS: tuple[str, ...] = ("hitl_decision",)

#: Minutes a human still spends per action the agents hand back. Charged against the saving.
DEFAULT_HUMAN_MINUTES: dict[str, float] = {
    "hitl_decision": 2.0,  # read both renderings and the facts on an approval card, decide
}

#: Step statuses that count as work done (see "What a step is worth").
DONE_STEP_STATUSES: frozenset[str] = frozenset({"SUCCEEDED", "WAITING_HITL"})

#: Run statuses that mean the run has decided what it was going to decide.
FINISHED_RUN_STATUSES: frozenset[str] = frozenset({"SUCCEEDED", "WAITING_HITL"})

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
_BROADCAST_HELD = "PENDING_HITL"
_TERMINAL_INCIDENT = ("CLOSED", "CANCELLED")


# ----------------------------------------------------------------------------- config


def toil_minutes_for(cfg: OperatorConfig) -> dict[str, float]:
    """The toil model in force: the defaults, overridden key by key by the profile (keys are
    case-insensitive). Unknown keys are kept out of the model; ``productivity`` reports them."""
    model = dict(DEFAULT_TOIL_MINUTES)
    for key, value in cfg.productivity.toil_minutes.items():
        name = str(key).upper()
        if name in DEFAULT_TOIL_MINUTES:
            model[name] = float(value)
    return model


def human_minutes_for(cfg: OperatorConfig) -> dict[str, float]:
    model = dict(DEFAULT_HUMAN_MINUTES)
    for key, value in cfg.productivity.human_minutes.items():
        if str(key) in HUMAN_ACTIONS:
            model[str(key)] = float(value)
    return model


def ignored_keys_for(cfg: OperatorConfig) -> dict[str, list[str]]:
    """Profile keys that name no lifecycle node / no human action, so an operator who
    "corrected it with the floor" can see the override did nothing."""
    return {
        "toil_minutes": sorted(str(k) for k in cfg.productivity.toil_minutes if str(k).upper() not in DEFAULT_TOIL_MINUTES),
        "human_minutes": sorted(str(k) for k in cfg.productivity.human_minutes if str(k) not in HUMAN_ACTIONS),
    }


# ----------------------------------------------------------------------------- helpers


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


def _avg(total: Any, count: Any) -> float | None:
    return _round(float(total) / int(count), 1) if total is not None and count else None


def _later(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _filled_expr(name: str):
    """``1`` when the column holds a value a person would otherwise have typed, else ``0``."""
    column = getattr(IncidentRow, name)
    if isinstance(IncidentRow.__table__.c[name].type, (String, Text)):
        return case((and_(column.isnot(None), column != ""), 1), else_=0)
    return case((column.isnot(None), 1), else_=0)


_FILLED_FIELDS_EXPR = reduce(add, (_filled_expr(name) for name in AGENT_FILLED_FIELDS))


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
    ignored = ignored_keys_for(cfg)
    if ignored["toil_minutes"] or ignored["human_minutes"]:
        log.warning("productivity: profile %s names unknown keys %s", cfg.operator_id, ignored)

    # --- one row per lifecycle run: status, timing and whether it opened a ticket
    runs_stmt = (
        select(
            AgentRunRow.id,
            AgentRunRow.status,
            AgentRunRow.started_at,
            AgentRunRow.finished_at,
            func.max(case((AgentRunStepRow.node_name == "TICKET", 1), else_=0)).label("ticketed"),
        )
        .select_from(AgentRunRow)
        .outerjoin(AgentRunStepRow, AgentRunStepRow.run_id == AgentRunRow.id)
        .where(AgentRunRow.graph_name == LIFECYCLE_GRAPH)
        .group_by(AgentRunRow.id, AgentRunRow.status, AgentRunRow.started_at, AgentRunRow.finished_at)
    )
    runs_stmt = windowed(_operator_scoped(runs_stmt, AgentRunRow), AgentRunRow.started_at)
    alarms = created = absorbed = failed_runs = in_flight = 0
    pipeline_ms: list[int] = []
    for _run_id, status, started_at, finished_at, ticketed in session.execute(runs_stmt):
        alarms += 1
        if status == "FAILED":
            failed_runs += 1
        if ticketed:
            created += 1
            ms = _ms(started_at, finished_at)
            if ms is not None:
                pipeline_ms.append(ms)
        elif status in FINISHED_RUN_STATUSES:
            absorbed += 1
        elif status != "FAILED":
            in_flight += 1
    settled = created + absorbed
    noise_pct = _round(100.0 * absorbed / settled, 1) if settled else None
    ran = settled + in_flight

    # --- one row per (node, agent, status): counts and timings, in SQL
    steps_stmt = (
        select(
            AgentRunStepRow.node_name,
            AgentRunStepRow.agent_name,
            AgentRunStepRow.status,
            func.count().label("n"),
            func.sum(AgentRunStepRow.duration_ms).label("sum_ms"),
            func.count(AgentRunStepRow.duration_ms).label("n_ms"),
            func.max(AgentRunStepRow.duration_ms).label("max_ms"),
            func.max(AgentRunStepRow.finished_at).label("last_finished"),
            func.max(AgentRunStepRow.started_at).label("last_started"),
        )
        .select_from(AgentRunStepRow)
        .join(AgentRunRow, AgentRunRow.id == AgentRunStepRow.run_id)
        .where(AgentRunRow.graph_name == LIFECYCLE_GRAPH)
        .group_by(AgentRunStepRow.node_name, AgentRunStepRow.agent_name, AgentRunStepRow.status)
    )
    steps_stmt = windowed(_operator_scoped(steps_stmt, AgentRunRow), AgentRunRow.started_at)

    per_node: dict[str, dict[str, Any]] = {}
    by_node: list[dict[str, Any]] = []
    for card in NODE_CARDS:
        entry = {
            "node": card.node_id,
            "label": card.label,
            "agent": card.agent,
            "steps": 0,
            "succeeded": 0,
            "waiting_hitl": 0,
            "failed": 0,
            "_sum_ms": 0,
            "_n_ms": 0,
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
            "_sum_ms": 0,
            "_n_ms": 0,
            "_max_ms": None,
            "_last": None,
        }

    total = succeeded = waiting = failed = 0
    for node_name, agent_name, status, n, sum_ms, n_ms, max_ms, last_finished, last_started in session.execute(steps_stmt):
        n = int(n)
        total += n
        if status == "SUCCEEDED":
            succeeded += n
        elif status == "WAITING_HITL":
            waiting += n
        elif status == "FAILED":
            failed += n
        node = per_node.get(node_name)
        if node is not None:
            node["steps"] += n
            if status == "SUCCEEDED":
                node["succeeded"] += n
            elif status == "WAITING_HITL":
                node["waiting_hitl"] += n
            elif status == "FAILED":
                node["failed"] += n
            node["_sum_ms"] += int(sum_ms or 0)
            node["_n_ms"] += int(n_ms or 0)
        agent = per_agent.get(agent_name)
        if agent is not None:
            agent["steps"] += n
            if status in DONE_STEP_STATUSES:
                agent["succeeded"] += n
            elif status == "FAILED":
                agent["failed"] += n
            agent["_sum_ms"] += int(sum_ms or 0)
            agent["_n_ms"] += int(n_ms or 0)
            if max_ms is not None and (agent["_max_ms"] is None or int(max_ms) > agent["_max_ms"]):
                agent["_max_ms"] = int(max_ms)
            agent["_last"] = _later(agent["_last"], _later(last_finished, last_started))

    minutes_saved = 0.0
    for entry in by_node:
        entry["avg_ms"] = _avg(entry.pop("_sum_ms"), entry.pop("_n_ms"))
        done = entry["succeeded"] + entry["waiting_hitl"]
        entry["minutes_saved"] = _round(done * entry["toil_minutes_each"], 1)
        minutes_saved += done * entry["toil_minutes_each"]
    agents_out: list[dict[str, Any]] = []
    for p in AGENT_PROFILES:
        entry = per_agent[p.name]
        entry["avg_ms"] = _avg(entry.pop("_sum_ms"), entry.pop("_n_ms"))
        entry["max_ms"] = entry.pop("_max_ms")
        entry["last_step_at"] = iso_z(entry.pop("_last"))
        agents_out.append(entry)

    # --- incidents created in the window: one row per (priority, status)
    inc_stmt = select(
        IncidentRow.priority,
        IncidentRow.status,
        func.count().label("n"),
        func.sum(_FILLED_FIELDS_EXPR).label("filled"),
    ).group_by(IncidentRow.priority, IncidentRow.status)
    inc_stmt = windowed(_operator_scoped(inc_stmt, IncidentRow), IncidentRow.created_at)
    by_priority = {"P1": 0, "P2": 0, "P3": 0, "P4": 0}
    incidents = open_count = closed_count = 0
    fields_filled = 0
    for priority, status, n, filled in session.execute(inc_stmt):
        n = int(n)
        incidents += n
        by_priority[priority] = by_priority.get(priority, 0) + n
        if status in _TERMINAL_INCIDENT:
            closed_count += n
        else:
            open_count += n
        fields_filled += int(filled or 0)

    # --- the records that hang off those incidents
    bc_stmt = (
        select(BroadcastRow.status, BroadcastRow.channel, func.count().label("n"))
        .select_from(BroadcastRow)
        .join(IncidentRow, IncidentRow.id == BroadcastRow.incident_id)
        .group_by(BroadcastRow.status, BroadcastRow.channel)
    )
    bc_stmt = windowed(_operator_scoped(bc_stmt, IncidentRow), IncidentRow.created_at)
    bc_by_status: dict[str, int] = {}
    bc_by_channel: dict[str, int] = {}
    for status, channel, n in session.execute(bc_stmt):
        bc_by_status[status] = bc_by_status.get(status, 0) + int(n)
        bc_by_channel[channel] = bc_by_channel.get(channel, 0) + int(n)
    briefs = int(
        session.scalar(
            windowed(_operator_scoped(select(func.count()).select_from(IncidentBriefRow), IncidentBriefRow), IncidentRow.created_at)
        )
        or 0
    )
    ledger_rows = int(
        session.scalar(
            windowed(_operator_scoped(select(func.count()).select_from(ShiftLedgerRow), ShiftLedgerRow), ShiftLedgerRow.row_written_at)
        )
        or 0
    )
    problems_opened = int(
        session.scalar(windowed(_operator_scoped(select(func.count()).select_from(ProblemRow), ProblemRow), ProblemRow.first_seen)) or 0
    )

    # --- the approvals the agents asked for, and how long humans took
    task_stmt = windowed(
        _operator_scoped(select(HitlTaskRow.status, func.count().label("n")).group_by(HitlTaskRow.status), HitlTaskRow),
        HitlTaskRow.created_at,
    )
    by_task_status: dict[str, int] = {status: int(n) for status, n in session.execute(task_stmt)}
    raised = sum(by_task_status.values())
    approved = by_task_status.get("APPROVED", 0)
    rejected = by_task_status.get("REJECTED", 0)
    pending = sum(by_task_status.get(s, 0) for s in OPEN_TASK_STATUSES)
    decided_stmt = windowed(
        _operator_scoped(
            select(HitlTaskRow.created_at, HitlTaskRow.resolved_at).where(
                HitlTaskRow.status.in_(_HITL_DECIDED), HitlTaskRow.resolved_at.isnot(None)
            ),
            HitlTaskRow,
        ),
        HitlTaskRow.created_at,
    )
    decision_minutes = [(resolved - created_at).total_seconds() / 60.0 for created_at, resolved in session.execute(decided_stmt)]
    human_minutes = (approved + rejected) * float(human.get("hitl_decision", 0.0))

    return {
        "generated_at": iso_z(at),
        "operator_id": cfg.operator_id,
        "window_hours": hours,
        "since": iso_z(since),
        "alarms": {
            "processed": alarms,
            "incidents_created": created,
            "absorbed": absorbed,
            "in_flight": in_flight,
            "failed_runs": failed_runs,
            "noise_reduction_pct": noise_pct,
        },
        "incidents": {
            "created": incidents,
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
            "raised": raised,
            "pending": pending,
            "approved": approved,
            "rejected": rejected,
            "median_decision_minutes": _round(median(decision_minutes), 1) if decision_minutes else None,
        },
        "broadcasts": {
            "drafted": sum(bc_by_status.values()),
            "sent": bc_by_status.get("SENT", 0),
            "held_for_approval": bc_by_status.get(_BROADCAST_HELD, 0),
            "queued": bc_by_status.get("QUEUED", 0),
            "suppressed": bc_by_status.get("SUPPRESSED", 0),
            "failed": bc_by_status.get("FAILED", 0),
            "by_channel": dict(sorted(bc_by_channel.items())),
        },
        "ticket_fields": {
            "auto_filled": fields_filled,
            "per_incident": _round(fields_filled / incidents, 1) if incidents else None,
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
            # Per alarm that has run, settled or still in flight: an in-flight run's finished hops
            # are in the numerator, so the run belongs in the denominator too.
            "minutes_saved_per_alarm": _round(minutes_saved / ran, 1) if ran else None,
            "assumptions": {
                "toil_minutes": {c.node_id: float(toil.get(c.node_id, 0.0)) for c in NODE_CARDS},
                "human_minutes": dict(human),
                "ignored_keys": ignored,
                "note": (
                    "Minutes are the operator profile's estimate of a NOC analyst doing each step by "
                    "hand (productivity.toil_minutes), multiplied by the steps the agents completed; "
                    "they are a model to be corrected with the floor, not a measurement."
                ),
            },
        },
        "agents": agents_out,
    }
