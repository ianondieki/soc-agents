"""INGEST: normalise the alarm into a correlation fingerprint."""

from __future__ import annotations

from noc_agents.orchestrator.contract import IncidentState, RunContext, StepResult
from noc_agents.services.fingerprint import build_fingerprint


def input_summary(state: IncidentState, ctx: RunContext) -> str:
    return f"site={state.event.site_id}"


def run(state: IncidentState, ctx: RunContext) -> StepResult:
    event = state.event
    state.fingerprint = build_fingerprint(event.site_id, event.alarm_code, event.failure_domain)
    return StepResult(
        output_summary=f"normalized event fingerprint={state.fingerprint}",
        rationale="Normalized alarm payload; ready for correlation window check",
        tools=[{"name": "normalize_event", "ok": True, "latency_ms": 1}],
    )
