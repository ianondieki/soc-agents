"""HITL gate: decide whether a human must approve the external broadcast.

Content-aware since Phase 2 (spec §5.3.1, §6.5): the node composes the ``NocAlert``
envelope (``services/alerts.build_alert``) for the incident, renders the channels from it
(``services/hitl.render_channels``) and gates on ``needs_hitl(priority, autonomy) OR
alert.governance.requires_hitl``. When approval is required the wording is drafted as
PENDING_HITL rows, the envelope is stored on the task's ``proposed_payload`` (beside the
rendered strings) and a HITL task is opened; the run continues but is marked WAITING_HITL.

``ALERT_ENVELOPE_V2`` is thrown inside ``render_channels``. Off (default): today's composers,
the same code path as before the envelope existed. On: ``services/render/*`` over the
envelope, gated by the template registry and the §6.2 validators; every (audience, channel)
payload — with its verdict, reason, segments and template version — is stored on the task
under ``proposed_payload["channels"]`` (the §6.5 side-by-side card) and handed to the
BROADCAST node on ``state.channel_rendering``. A refused rendering is drafted so the
approver can see what was refused and why; it is never queued as if it were fine.

The approve route re-renders from the envelope after the supervisor's overrides — this
node's draft is what the approver reviews, never what is transmitted (brief defect #11).
"""

from __future__ import annotations

from noc_agents.db.models import BroadcastRow, HitlTaskRow, new_id
from noc_agents.orchestrator.contract import WAITING_HITL, IncidentState, RunContext, StepResult
from noc_agents.services.alerts import v1_audience_name
from noc_agents.services.composition import needs_hitl
from noc_agents.services.hitl import (
    AGENT_RAISER,
    GATING_TASK_TYPE,
    ChannelRendering,
    compose_alert,
    envelope_payload,
    render_channels,
    sync_incident_hitl_scalars,
)
from noc_agents.services.render import ChannelPayload  # the §6.2 payload the v2 drafts are written from


def input_summary(state: IncidentState, ctx: RunContext) -> str:
    return ctx.cfg.autonomy_level


def _render_tool(rendering: ChannelRendering) -> dict:
    """The tools_called entry the v2 path adds: which renderer ran and what it refused."""
    refused = rendering.suppressed
    return {
        "name": "render_channels",
        "ok": not refused,
        "latency_ms": 1,
        "error": f"{len(refused)}/{len(rendering.payloads)} refused ({rendering.summary()})" if refused else None,
    }


def run(state: IncidentState, ctx: RunContext) -> StepResult:
    """inc.requires_hitl / inc.hitl_state are derived from the task table by
    sync_incident_hitl_scalars (C3): this agent only decides whether to open a task."""
    cfg, session, inc = ctx.cfg, ctx.session, state.incident
    alert = compose_alert(inc, cfg)  # sequence 1; read-only on the row; None when the profile's numbers do not fit §6.1
    rendering = render_channels(alert, inc, cfg, session=session)
    state.sms_body, state.email_body = rendering.sms, rendering.email
    # The envelope and the rendering ride on the state for later nodes. ``IncidentState``
    # (orchestrator/contract.py) does not declare the fields yet, so they are set dynamically;
    # read them with getattr(state, "alert" / "channel_rendering", None).
    state.alert = alert  # type: ignore[attr-defined]
    state.channel_rendering = rendering  # type: ignore[attr-defined]
    requires = needs_hitl(state.sev.priority, cfg.autonomy_level) or (alert is not None and alert.governance.requires_hitl)
    if not requires:
        sync_incident_hitl_scalars(session, inc)  # a fresh incident has no tasks: stays False / NONE
        state.waiting_hitl = False
        return StepResult(
            output_summary="auto-approved under autonomy policy",
            rationale=f"{inc.priority} allowed auto-broadcast at {cfg.autonomy_level}",
            tools=[_render_tool(rendering)] if rendering.v2 else [],
        )

    task = HitlTaskRow(
        id=new_id(),
        incident_id=inc.id,
        task_type=GATING_TASK_TYPE,
        status="PENDING",
        run_id=ctx.run.id,
        entity_type="incident",
        entity_id=inc.id,
        created_by=AGENT_RAISER,  # raised by the pipeline, not by a person (§6.5 raiser ≠ approver)
    )
    payload = {
        "priority": inc.priority,
        "sms": state.sms_body,
        "email": state.email_body,
        "audiences": (cfg.broadcast or {}).get(f"{inc.priority.lower()}_audiences")
        or (cfg.broadcast or {}).get("p1_audiences")
        or ["RNIO", "FIELD_ENGINEER"],
        "assignee": inc.assignee_name,
    }
    if alert is not None:
        alert = alert.model_copy(update={"governance": alert.governance.model_copy(update={"hitl_task_id": task.id})})
        state.alert = alert  # type: ignore[attr-defined]
        payload["audiences"] = [v1_audience_name(spec.audience) for spec in alert.audiences]  # today's config names
        payload["alert_id"] = alert.alert_id
        payload["envelope"] = envelope_payload(alert)
        if rendering.v2:
            payload["channels"] = rendering.card()  # §6.5: every rendering side by side, verdicts included
    task.proposed_payload = payload
    audiences = payload["audiences"]
    session.add(task)
    sync_incident_hitl_scalars(session, inc)  # derived from the task table: PENDING / True
    # Still draft broadcast records as DRAFTED
    if rendering.v2:
        for p in rendering.transmitted:  # one draft per rendered (audience, channel) the outbox can carry
            session.add(_draft(inc.id, p, rendering))
    else:
        for audience in audiences:
            session.add(
                BroadcastRow(
                    incident_id=inc.id,
                    channel="SMS",
                    audience=audience,
                    message=state.sms_body,
                    status="PENDING_HITL",
                )
            )
            session.add(
                BroadcastRow(
                    incident_id=inc.id,
                    channel="EMAIL",
                    audience=audience,
                    message=state.email_body,
                    status="PENDING_HITL",
                )
            )
    state.waiting_hitl = True
    tools = [{"name": "create_hitl_task", "ok": True, "latency_ms": 1}]
    if rendering.v2:
        tools.append(_render_tool(rendering))
    return StepResult(
        status=WAITING_HITL,
        output_summary=f"HITL task {task.id}",
        rationale=f"{inc.priority} under {cfg.autonomy_level} requires human approval before external blast",
        tools=tools,
    )


def _draft(incident_id: str, p: ChannelPayload, rendering: ChannelRendering) -> BroadcastRow:
    """A PENDING_HITL draft from one v2 payload. A refused rendering is still drafted — the
    approver must see what was refused — and the verdict travels on the task card; the approve
    re-render decides the row's final status (QUEUED or SUPPRESSED)."""
    return BroadcastRow(
        incident_id=incident_id,
        channel=p.channel,
        audience=v1_audience_name(p.audience),
        message=rendering.text(p),
        status="PENDING_HITL",
    )
