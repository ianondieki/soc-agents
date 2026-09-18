"""TICKET: allocate the INC number and create the incident row with its narrative fields.

The only agent that talks to the tracker: it binds the new incident so every later
event carries the incident id and number.
"""

from __future__ import annotations

from datetime import timedelta

from noc_agents.db.models import IncidentRow, new_id, utcnow
from noc_agents.domain.enums import IncidentStatus
from noc_agents.orchestrator.contract import IncidentState, RunContext, StepResult
from noc_agents.services.composition import compose_narrative, region_label, sla_due
from noc_agents.services.numbering import next_incident_number


def input_summary(state: IncidentState, ctx: RunContext) -> str:
    return state.sev.priority.value


def run(state: IncidentState, ctx: RunContext) -> StepResult:
    cfg, session, event = ctx.cfg, ctx.session, state.event
    users, site_name, tt, sev = state.users, state.site_name, state.tt, state.sev
    number = next_incident_number(
        session,
        cfg.incident_prefix,
        cfg.timezone,
        numbering_style=getattr(cfg, "numbering_style", "inc9"),
    )
    services = ["VOICE", "DATA", "SMS"]
    if sev.mpesa_risk:
        services.append("MPESA_CORRIDOR")
    state.sla_ack_due, state.sla_restore_due = sla_due(sev.priority.value, cfg)
    narrative = compose_narrative(event, users, region_label(cfg, event.region_code), sev.rationale)
    # Hyphen, not an em dash. This title becomes the third line of every outgoing SMS, and a
    # single U+2014 forces the whole message out of the GSM 03.38 alphabet into UCS-2 — which
    # cuts the segment budget from 160 characters to 70 and turned the standard site-down
    # alert into 3 segments instead of 1. Three times the cost per recipient, and multipart
    # SMS can arrive out of order. Measured with services/gsm7.py, not guessed.
    #
    # Fixing template @2 alone does NOT solve this: the template's own em dash and this one
    # are two independent halves of the same defect, and the message stays UCS-2 until both
    # are clean. Found by the Phase 3 template review.
    title = (
        f"[{tt.tt_category}] {event.site_type} {event.failure_domain} - {site_name} "
        f"({region_label(cfg, event.region_code)})"
    )
    state.outage_start = event.outage_start_at or utcnow()
    inc = IncidentRow(
        id=new_id(),
        operator_id=cfg.operator_id,
        incident_number=number,
        status=IncidentStatus.TICKETED.value,
        priority=sev.priority.value,
        users_affected=users,
        service_affecting=True,
        site_id=event.site_id.upper(),
        site_name=site_name,
        site_type=event.site_type.upper(),
        region_code=event.region_code.upper(),
        county=state.county,
        title=title,
        description=event.description or f"{event.alarm_code} at {site_name}",
        narrative=narrative,
        root_cause_hypothesis=(
            f"Suspected {tt.tt_category_label} at {site_name} "
            f"({event.failure_domain}); awaiting field/MSP confirmation."
        ),
        impact_summary=(
            f"Est. {users:,} users; region {region_label(cfg, event.region_code)}; "
            f"class {tt.site_class}; children_down={event.child_sites_down}"
        ),
        access_notes=event.access_notes,
        correlation_fingerprint=state.fingerprint,
        is_hub_major=state.is_hub,
        mpesa_risk=sev.mpesa_risk,
        autonomy_level_applied=cfg.autonomy_level,
        failure_domain=event.failure_domain.upper(),
        alarm_code=event.alarm_code,
        sla_ack_due=state.sla_ack_due,
        sla_restore_due=state.sla_restore_due,
        next_update_at=utcnow() + timedelta(minutes=15),
        outage_start_at=state.outage_start,
        technology=tt.technology_csv,
        tt_category=tt.tt_category,
        tt_category_label=tt.tt_category_label,
        symptom_code=tt.symptom_code,
        site_class=tt.site_class,
        network_element=event.network_element or site_name,
        parent_hub_id=event.parent_hub_id.upper() if event.parent_hub_id else None,
        parent_incident_id=event.parent_incident_id,
        battery_countdown_min=event.battery_countdown_min,
        child_sites_down=event.child_sites_down,
        vendor_tt_ref=event.vendor_tt_ref,
        severity_rationale=sev.rationale,
    )
    inc.services_impacted = services
    session.add(inc)
    session.flush()
    state.incident = inc
    ctx.tracker.bind_incident(inc.id, inc.incident_number)
    return StepResult(
        output_summary=f"created {number} category={tt.tt_category}",
        rationale=(
            f"Unique incident number; TT fields filled as NOC UI would: "
            f"category={tt.tt_category}, class={tt.site_class}, tech={tt.technology_csv}; "
            f"HUB auto-ticket={'yes' if state.is_hub else 'n/a'}"
        ),
        tools=[{"name": "create_incident", "ok": True, "latency_ms": 5}],
    )
