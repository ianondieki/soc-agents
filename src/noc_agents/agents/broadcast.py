"""BROADCAST: render the notifications per channel into transactional-outbox rows.

The node never transmits (spec §5.3.7 / §7.0.2). On the auto-send path it writes one
``BroadcastRow`` draft per (channel, audience) as QUEUED, then queues one SMS outbox row
per audience and one EMAIL outbox row per incident. The outbox dispatcher
(``orchestrator.outbox.drain_once``) sends them after the run has committed, flips the
drafts to SENT/FAILED, writes the email WorkNote and publishes ``email.sent``.

While a HITL task is open the drafts stay PENDING_HITL (written by the HITL node) and
nothing is queued; ``services/hitl.rerender_and_release`` queues them on approval.

``ALERT_ENVELOPE_V2`` (thrown in ``services/hitl.render_channels``, which the HITL node ran):

* off (default) — ``run`` below is today's code, unchanged: the strings on the state come
  from ``services/composition`` and the rows/keys/step literals are the golden ones;
* on — ``_run_v2``: the HITL node handed over ``state.channel_rendering``, the
  ``services/render`` payloads per (audience, channel). An ``OK`` payload is drafted and
  queued exactly as before, with §6.3 provenance (template version, encoding, segments) in
  ``outbox.payload_json``. A ``SUPPRESSED`` payload — the §6.2 validators or the template
  registry refused it — is drafted ``SUPPRESSED``, written to the outbox ``SUPPRESSED`` with
  the reason in ``last_error``, named in the step row, and never transmitted. It cannot look
  like a send: no QUEUED/SENT status, no ``email.sent`` event, no WorkNote.
"""

from __future__ import annotations

from noc_agents.db.models import BroadcastRow, new_id, utcnow
from noc_agents.orchestrator import outbox
from noc_agents.orchestrator.contract import WAITING_HITL, IncidentState, RunContext, StepResult
from noc_agents.services.alerts import v1_audience_name
from noc_agents.services.hitl import (
    RENDERER_V2,
    SUPPRESSED_DRAFT,
    ChannelRendering,
    enqueue_payload,
    payload_provenance,
)
from noc_agents.services.notify import (
    QUEUED,
    dispatch_incident_email,
    dispatch_incident_sms,
    render_email_payload,
    render_sms_payload,
)
from noc_agents.services.render import ChannelPayload
from noc_agents.services.render.email import email_text


def input_summary(state: IncidentState, ctx: RunContext) -> str:
    return "channels=SMS,EMAIL"


def run(state: IncidentState, ctx: RunContext) -> StepResult:
    rendering: ChannelRendering | None = getattr(state, "channel_rendering", None)
    v2 = rendering is not None and rendering.v2
    if state.waiting_hitl:
        summary = "broadcasts drafted; waiting HITL"
        if v2 and rendering.suppressed:  # the approver's card carries the detail; the step row names it
            summary += f"; {len(rendering.suppressed)}/{len(rendering.payloads)} renderings refused by §6.2 ({rendering.summary()})"
        return StepResult(
            status=WAITING_HITL,
            output_summary=summary,
            rationale="External wording held for supervisor/duty manager — approve HITL to send Gmail",
            tools=[{"name": "draft_broadcast", "ok": True, "latency_ms": 2}],
        )
    if v2:
        return _run_v2(state, ctx, rendering)

    cfg, session, inc = ctx.cfg, ctx.session, state.incident
    audiences = (cfg.broadcast or {}).get(f"{inc.priority.lower()}_audiences") or ["RNIO", "FIELD_ENGINEER"]
    # The HITL gate already let this priority through under the autonomy policy; the outbox
    # rows record that policy as their approver so the dispatcher's refusal rule can see it.
    approved_by = f"policy:{cfg.autonomy_level}"
    approved_at = utcnow()

    sms_drafts: list[tuple[str, str, str | None]] = []
    email_ids: list[str] = []
    for audience in audiences:
        sms = BroadcastRow(id=new_id(), incident_id=inc.id, channel="SMS", audience=audience, message=state.sms_body, status=QUEUED)
        mail = BroadcastRow(id=new_id(), incident_id=inc.id, channel="EMAIL", audience=audience, message=state.email_body, status=QUEUED)
        session.add_all([sms, mail])
        sms_drafts.append((audience, state.sms_body, sms.id))
        email_ids.append(mail.id)

    # render_sms + outbox.enqueue: one row per audience (the SMS adapter lands in P3; until then
    # the dispatcher records provider=mock, as the SMS drafts were mock-SENT before the outbox)
    sms_rows = dispatch_incident_sms(
        session, inc, drafts=sms_drafts, run_id=ctx.run.id, approved_by=approved_by, approved_at=approved_at
    )
    # render_email + outbox.enqueue: one real email per incident (not N copies per audience)
    mail = dispatch_incident_email(
        session,
        inc,
        state.email_body,
        audience=",".join(audiences),
        run_id=ctx.run.id,
        approved_by=approved_by,
        approved_at=approved_at,
        broadcast_ids=email_ids,
    )
    queued = len(sms_rows) + 1
    return StepResult(
        output_summary=f"queued {queued} outbox rows for {audiences}; email={mail['status']}",
        rationale=(
            f"{inc.priority} auto-send under {cfg.autonomy_level} (approved_by={approved_by}); "
            "dispatcher transmits after commit"
        ),
        tools=[
            {"name": "render_sms", "ok": True, "latency_ms": 1},
            {"name": "render_email", "ok": True, "latency_ms": 1},
            {"name": "outbox.enqueue", "ok": True, "latency_ms": 2, "error": None},
        ],
    )


def _run_v2(state: IncidentState, ctx: RunContext, rendering: ChannelRendering) -> StepResult:
    """The auto-send path from ``services/render`` payloads (``ALERT_ENVELOPE_V2`` on).

    Same rows and idempotency keys as the v1 path — one SMS outbox row per audience
    (``SMS:<incident id>:<audience>``), one EMAIL row per incident — so flipping the flag
    never double-sends an incident. Only the source of the bytes and the verdict differ.
    """
    cfg, session, inc = ctx.cfg, ctx.session, state.incident
    alert = state.alert  # type: ignore[attr-defined]  # set by the HITL node; v2 implies it exists
    audiences = [v1_audience_name(spec.audience) for spec in alert.audiences]
    approved_by = f"policy:{cfg.autonomy_level}"
    approved_at = utcnow()
    common = dict(
        envelope=alert,
        incident_id=inc.id,
        run_id=ctx.run.id,
        alert_id=alert.alert_id,
        approved_by=approved_by,
        approved_at=approved_at,
        operator_id=inc.operator_id,
    )

    queued = 0
    email_ids: list[str] = []
    email_payload: ChannelPayload | None = None
    for p in rendering.transmitted:  # envelope order: audience by audience, channel by channel
        audience = v1_audience_name(p.audience)
        draft = BroadcastRow(
            id=new_id(),
            incident_id=inc.id,
            channel=p.channel,
            audience=audience,
            message=rendering.text(p),  # the refused text is kept: the timeline shows what was not sent
            status=QUEUED if p.status == "OK" else SUPPRESSED_DRAFT,
        )
        session.add(draft)
        if p.channel == "SMS":
            row = enqueue_payload(
                session,
                kind=outbox.SMS,
                idempotency_key=f"SMS:{inc.id}:{audience}",
                payload={**render_sms_payload(inc, p.body, audience=audience, broadcast_id=draft.id), **payload_provenance(p)},
                rendered=p,
                **common,
            )
            queued += row.status == outbox.PENDING
        else:
            email_ids.append(draft.id)
            if email_payload is None:
                email_payload = p
    email_status = "not rendered"
    if email_payload is not None:  # one real email per incident (not N copies per audience)
        label = ",".join(audiences)
        row = enqueue_payload(
            session,
            kind=outbox.EMAIL,
            idempotency_key=f"EMAIL:{inc.id}:{label}",
            payload={
                **render_email_payload(inc, email_text(email_payload), audience=label, broadcast_ids=email_ids),
                **payload_provenance(email_payload),
            },
            rendered=email_payload,
            **common,
        )
        queued += row.status == outbox.PENDING
        email_status = row.status

    refused = rendering.suppressed
    summary = f"queued {queued} outbox rows for {audiences}; email={email_status}"
    if refused:
        summary += f"; {len(refused)}/{len(rendering.payloads)} renderings refused by §6.2 ({rendering.summary()}) — nothing sent for them"
    templates = rendering.templates
    template_label = ", ".join(f"{ch} {t['template_key']}@{t['template_version']}" for ch, t in templates.items()) or "no template"
    registry_errors = "; ".join(f"{ch}: {t['reason']}" for ch, t in templates.items() if not t["sendable"]) or None
    return StepResult(
        output_summary=summary,
        rationale=(
            f"{inc.priority} auto-send under {cfg.autonomy_level} (approved_by={approved_by}); "
            f"dispatcher transmits after commit; rendered by {RENDERER_V2} from envelope {alert.alert_id} "
            f"({template_label})"
        ),
        tools=[
            {"name": "template_registry.resolve", "ok": registry_errors is None, "latency_ms": 1, "error": registry_errors},
            {"name": "render_sms", "ok": rendering.channel_error("SMS") is None, "latency_ms": 1, "error": rendering.channel_error("SMS")},
            {"name": "render_email", "ok": rendering.channel_error("EMAIL") is None, "latency_ms": 1, "error": rendering.channel_error("EMAIL")},
            {"name": "outbox.enqueue", "ok": True, "latency_ms": 2, "error": None},
        ],
    )
