"""ASSIGN: pick the owner (field engineer vs MSP) from the dispatch matrix."""

from __future__ import annotations

from noc_agents.db.models import utcnow
from noc_agents.domain.enums import IncidentStatus
from noc_agents.orchestrator.contract import IncidentState, RunContext, StepResult
from noc_agents.services.assignment import assign


def input_summary(state: IncidentState, ctx: RunContext) -> str:
    return state.event.failure_domain


def run(state: IncidentState, ctx: RunContext) -> StepResult:
    event, inc = state.event, state.incident
    asg = assign(
        failure_domain=event.failure_domain,
        site_type=event.site_type,
        region_code=event.region_code,
        cfg=ctx.cfg,
        alarm_code=event.alarm_code,
    )
    now_esc = utcnow()
    inc.assignee_type = asg.assignee_type.value
    inc.assignee_name = asg.assignee_name
    inc.msp_name = asg.msp_name
    inc.responsible_msp = asg.msp_name or asg.responsible_party
    inc.fe_name = asg.fe_name
    inc.rnio_name = asg.rnio_name
    inc.radio_oem = asg.radio_oem
    inc.assignment_rationale = asg.rationale
    inc.escalated_at = now_esc  # time escalated to MSP / FE
    inc.failure_time = state.outage_start
    inc.expected_resolution_at = state.sla_restore_due
    inc.status = (
        IncidentStatus.AWAITING_VENDOR.value
        if asg.assignee_type.value == "MSP"
        else IncidentStatus.ASSIGNED.value
    )
    return StepResult(
        output_summary=f"{asg.assignee_type.value}:{asg.responsible_party}",
        rationale=asg.rationale,
        tools=[{"name": "assign_incident", "ok": True, "latency_ms": 2}],
    )
