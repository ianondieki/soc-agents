"""Text/SLA/HITL-gate helpers shared by the lifecycle agents (moved from graph/pipeline.py).

``compose_sms`` / ``compose_email`` are the wording the pipeline releases while
``ALERT_ENVELOPE_V2`` is off (the default): ``services/hitl.render_channels`` calls them on
the incident row, the same code path as before the envelope existed. With the flag on the
same bytes come from ``services/render/*`` over the ``NocAlert`` envelope (the ``site_down_alert@1``
template, pinned byte-identical by ``tests/unit/test_alert_renderers.py::test_v1_fidelity_*``)
and the §6.2 validators decide whether they may leave. Do not "tidy" these strings: they are a
byte contract, and a change is a new template version, not an edit (spec §6.2, D3).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from noc_agents.config import OperatorConfig
from noc_agents.db.models import IncidentRow, utcnow
from noc_agents.domain.enums import Priority
from noc_agents.domain.schemas import EventIngest


def region_label(cfg: OperatorConfig, code: str) -> str:
    r = cfg.regions.get(code.upper())
    return r.label if r else code


def sla_due(priority: str, cfg: OperatorConfig) -> tuple[Any, Any]:
    band = cfg.sla_minutes.get(priority)
    now = utcnow()
    if not band:
        return now + timedelta(minutes=30), now + timedelta(hours=8)
    return now + timedelta(minutes=band.ack), now + timedelta(minutes=band.restore)


def needs_hitl(priority: Priority, autonomy: str) -> bool:
    if autonomy == "L1_COPILOT":
        return True
    if autonomy == "L3_CONDITIONAL":
        return priority == Priority.P1
    # L2
    return priority in (Priority.P1, Priority.P2)


def compose_sms(inc: IncidentRow) -> str:
    return (
        f"[{inc.priority}] {inc.incident_number} {inc.site_id} {inc.region_code}\n"
        f"{inc.failure_domain}|est.users {inc.users_affected}\n"
        f"{inc.title[:80]}\n"
        # Hyphen, not an em dash: this is the last line of every outgoing SMS and a single
        # U+2014 forces the whole message from GSM-7 (160 chars/segment) into UCS-2 (70),
        # turning the standard site-down alert into 3 segments instead of 1 -- triple the
        # cost per recipient, and multipart SMS can arrive out of order. Measured with
        # services/gsm7.py. Changed on the owner's explicit approval (it alters bytes that
        # reach customers); template @2 makes the same substitution.
        f"Owner:{inc.assignee_name} - ticket notes for updates"
    )


def compose_email(inc: IncidentRow, cfg: OperatorConfig) -> str:
    return (
        f"Subject: [{inc.priority}] {inc.incident_number} | {inc.site_name} ({inc.site_type}) | "
        f"{region_label(cfg, inc.region_code)} | {cfg.display_name}\n\n"
        f"Service affecting: {'YES' if inc.service_affecting else 'NO'}\n"
        f"Est. users: {inc.users_affected}\n"
        f"Services: {', '.join(inc.services_impacted)}\n"
        f"Failure domain: {inc.failure_domain}\n"
        f"M-PESA corridor risk: {'YES' if inc.mpesa_risk else 'NO'}\n\n"
        f"Summary: {inc.title}\n\n"
        f"Narrative:\n{inc.narrative}\n\n"
        f"Owner: {inc.assignee_name}\n"
        f"Hypothesis: {inc.root_cause_hypothesis}\n\n"
        f"Do not call NOC for routine status — update ticket / wait for next brief.\n"
    )


def compose_brief(cfg: OperatorConfig, inc: IncidentRow) -> str:
    """The executive status brief text (EXEC_BRIEF agent; also the LLM draft fallback)."""
    return (
        f"{cfg.display_name} status — {inc.incident_number} ({inc.priority})\n"
        f"Site: {inc.site_name} ({inc.site_type}) · Region: {region_label(cfg, inc.region_code)}\n"
        f"Est. users: {inc.users_affected:,} · M-PESA risk: {'YES' if inc.mpesa_risk else 'NO'}\n"
        f"Owner: {inc.assignee_name}\n"
        f"What we know: {inc.root_cause_hypothesis}\n"
        f"Impact: {inc.impact_summary}\n"
        f"Next update: ~15 min or on material change.\n"
        f"Please use this brief instead of calling NOC for routine status."
    )


def compose_narrative(event: EventIngest, users: int, region_label: str, sev_rationale: str) -> str:
    return (
        f"Service-affecting event detected at {event.site_name or event.site_id} "
        f"({event.site_type}) in {region_label} ({event.region_code}). "
        f"Alarm {event.alarm_code} / domain {event.failure_domain}. "
        f"Estimated subscribers impacted: {users:,}. "
        f"Access notes: {event.access_notes or 'none provided'}. "
        f"Severity engine: {sev_rationale}."
    )
