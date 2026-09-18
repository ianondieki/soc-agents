from __future__ import annotations

from datetime import datetime
from typing import Any

from noc_agents.db.models import AgentRunRow, AgentRunStepRow, IncidentRow
from noc_agents.domain.schemas import AgentRunOut, AgentStepOut, IncidentOut
from noc_agents.services.clock import z_utc


def _z(**values: Any) -> dict[str, Any]:
    """Stamp every outgoing timestamp with an explicit ``Z`` (spec §7.0.6, defect #41).

    The DB stores **naive UTC** and that contract is untouched — nothing is converted
    here, only labelled. A naive ISO string like ``"2026-09-16T09:00:00"`` is read by a
    browser as *local* time, which in Nairobi silently backdates every incident by three
    hours; ``"2026-09-16T09:00:00Z"`` cannot be misread.

    Applied by ``isinstance`` rather than by a hand-kept field list, so a timestamp added
    to a schema later is labelled automatically and cannot regress this defect. ``date``
    values are unaffected (``date`` is the base class of ``datetime``, not a subclass).
    """
    return {k: (z_utc(v) if isinstance(v, datetime) else v) for k, v in values.items()}


class IncidentOutWithProvenance(IncidentOut):
    """``IncidentOut`` plus the restore-provenance columns (spec §7.0.8).

    Declared here rather than on ``IncidentOut`` itself so this wave touches one
    file: it is a strict superset, every existing key keeps its name, type and
    position, and ``isinstance(x, IncidentOut)`` still holds for every caller.
    Fold the two fields into ``domain.schemas.IncidentOut`` whenever that module
    is next opened and delete this class.

    ``restored_at`` alone has always been on the wire; without these two the API
    could tell a client *when* a ticket was restored but never whether that came
    from a human or from a regex over a vendor SMS.
    """

    restored_source: str | None = None
    restored_by: str | None = None


def incident_out(row: IncidentRow) -> IncidentOut:
    return IncidentOutWithProvenance(
        **_z(
        id=row.id,
        operator_id=row.operator_id,
        incident_number=row.incident_number,
        status=row.status,
        priority=row.priority,
        users_affected=row.users_affected,
        service_affecting=row.service_affecting,
        services_impacted=row.services_impacted,
        site_id=row.site_id,
        site_name=row.site_name,
        site_type=row.site_type,
        region_code=row.region_code,
        county=row.county,
        title=row.title,
        description=row.description,
        narrative=row.narrative,
        root_cause_hypothesis=row.root_cause_hypothesis,
        impact_summary=row.impact_summary,
        assignee_type=row.assignee_type,
        assignee_name=row.assignee_name,
        msp_name=row.msp_name,
        fe_name=row.fe_name,
        rnio_name=getattr(row, "rnio_name", None),
        access_notes=row.access_notes,
        created_at=row.created_at,
        updated_at=row.updated_at,
        is_hub_major=row.is_hub_major,
        recurrence_count=row.recurrence_count,
        problem_id=row.problem_id,
        mpesa_risk=row.mpesa_risk,
        autonomy_level_applied=row.autonomy_level_applied,
        requires_hitl=row.requires_hitl,
        hitl_state=row.hitl_state,
        failure_domain=row.failure_domain,
        correlation_fingerprint=row.correlation_fingerprint,
        outage_start_at=getattr(row, "outage_start_at", None),
        technology=getattr(row, "technology", None) or "4G",
        tt_category=getattr(row, "tt_category", None) or "OTHER",
        tt_category_label=getattr(row, "tt_category_label", None) or "",
        symptom_code=getattr(row, "symptom_code", None) or "",
        site_class=getattr(row, "site_class", None) or "STANDARD",
        network_element=getattr(row, "network_element", None) or "",
        parent_hub_id=getattr(row, "parent_hub_id", None),
        parent_incident_id=getattr(row, "parent_incident_id", None),
        vendor_tt_ref=getattr(row, "vendor_tt_ref", None),
        battery_countdown_min=getattr(row, "battery_countdown_min", None),
        child_sites_down=getattr(row, "child_sites_down", 0) or 0,
        acknowledged_at=getattr(row, "acknowledged_at", None),
        first_vendor_note_at=getattr(row, "first_vendor_note_at", None),
        restored_at=getattr(row, "restored_at", None),
        restored_source=getattr(row, "restored_source", None),
        restored_by=getattr(row, "restored_by", None),
        closed_at=row.closed_at,
        resolution_code=getattr(row, "resolution_code", None),
        resolution_summary=getattr(row, "resolution_summary", None),
        assignment_rationale=getattr(row, "assignment_rationale", None) or "",
        severity_rationale=getattr(row, "severity_rationale", None) or "",
        sla_ack_due=row.sla_ack_due,
        sla_restore_due=row.sla_restore_due,
        responsible_msp=getattr(row, "responsible_msp", None) or row.msp_name,
        radio_oem=getattr(row, "radio_oem", None) or "MIXED",
        failure_time=getattr(row, "failure_time", None) or getattr(row, "outage_start_at", None),
        expected_resolution_at=getattr(row, "expected_resolution_at", None) or row.sla_restore_due,
        escalated_at=getattr(row, "escalated_at", None),
        msp_eta_at=getattr(row, "msp_eta_at", None),
        msp_root_cause=getattr(row, "msp_root_cause", None),
        msp_action_taken=getattr(row, "msp_action_taken", None),
        msp_percent_complete=getattr(row, "msp_percent_complete", None),
        )
    )


def step_out(s: AgentRunStepRow) -> AgentStepOut:
    return AgentStepOut(
        **_z(
        id=s.id,
        seq=s.seq,
        node_name=s.node_name,
        agent_name=s.agent_name,
        status=s.status,
        started_at=s.started_at,
        finished_at=s.finished_at,
        duration_ms=s.duration_ms,
        input_summary=s.input_summary,
        output_summary=s.output_summary,
        rationale=s.rationale,
        tools_called=s.tools_called,
        confidence=s.confidence,
        )
    )


def run_out(run: AgentRunRow, include_steps: bool = True) -> AgentRunOut:
    steps = [step_out(s) for s in (run.steps if include_steps else [])]
    return AgentRunOut(
        **_z(
        id=run.id,
        incident_id=run.incident_id,
        operator_id=run.operator_id,
        graph_name=run.graph_name,
        trigger=run.trigger,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        current_node=run.current_node,
        error_summary=run.error_summary,
        steps=steps,
        )
    )
