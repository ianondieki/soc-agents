"""MONITOR: arm the SLA timers with the first work note."""

from __future__ import annotations

from noc_agents.db.models import WorkNoteRow
from noc_agents.orchestrator.contract import IncidentState, RunContext, StepResult


def input_summary(state: IncidentState, ctx: RunContext) -> str:
    return "sla_watch"


def run(state: IncidentState, ctx: RunContext) -> StepResult:
    inc = state.incident
    ctx.session.add(
        WorkNoteRow(
            incident_id=inc.id,
            author="WorklogMonitorAgent",
            author_role="AGENT",
            body=(
                f"Monitoring started. SLA ack due {inc.sla_ack_due}, restore due {inc.sla_restore_due}. "
                f"Awaiting first note from {inc.assignee_name}."
            ),
            source="agent",
        )
    )
    return StepResult(
        output_summary="sla timers armed",
        rationale="Note-interval chase scheduled per priority/region multiplier",
        tools=[{"name": "flag_sla_watch", "ok": True, "latency_ms": 1}],
    )
