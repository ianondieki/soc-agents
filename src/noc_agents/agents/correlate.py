"""CORRELATE: merge duplicates and link cascade children under an open HUB major.

Both outcomes short-circuit the run: the existing (or parent) incident is returned
and no new ticket is created.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from noc_agents.db.models import IncidentRow, WorkNoteRow, utcnow
from noc_agents.orchestrator.contract import SHORT_CIRCUIT, IncidentState, RunContext, StepResult


def input_summary(state: IncidentState, ctx: RunContext) -> str:
    return f"fp={state.fingerprint}"


def run(state: IncidentState, ctx: RunContext) -> StepResult:
    cfg, session, event = ctx.cfg, ctx.session, state.event
    window = int((cfg.correlation or {}).get("window_minutes", 15))
    cutoff = utcnow() - timedelta(minutes=window)
    existing = session.scalar(
        select(IncidentRow)
        .where(
            IncidentRow.operator_id == cfg.operator_id,
            IncidentRow.correlation_fingerprint == state.fingerprint,
            IncidentRow.created_at >= cutoff,
            IncidentRow.status.not_in(["CLOSED", "CANCELLED"]),
        )
        .order_by(IncidentRow.created_at.desc())
    )
    if existing:
        existing.updated_at = utcnow()
        if event.child_sites_down:
            existing.child_sites_down = max(existing.child_sites_down or 0, event.child_sites_down)
        session.add(
            WorkNoteRow(
                incident_id=existing.id,
                author="IngestCorrelationAgent",
                author_role="AGENT",
                body=f"Correlated duplicate alarm {event.alarm_code} into open ticket.",
                source="agent",
            )
        )
        return StepResult(
            status=SHORT_CIRCUIT,
            output_summary=f"merged into {existing.incident_number}",
            rationale=f"Duplicate within {window}m window — idempotent merge",
            tools=[{"name": "find_open_by_fingerprint", "ok": True, "latency_ms": 2}],
            incident=existing,
            event_type="incident.merged",
            event_payload={"incident_number": existing.incident_number},
        )

    # Parent HUB cascade: child site alarms under an open HUB major are linked, not standalone majors
    parent_inc = None
    if event.parent_hub_id or event.parent_incident_id:
        if event.parent_incident_id:
            parent_inc = session.get(IncidentRow, event.parent_incident_id)
        if parent_inc is None and event.parent_hub_id:
            parent_inc = session.scalar(
                select(IncidentRow)
                .where(
                    IncidentRow.operator_id == cfg.operator_id,
                    IncidentRow.site_id == event.parent_hub_id.upper(),
                    IncidentRow.site_type.in_(["HUB", "CORE"]),
                    IncidentRow.status.not_in(["CLOSED", "CANCELLED"]),
                    IncidentRow.created_at >= cutoff,
                )
                .order_by(IncidentRow.created_at.desc())
            )
        if parent_inc and event.site_type.upper() not in ("HUB", "CORE"):
            parent_inc.child_sites_down = (parent_inc.child_sites_down or 0) + 1
            parent_inc.updated_at = utcnow()
            session.add(
                WorkNoteRow(
                    incident_id=parent_inc.id,
                    author="IngestCorrelationAgent",
                    author_role="AGENT",
                    body=(
                        f"Cascade child alarm: {event.site_id} ({event.alarm_code}/"
                        f"{event.failure_domain}) linked under HUB major "
                        f"{parent_inc.incident_number}. Child count now {parent_inc.child_sites_down}."
                    ),
                    source="cascade",
                )
            )
            return StepResult(
                status=SHORT_CIRCUIT,
                output_summary=f"cascade child under {parent_inc.incident_number}",
                rationale=(
                    f"Site feeds parent HUB {event.parent_hub_id or parent_inc.site_id}; "
                    "linked as child note instead of new major (cascade control)"
                ),
                tools=[{"name": "link_parent_hub", "ok": True, "latency_ms": 2}],
                incident=parent_inc,
                event_type="incident.cascade_child",
                event_payload={
                    "parent": parent_inc.incident_number,
                    "child_site": event.site_id,
                    "child_sites_down": parent_inc.child_sites_down,
                },
            )

    return StepResult(
        output_summary="no open duplicate — new incident candidate",
        rationale="Fingerprint unique in correlation window; no parent HUB merge",
        tools=[{"name": "find_open_by_fingerprint", "ok": True, "latency_ms": 2}],
    )
