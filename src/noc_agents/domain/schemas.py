from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class EventIngest(BaseModel):
    site_id: str
    site_name: str | None = None
    site_type: str = "BTS"
    region_code: str = "NBI"
    county: str | None = None
    alarm_code: str = "SITE_DOWN"
    failure_domain: str = "UNKNOWN"
    technology: list[str] = Field(default_factory=lambda: ["4G"])
    severity_raw: str = "MAJOR"
    users_affected: int | None = None
    child_sites_down: int = 0
    multi_region: bool = False
    access_notes: str | None = None
    description: str | None = None
    source: str = "manual"
    parent_hub_id: str | None = None
    parent_incident_id: str | None = None
    network_element: str | None = None
    battery_countdown_min: int | None = None
    outage_start_at: datetime | None = None
    vendor_tt_ref: str | None = None


class SessionIn(BaseModel):
    display_name: str = "NOC Analyst"
    role: str = "noc_analyst"
    region_filter: str | None = None
    msp_filter: str | None = None


class NoteIn(BaseModel):
    author: str = "NOC"
    author_role: str = "NOC"
    body: str
    source: str = "ui"
    mark_restored: bool = False
    vendor_tt_ref: str | None = None
    # MSP progress fields (filled while resolving)
    msp_eta_at: datetime | None = None
    msp_root_cause: str | None = None
    msp_action_taken: str | None = None
    msp_percent_complete: int | None = None


class HitlDecision(BaseModel):
    resolved_by: str = "supervisor"
    overrides: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None


class CloseIn(BaseModel):
    closed_by: str = "NOC"
    resolution_code: str = "CLOSED_NORMAL"
    resolution_summary: str = ""


class ReassignIn(BaseModel):
    by: str = "supervisor"
    reason: str
    assignee_type: str = "MSP"  # MSP | FIELD_ENGINEER | NOC
    assignee_name: str
    msp_name: str | None = None
    fe_name: str | None = None


class IncidentOut(BaseModel):
    id: str
    operator_id: str
    incident_number: str
    status: str
    priority: str
    users_affected: int
    service_affecting: bool
    services_impacted: list[str]
    site_id: str
    site_name: str
    site_type: str
    region_code: str
    county: str | None
    title: str
    description: str
    narrative: str
    root_cause_hypothesis: str
    impact_summary: str
    assignee_type: str
    assignee_name: str | None
    msp_name: str | None
    fe_name: str | None
    rnio_name: str | None = None
    access_notes: str | None
    created_at: datetime
    updated_at: datetime
    is_hub_major: bool
    recurrence_count: int
    problem_id: str | None
    mpesa_risk: bool
    autonomy_level_applied: str
    requires_hitl: bool
    hitl_state: str
    failure_domain: str
    correlation_fingerprint: str
    # Extended NOC TT fields
    outage_start_at: datetime | None = None
    technology: str = "4G"
    tt_category: str = "OTHER"
    tt_category_label: str = ""
    symptom_code: str = ""
    site_class: str = "STANDARD"
    network_element: str = ""
    parent_hub_id: str | None = None
    parent_incident_id: str | None = None
    vendor_tt_ref: str | None = None
    battery_countdown_min: int | None = None
    child_sites_down: int = 0
    acknowledged_at: datetime | None = None
    first_vendor_note_at: datetime | None = None
    restored_at: datetime | None = None
    closed_at: datetime | None = None
    resolution_code: str | None = None
    resolution_summary: str | None = None
    assignment_rationale: str = ""
    severity_rationale: str = ""
    sla_ack_due: datetime | None = None
    sla_restore_due: datetime | None = None
    responsible_msp: str | None = None
    radio_oem: str = "MIXED"
    failure_time: datetime | None = None
    expected_resolution_at: datetime | None = None
    escalated_at: datetime | None = None
    msp_eta_at: datetime | None = None
    msp_root_cause: str | None = None
    msp_action_taken: str | None = None
    msp_percent_complete: int | None = None

    model_config = {"from_attributes": True}


class AgentStepOut(BaseModel):
    id: str
    seq: int
    node_name: str
    agent_name: str
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    input_summary: str | None
    output_summary: str | None
    rationale: str | None
    tools_called: list[dict[str, Any]]
    confidence: float | None


class AgentRunOut(BaseModel):
    id: str
    incident_id: str | None
    operator_id: str
    graph_name: str
    trigger: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    current_node: str | None
    error_summary: str | None
    steps: list[AgentStepOut] = Field(default_factory=list)


class WorkflowOut(BaseModel):
    incident_id: str
    run_id: str | None
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    steps: list[AgentStepOut]


class MetricsSummary(BaseModel):
    operator_id: str
    operator_display_name: str
    autonomy_level: str
    open_total: int
    by_priority: dict[str, int]
    hitl_pending: int
    sla_risk: int
    agents_running: int
    by_region: dict[str, int]
    problems_open: int
    silent_at_risk: int = 0
