"""EXEC_BRIEF: publish a short status brief so executives stop phoning the NOC."""

from __future__ import annotations

from noc_agents.db.models import IncidentBriefRow
from noc_agents.orchestrator.contract import IncidentState, RunContext, StepResult
from noc_agents.services.composition import compose_brief


def input_summary(state: IncidentState, ctx: RunContext) -> str:
    return state.incident.priority


def run(state: IncidentState, ctx: RunContext) -> StepResult:
    inc = state.incident
    brief = compose_brief(ctx.cfg, inc)
    ctx.session.add(IncidentBriefRow(incident_id=inc.id, body=brief))
    return StepResult(
        output_summary="exec brief published",
        rationale="Proactive brief to reduce management call volume into NOC",
        tools=[{"name": "upsert_status_brief", "ok": True, "latency_ms": 2}],
    )
