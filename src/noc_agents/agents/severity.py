"""SEVERITY: P1-P4 from the priority engine (HUB floors, M-PESA corridor tag)."""

from __future__ import annotations

from noc_agents.orchestrator.contract import IncidentState, RunContext, StepResult
from noc_agents.services.priority import evaluate_severity


def input_summary(state: IncidentState, ctx: RunContext) -> str:
    return f"users={state.users}"


def run(state: IncidentState, ctx: RunContext) -> StepResult:
    event = state.event
    state.sev = evaluate_severity(
        users_affected=state.users,
        site_type=event.site_type,
        region_code=event.region_code,
        multi_region=event.multi_region,
        child_sites_down=event.child_sites_down,
        cfg=ctx.cfg,
    )
    return StepResult(
        output_summary=f"priority={state.sev.priority.value} mpesa_risk={state.sev.mpesa_risk}",
        rationale=state.sev.rationale,
        tools=[{"name": "priority_engine", "ok": True, "latency_ms": 1}],
        confidence=0.95,
    )
