"""The contract between the orchestrator and every lifecycle agent.

How an agent talks to the orchestrator, in plain words:

* The runner walks the workflow nodes in order. For each node it calls the agent
  module's ``input_summary(state, ctx)`` (a short string for the audit trail), opens
  a step row, then calls ``run(state, ctx)``.
* ``state`` (:class:`IncidentState`) is the shared scratchpad: an agent reads what
  earlier agents wrote and assigns its own fields directly. ``ctx`` (:class:`RunContext`)
  carries the dependencies the runner injects: the DB session, settings, the tracker.
* An agent returns a :class:`StepResult`. Its ``status`` tells the runner what to do
  next: ``SUCCEEDED`` / ``WAITING_HITL`` record the step and continue; ``SHORT_CIRCUIT``
  ends the run early with ``result.incident`` as the answer (merge / cascade).
* An agent never commits, never publishes realtime events and never touches the
  tracker except TICKET, which binds the new incident so later events carry its
  number. Raising an exception is how an agent reports failure; the runner decides
  whether that fails the run (fail-closed) or only the step (fail-soft) from the
  agent's profile in the registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from noc_agents.db.models import AgentRunRow, IncidentRow
from noc_agents.domain.schemas import EventIngest

if TYPE_CHECKING:  # typing only: keeps this module free of sqlalchemy/config/graph at runtime
    from sqlalchemy.orm import Session

    from noc_agents.config import AppSettings, OperatorConfig
    from noc_agents.graph.instrumentation import RunTracker
    from noc_agents.services.priority import SeverityResult
    from noc_agents.services.tt_classify import TTClassification

# Step status vocabulary (strings, because RunTracker and the DB store strings)
SUCCEEDED = "SUCCEEDED"  # normal completion
WAITING_HITL = "WAITING_HITL"  # recorded on the step, run continues (HITL / BROADCAST)
SHORT_CIRCUIT = "SHORT_CIRCUIT"  # merge/cascade: finish the run now, return result.incident
FAILED = "FAILED"  # fail-soft step raised; the runner records it and continues

# Agent criticality (AgentProfile.criticality)
FAIL_CLOSED = "fail_closed"  # an exception fails the whole run
FAIL_SOFT = "fail_soft"  # an exception fails only this step

# Tool-call entries are plain dicts built literally by each agent, e.g.
# {"name": "send_email", "ok": True, "latency_ms": 50, "error": None}. They are
# persisted as-is in tools_called_json, so there is deliberately no helper class.


@dataclass
class StepResult:
    status: str = SUCCEEDED  # SUCCEEDED | WAITING_HITL | SHORT_CIRCUIT | FAILED
    output_summary: str = ""
    rationale: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)  # exact tools_called entries
    confidence: float | None = 0.9  # RunTracker.complete_step default
    # SHORT_CIRCUIT only:
    incident: IncidentRow | None = None  # row to return from process_event
    event_type: str | None = None  # "incident.merged" | "incident.cascade_child"
    event_payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class IncidentState:
    """Everything one agent hands to the next. Agents assign fields directly."""

    event: EventIngest
    # INGEST
    fingerprint: str | None = None
    # ENRICH
    users: int | None = None
    site_name: str | None = None
    county: str | None = None
    is_hub: bool = False
    tt: TTClassification | None = None
    # SEVERITY
    sev: SeverityResult | None = None
    # TICKET
    incident: IncidentRow | None = None
    sla_ack_due: datetime | None = None
    sla_restore_due: datetime | None = None
    outage_start: datetime | None = None
    # HITL
    waiting_hitl: bool = False
    email_body: str | None = None
    sms_body: str | None = None


@dataclass
class RunContext:
    """Dependencies injected by the runner; never persisted."""

    session: Session
    settings: AppSettings
    tracker: RunTracker
    run: AgentRunRow
    llm: Any | None = None  # reserved for the optional LLM assist layer; the hot path never uses it

    @property
    def cfg(self) -> OperatorConfig:
        return self.settings.operator
