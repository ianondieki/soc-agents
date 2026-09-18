"""HITL task helpers shared by the HITL agent, the monitor and the decision routes.

An incident carries two convenience scalars (``requires_hitl``, ``hitl_state``) but may
own many ``HitlTaskRow`` rows. They are derived here from the task table so that no
writer can leave them stale (Stage C3).

Phase 2 (spec §6.5, brief defect #11) adds the content-aware half of the gate:

* ``render_channels`` — the ONE place ``ALERT_ENVELOPE_V2`` chooses how the wording is
  produced. Flag off (the default): ``services/composition.compose_sms`` / ``compose_email``
  on the incident row, the same code path as before the envelope existed, so the released
  bytes cannot move. Flag on: ``services/render/*`` over the ``NocAlert`` envelope, gated by
  the ``services/templates.TemplateRegistry`` (the declared ``template_key@version`` must be
  ``APPROVED`` in ``message_templates``) and by the §6.2 validators. A refused rendering is
  returned as ``SUPPRESSED`` with its reason; it is persisted that way (draft, outbox row,
  step row, HITL card) and never transmitted — and never looks like a send;
* ``rerender_and_release`` — what an APPROVE_BROADCAST approval does after the
  supervisor's overrides are applied: rebuild the envelope from the **updated** incident
  (``sequence + 1``, ``references=[old alert_id]``, the human as approver), re-render every
  channel, replace the draft wording, enqueue the new rows HELD and hand them to
  ``outbox.release_held`` — the one HELD → PENDING transition, which also suppresses any
  superseded HELD row. The draft the supervisor corrected is never enqueued;
* ``suppress_held_outbox`` — the reject side: HELD outbox rows → SUPPRESSED;
* the two policy switches: raiser ≠ approver (``is_raiser``) and the approve reason flag
  (``approve_reason_required``, default false — §2.1 R6 keeps the legacy approve bodies).

The v1 wording exists in exactly two places on purpose: ``services/composition.py`` (flag
off) and ``services/render/{sms,email}.py`` (flag on), pinned byte-identical by
``tests/unit/test_alert_renderers.py::test_v1_fidelity_*``. This module carries no copy.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from noc_agents.config import OperatorConfig
from noc_agents.db.models import BroadcastRow, HitlTaskRow, IncidentRow, OutboxRow, utcnow
from noc_agents.domain.alerts import AudienceSpec, NocAlert
from noc_agents.domain.enums import HitlState
from noc_agents.orchestrator import outbox
from noc_agents.services.alerts import (
    AUDIENCE_BY_CONFIG_NAME,
    alert_envelope_v2_enabled,
    build_alert,
    default_audiences,
)
from noc_agents.services.composition import compose_email, compose_sms
from noc_agents.services.notify import QUEUED, render_email_payload, render_sms_payload
from noc_agents.services.render import ChannelPayload, render_for_audience, warning
from noc_agents.services.render.email import email_text
from noc_agents.services.templates import DEFAULT_LANGUAGE, TemplateError, TemplateRegistry

log = logging.getLogger(__name__)

OPEN_TASK_STATUSES = ("PENDING", "CLAIMED")
GATING_TASK_TYPE = "APPROVE_BROADCAST"  # the only task type that holds the external broadcast

# ``hitl_tasks.created_by`` for a task the pipeline raises itself. An agent-raised task has
# no human raiser; this principal-shaped string can never equal a supervisor's name, so the
# raiser ≠ approver rule (§6.5) is satisfied by any human and never blocks an agent's task.
AGENT_RAISER = "agent:SupervisorAgent"
# §6.5 / §2.1 R6: approve needs a non-empty reason only when this is true (default false).
APPROVE_REASON_FLAG = "HITL_APPROVE_REASON_REQUIRED"
_TRUE = {"1", "true", "yes", "on"}
# The audience label today's release stamps on the one approved email row (and its WorkNote).
RELEASED_EMAIL_AUDIENCE = "HITL_APPROVED"

# --- the v2 rendering vocabulary (ALERT_ENVELOPE_V2 on) ------------------------------------------
# BroadcastRow.status for a rendering the validators or the registry refused. In BROADCAST_STATUSES
# (db/models.py), 10 chars, fits String(16). Distinct from CANCELLED (a human said no) and from
# any sent/queued status: a refused rendering must never read as a send.
SUPPRESSED_DRAFT = "SUPPRESSED"
RENDERER_V2 = "services/render"
# The channels the outbox can transmit today (orchestrator/outbox.py:_TRANSMITTERS). A rendering
# for any other channel (INAPP, WHATSAPP) is shown on the HITL card but gets no draft/outbox row.
TRANSMITTED_CHANNELS: tuple[str, ...] = ("SMS", "EMAIL")
# Operator-facing explanations for the codes the v1 template can hit (§6.2). The code alone is
# what the payload carries; this says what to do about it.
SUPPRESSION_HINTS: dict[str, str] = {
    "sms_not_gsm7": (
        "SMS body leaves the GSM-7 alphabet (the em dash in site_down_alert@1 forces UCS-2); "
        "§6.2 refuses it with the flag on — the GSM-7 wording is template @2 (spec D3)"
    ),
    "sms_too_many_segments": "SMS needs more segments than rendering.sms.max_segments allows; shorten the wording or raise the budget",
    "sms_missing_incident_number": "SMS body does not carry the incident number (§6.2)",
    "sms_missing_priority": "SMS body does not carry the priority token (§6.2)",
    "email_missing_incident_number": (
        "the v1 email carries the incident number only in the subject; §6.2 requires it in the body "
        "— needs the next template version (spec D3)"
    ),
    "email_missing_priority": "the v1 email carries the priority only in the subject; §6.2 requires it in the body (spec D3)",
    "email_missing_region_label": "the email body does not carry the region label; §6.2 requires it (spec D3)",
    "email_missing_next_update": "the v1 email has no 'Next update … EAT' line; §6.2 requires one (spec D3)",
    "no_template": "the envelope names a template_key@version the renderers cannot produce; templates are data (§6.3)",
    "no_approved_template": "message_templates has no APPROVED row for the declared template version (§6.3); approve it via PUT /api/v1/templates/{id}/status",
    "whatsapp_disabled": "WHATSAPP_ENABLED is off; WhatsApp is draft-only until Phase 6",
}


def suppression_text(code: str | None) -> str:
    """``"<code>: <what an operator can do about it>"`` — the outbox ``last_error`` / card text."""
    code = code or "suppressed"
    return f"{code}: {SUPPRESSION_HINTS.get(code, 'refused by the §6.2 validators; nothing was sent')}"


def is_open(task: HitlTaskRow) -> bool:
    return task.status in OPEN_TASK_STATUSES


def transition_open_task(session: Session, task: HitlTaskRow, new_status: str, **values) -> bool:
    """Compare-and-set: move ``task`` from an open status to ``new_status`` in one conditional UPDATE.

    Returns True when this call made the transition. Returns False when the task was no longer
    open, including when a concurrent decision won the write lock first: the losing UPDATE waits
    for the lock (SQLite busy timeout), then matches no row, so its caller can answer 409 without
    repeating the decision's side effects. ``task`` is refreshed to the row's current state.
    """
    changed = session.execute(
        update(HitlTaskRow)
        .where(HitlTaskRow.id == task.id, HitlTaskRow.status.in_(OPEN_TASK_STATUSES))
        .values(status=new_status, **values)
    ).rowcount
    if changed != 1:
        session.rollback()  # release the write lock before reading the winner's committed state
    session.refresh(task)
    return changed == 1


def sync_incident_hitl_scalars(session: Session, inc: IncidentRow) -> None:
    """Derive requires_hitl / hitl_state from the incident's tasks. Call after any task write.

    requires_hitl: any task PENDING or CLAIMED.
    hitl_state: PENDING while any task is open; otherwise the result (APPROVED / REJECTED) of
    the latest resolved gating task, or of the latest resolved task of any type when the
    incident never had a gating task; NONE when nothing was ever resolved.
    """
    session.flush()
    tasks = session.scalars(select(HitlTaskRow).where(HitlTaskRow.incident_id == inc.id)).all()
    open_tasks = [t for t in tasks if is_open(t)]
    inc.requires_hitl = bool(open_tasks)
    if open_tasks:
        inc.hitl_state = HitlState.PENDING.value
        return
    resolved = [t for t in tasks if t.resolved_at is not None]
    gating = [t for t in resolved if t.task_type == GATING_TASK_TYPE]
    latest = max(gating or resolved, key=lambda t: t.resolved_at, default=None)
    inc.hitl_state = latest.status if latest else HitlState.NONE.value


# --- policy switches (§6.5) -----------------------------------------------------------------


def approve_reason_required() -> bool:
    """``HITL_APPROVE_REASON_REQUIRED`` — default **false** (§2.1 R6: the legacy approve bodies
    of ``{"resolved_by": …}`` keep returning 200; production sets it true)."""
    return (os.getenv(APPROVE_REASON_FLAG) or "").strip().lower() in _TRUE


def is_raiser(task: HitlTaskRow, actor: str | None) -> bool:
    """Raiser ≠ approver (§6.5): true when the deciding actor is the person who raised the task.

    NULL ``created_by`` (rows older than the column, or tasks raised by code that does not
    stamp it yet) never matches: the rule fails open rather than locking a task out.
    """
    return bool(task.created_by) and bool(actor) and task.created_by == actor


# --- the envelope -------------------------------------------------------------------------------


def compose_alert(inc: IncidentRow, cfg: OperatorConfig, **kwargs) -> NocAlert | None:
    """``build_alert``, fail-soft: ``None`` when no envelope can be built for this row.

    The known case is an incident number outside §6.1's ``^INC\\d{6}$`` — the airtel
    profile's dated ``ATL-YYYYMMDD-NNNNN`` style, a spec/config contradiction the envelope
    wave reported rather than widened. SupervisorAgent is fail-closed, so a raise here
    would fail every such run; instead the gate and the wording fall back to exactly what
    they were before the envelope existed (``needs_hitl`` and today's composers).
    """
    try:
        return build_alert(inc, cfg, **kwargs)
    except ValidationError as exc:
        log.warning("hitl: no NocAlert envelope for %s (%s); rendering without one", inc.incident_number, exc.errors()[0].get("msg"))
        return None


# --- the flag: which renderer produced the wording ---------------------------------------------


@dataclass
class ChannelRendering:
    """What one rendering pass produced, for the HITL and BROADCAST nodes and the approve release.

    ``sms`` / ``email`` are the strings today's rows and task payloads carry (the email in its
    single-string ``"Subject: …\\n\\n<body>"`` form). ``v2`` says ``services/render`` produced
    them; then ``payloads`` holds one §6.2 ``ChannelPayload`` per (audience, channel) in
    envelope order — status, reason, measured encoding and segments, template provenance —
    and ``templates`` the registry's verdict per channel. On the v1 path both are empty.
    """

    sms: str
    email: str
    v2: bool = False
    payloads: list[ChannelPayload] = field(default_factory=list)
    templates: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def suppressed(self) -> list[ChannelPayload]:
        return [p for p in self.payloads if p.status != "OK"]

    @property
    def transmitted(self) -> list[ChannelPayload]:
        """The payloads that get a draft and an outbox row (SMS / EMAIL), in envelope order."""
        return [p for p in self.payloads if p.channel in TRANSMITTED_CHANNELS]

    def payload_for(self, channel: str, audience: str) -> ChannelPayload | None:
        """The payload for ``channel`` and ``audience`` (envelope name or today's config name,
        ``FIELD_ENGINEER`` ↔ ``FE``); the channel's first payload when that audience has none."""
        wanted = AUDIENCE_BY_CONFIG_NAME.get(audience, audience)
        for p in self.payloads:
            if p.channel == channel and p.audience == wanted:
                return p
        return next((p for p in self.payloads if p.channel == channel), None)

    @staticmethod
    def text(p: ChannelPayload) -> str:
        """The single string a draft / outbox payload carries for ``p`` (email joins subject+body)."""
        return email_text(p) if p.channel == "EMAIL" else p.body

    def summary(self) -> str:
        """``"SMS: sms_not_gsm7 x2; EMAIL: email_missing_incident_number x2"`` — for step rows."""
        counts: dict[tuple[str, str], int] = {}
        for p in self.suppressed:
            key = (p.channel, p.suppress_reason or "suppressed")
            counts[key] = counts.get(key, 0) + 1
        return "; ".join(f"{channel}: {reason} x{n}" for (channel, reason), n in counts.items())

    def channel_error(self, channel: str) -> str | None:
        """``"sms_not_gsm7 x2"`` for the channel's refused payloads, None when all are OK."""
        refused = [p for p in self.suppressed if p.channel == channel]
        if not refused:
            return None
        counts: dict[str, int] = {}
        for p in refused:
            counts[p.suppress_reason or "suppressed"] = counts.get(p.suppress_reason or "suppressed", 0) + 1
        return "; ".join(f"{reason} x{n}" for reason, n in counts.items())

    def card(self) -> dict[str, Any]:
        """JSON-safe, for ``hitl_tasks.proposed_payload["channels"]`` — the inbox card's side-by-side
        block (§6.5): every rendering with its verdict, reason, segments/encoding and template."""
        return {
            "renderer": RENDERER_V2,
            "templates": self.templates,
            "total": len(self.payloads),
            "suppressed": len(self.suppressed),
            "payloads": [
                {**p.model_dump(mode="json"), "reason": suppression_text(p.suppress_reason) if p.status != "OK" else None}
                for p in self.payloads
            ],
        }


def render_channels(
    alert: NocAlert | None, inc: IncidentRow, cfg: OperatorConfig, *, session: Session | None = None
) -> ChannelRendering:
    """The wording for one envelope — and the switch ``ALERT_ENVELOPE_V2`` throws.

    Off (the default), or no envelope (``compose_alert`` returned None): today's composers on
    the incident row. This is the same code path as before the envelope existed, not merely an
    equivalent one, so the golden bytes cannot move.

    On: ``services/render`` over the envelope, one payload per (audience, channel), gated by
    the template registry (``session`` is required for it). The strings are still byte-identical
    to the composers for ``site_down_alert@1`` (``test_alert_renderers.py::test_v1_fidelity_*``);
    what changes is the verdict: a rendering the validators or the registry refuse comes back
    ``SUPPRESSED`` with its reason, and the callers persist it that way.
    """
    if alert is None or not alert_envelope_v2_enabled():
        return ChannelRendering(sms=compose_sms(inc), email=compose_email(inc, cfg))
    if session is None:
        raise ValueError("render_channels: a session is required with ALERT_ENVELOPE_V2 on (template registry)")
    return _render_v2(alert, inc, cfg, session)


def _render_v2(alert: NocAlert, inc: IncidentRow, cfg: OperatorConfig, session: Session) -> ChannelRendering:
    templates = resolve_templates(alert, session, cfg)
    payloads: list[ChannelPayload] = []
    for spec in alert.audiences:
        for payload in render_for_audience(alert, spec):
            gate = templates.get(payload.channel)
            if gate is not None and not gate["sendable"]:
                payload = _refused_by_registry(payload, gate)
            payloads.append(payload)
    sms = next((p for p in payloads if p.channel == "SMS"), None)
    email = next((p for p in payloads if p.channel == "EMAIL"), None)
    return ChannelRendering(
        # A channel no audience lists is not rendered (§6.7.2); the string then falls back to the
        # composer so state.sms_body / state.email_body are never None.
        sms=sms.body if sms is not None else compose_sms(inc),
        email=email_text(email) if email is not None else compose_email(inc, cfg),
        v2=True,
        payloads=payloads,
        templates=templates,
    )


def template_registry(session: Session, cfg: OperatorConfig) -> TemplateRegistry:
    """The operator's registry, seeded from ``config/templates`` the first time a database is used.

    Nothing else on the hot path calls ``sync`` today; without this the flag-on path would
    resolve every template to "no template". ``sync`` is idempotent and versioned (§6.3), so
    a seeded database is a pure read here and a later YAML change still needs an explicit
    sync plus a human approval before it can be sent.
    """
    registry = TemplateRegistry.for_config(session, cfg)
    if not registry.all_rows():
        report = registry.sync()
        log.info("hitl: seeded message_templates for %s on first use: %s", cfg.operator_id, report)
    return registry


def resolve_templates(alert: NocAlert, session: Session, cfg: OperatorConfig) -> dict[str, dict[str, Any]]:
    """Per channel, whether the envelope's ``template_key@template_version`` is APPROVED in the table.

    The envelope is the authority on *which* version (``governance.template_version``, stamped
    by ``build_alert``); the registry is the authority on whether that version may be sent.
    Never raises: a registry failure is a verdict (``sendable=False`` with the error as the
    reason), because SupervisorAgent is fail-closed and a refused rendering is the safe outcome.
    """
    declared = {
        "SMS": alert.rendering.sms.template_key if alert.rendering.sms is not None else None,
        "EMAIL": alert.rendering.email.template_key if alert.rendering.email is not None else None,
    }
    version = alert.governance.template_version
    verdicts: dict[str, dict[str, Any]] = {}
    try:
        registry: TemplateRegistry | None = template_registry(session, cfg)
        registry_error: str | None = None
    except TemplateError as exc:  # a broken seed must not crash the run; it must refuse the send
        registry, registry_error = None, f"template registry unavailable: {exc}"
        log.error("hitl: %s", registry_error)
    for channel, key in declared.items():
        if not key:
            continue
        verdict: dict[str, Any] = {
            "template_key": key,
            "template_version": version,
            "language": DEFAULT_LANGUAGE,
            "sendable": False,
            "approval_status": None,
            "approved_by": None,
            "reason": registry_error,
        }
        if registry is not None:
            try:
                pinned = int(version)
                resolution = registry.resolve(channel, key, DEFAULT_LANGUAGE, version=pinned, require_approved=True)
                row = resolution.row or registry.get(channel, key, DEFAULT_LANGUAGE, pinned)
            except (TemplateError, ValueError) as exc:
                verdict["reason"] = f"template registry error: {exc}"
            else:
                if row is not None:
                    verdict["approval_status"] = row.approval_status
                    verdict["approved_by"] = row.approved_by
                verdict["sendable"] = resolution.ok
                verdict["reason"] = resolution.reason
        verdicts[channel] = verdict
    return verdicts


def _refused_by_registry(payload: ChannelPayload, gate: dict[str, Any]) -> ChannelPayload:
    """§6.2 / §6.3: no APPROVED template row → ``SUPPRESSED no_approved_template``, whatever the
    validator said; the validator's own verdict is kept as a warning so the card shows both."""
    notes = list(payload.warnings)
    if payload.status != "OK" and payload.suppress_reason:
        notes.append(warning(payload.suppress_reason, "also refused by the §6.2 validator"))
    notes.append(warning("no_approved_template", gate.get("reason") or "no APPROVED template row"))
    return payload.model_copy(update={"status": "SUPPRESSED", "suppress_reason": "no_approved_template", "warnings": notes})


def payload_provenance(p: ChannelPayload) -> dict[str, Any]:
    """The §6.3 provenance stamped into ``outbox.payload_json`` on the v2 path: which renderer,
    which template version, the measured SMS cost, and the rendering verdict."""
    return {
        "renderer": RENDERER_V2,
        "template_key": p.template_key,
        "template_version": p.template_version,
        "language": p.rendered_language,
        "encoding": p.encoding,
        "segments": p.segments,
        "rendering_status": p.status,
        "suppress_reason": p.suppress_reason,
        "channel_idempotency_key": p.idempotency_key,
    }


def enqueue_payload(
    session: Session, *, kind: str, idempotency_key: str, payload: dict, rendered: ChannelPayload, **common
) -> OutboxRow:
    """``outbox.enqueue`` for one rendered channel.

    An ``OK`` rendering is queued exactly as the v1 path queues it (PENDING, or HELD when the
    caller says so). A ``SUPPRESSED`` rendering is still written — the outbox is the ledger of
    every outbound side effect, refused ones included — and closed as ``SUPPRESSED`` in the same
    transaction with the reason in ``last_error``, so the dispatcher never claims it, no
    ``email.sent`` is ever published for it, and ``release_held`` cannot promote it.
    """
    row = outbox.enqueue(session, kind=kind, idempotency_key=idempotency_key, payload=payload, **common)
    if rendered.status != "OK":
        session.execute(
            update(OutboxRow)
            .where(OutboxRow.id == row.id, OutboxRow.status.in_((outbox.PENDING, outbox.HELD)))
            .values(status=outbox.SUPPRESSED, last_error=suppression_text(rendered.suppress_reason)[:2000], updated_at=utcnow())
        )
        session.refresh(row)
    return row


def envelope_payload(alert: NocAlert) -> dict:
    """The envelope as JSON-safe data for ``hitl_tasks.proposed_payload_json`` (aware UTC, ``Z``)."""
    return alert.model_dump(mode="json")


def stored_envelope(task: HitlTaskRow) -> NocAlert | None:
    """The envelope the HITL node stored on the task, or None for rows written before it did."""
    raw = task.proposed_payload.get("envelope")
    if not raw:
        return None
    try:
        return NocAlert.model_validate(raw)
    except ValueError:  # a payload someone edited by hand: re-render from the row instead of crashing
        return None


# --- approve: re-render from the updated incident, then release (§6.5) --------------------------


@dataclass
class ReleaseResult:
    """What an approval did. ``edited`` is true when the released wording differs from the
    draft the task proposed — the source of ``hitl_tasks.edited`` and M15's zero-edit counter.
    ``suppressed`` counts the outbox rows the v2 renderers refused (written SUPPRESSED, never sent)."""

    alert: NocAlert | None = None
    sms: str | None = None
    email: str | None = None
    edited: bool = False
    released: int = 0  # outbox rows handed to the dispatcher
    suppressed: int = 0  # outbox rows refused by the §6.2 validators / registry (v2 only)
    broadcast_ids: list[str] = field(default_factory=list)
    rendering: ChannelRendering | None = None


def rerender_and_release(
    session: Session,
    inc: IncidentRow,
    cfg: OperatorConfig,
    *,
    task: HitlTaskRow,
    approved_by: str,
    approved_at: datetime,
) -> ReleaseResult:
    """APPROVE_BROADCAST, after the overrides are on ``inc``: rebuild → re-render → release.

    Runs inside the caller's transaction and transmits nothing. The PENDING_HITL drafts get
    the re-rendered wording and go QUEUED; one SMS outbox row per SMS draft and one EMAIL row
    are enqueued HELD for the NEW alert and released through ``outbox.release_held`` (HELD →
    PENDING with the human approver stamped; any other HELD row of the incident → SUPPRESSED).
    The draft wording the supervisor saw is kept on ``proposed_payload`` for the audit trail;
    what actually left is recorded beside it under ``released``.

    With ``ALERT_ENVELOPE_V2`` on the same rows are written from the ``services/render``
    payloads: a refused rendering makes its draft ``SUPPRESSED`` and its outbox row
    ``SUPPRESSED`` with the reason, and is not counted as released.
    """
    drafts = session.scalars(
        select(BroadcastRow)
        .where(BroadcastRow.incident_id == inc.id, BroadcastRow.status == "PENDING_HITL")
        .order_by(BroadcastRow.id)
    ).all()
    if not drafts:
        return ReleaseResult()

    previous = stored_envelope(task)
    sequence = (previous.sequence + 1) if previous else 2
    alert = compose_alert(
        inc,
        cfg,
        audiences=_reviewed_audiences(previous, inc, cfg),
        sequence=sequence,
        references=[previous.alert_id] if previous else None,
        hitl_task_id=task.id,
    )
    if alert is not None:
        # A human approved this rendering, whatever the autonomy policy says about the new
        # priority; revalidate so approved_at obeys the envelope's aware-UTC rule.
        data = alert.model_dump()
        data["governance"].update(requires_hitl=True, approved_by=approved_by, approved_at=approved_at, hitl_task_id=task.id)
        alert = NocAlert.model_validate(data)
        alert_id, seed = alert.alert_id, alert.idempotency_seed  # "<incident id>|ALERT|<sequence>": one key space per alert
    else:  # no envelope for this profile (see compose_alert): same release, keyed the same way
        alert_id, seed = str(uuid.uuid4()), f"{inc.id}|ALERT|{sequence}"
    rendering = render_channels(alert, inc, cfg, session=session)
    sms, email = rendering.sms, rendering.email

    proposed = task.proposed_payload
    edited = sms != proposed.get("sms") or email != proposed.get("email")

    common = dict(
        envelope=alert,
        incident_id=inc.id,
        run_id=task.run_id,
        hitl_task_id=task.id,
        alert_id=alert_id,
        requires_hitl=True,
        held=True,
        operator_id=inc.operator_id,
    )
    if rendering.v2:
        suppressed = _release_v2(
            session, inc, rendering, drafts, seed=seed, common=common, approved_by=approved_by, approved_at=approved_at
        )
    else:
        suppressed = 0
        sms_drafts = [b for b in drafts if b.channel == "SMS"]
        email_drafts = [b for b in drafts if b.channel == "EMAIL"]
        for b in drafts:
            b.message = sms if b.channel == "SMS" else email  # the approved wording replaces the draft
            b.status = QUEUED
            b.sent_at = None
        for b in sms_drafts:
            outbox.enqueue(
                session,
                kind=outbox.SMS,
                idempotency_key=f"SMS:{seed}:{b.audience}",
                payload=render_sms_payload(inc, sms, audience=b.audience, broadcast_id=b.id),
                **common,
            )
        if email_drafts:  # one real email per incident, from the approved wording
            outbox.enqueue(
                session,
                kind=outbox.EMAIL,
                idempotency_key=f"EMAIL:{seed}:{RELEASED_EMAIL_AUDIENCE}",
                payload=render_email_payload(
                    inc, email, audience=RELEASED_EMAIL_AUDIENCE, broadcast_ids=[b.id for b in email_drafts]
                ),
                **common,
            )
    released = outbox.release_held(
        session, incident_id=inc.id, alert_id=alert_id, approved_by=approved_by, approved_at=approved_at
    )
    record: dict[str, Any] = {
        "alert_id": alert_id,
        "sequence": sequence,
        "priority": inc.priority,
        "sms": sms,
        "email": email,
        "edited": edited,
    }
    if rendering.v2:
        record["channels"] = rendering.card()
    task.proposed_payload = {**proposed, "released": record}
    session.flush()
    return ReleaseResult(
        alert=alert,
        sms=sms,
        email=email,
        edited=edited,
        released=released,
        suppressed=suppressed,
        broadcast_ids=[b.id for b in drafts],
        rendering=rendering,
    )


def _release_v2(
    session: Session,
    inc: IncidentRow,
    rendering: ChannelRendering,
    drafts: list[BroadcastRow],
    *,
    seed: str,
    common: dict[str, Any],
    approved_by: str,
    approved_at: datetime,
) -> int:
    """The v2 half of ``rerender_and_release``: drafts and HELD outbox rows from the payloads.

    Same row shape and keys as the v1 branch (one SMS row per SMS draft, one EMAIL row per
    incident) plus §6.3 provenance in the payload. Returns the number of outbox rows the
    renderers refused; those are SUPPRESSED before ``release_held`` runs, so it cannot promote
    them, and their drafts read ``SUPPRESSED`` on the timeline rather than QUEUED. The approver
    is stamped at enqueue time (``release_held`` would stamp only the rows it promotes): the
    human did approve the wording; the renderers refused it, and the row records both.
    """
    common = {**common, "approved_by": approved_by, "approved_at": approved_at}
    suppressed = 0
    email_drafts = [b for b in drafts if b.channel == "EMAIL"]
    email_payload: ChannelPayload | None = None
    for b in drafts:
        payload = rendering.payload_for(b.channel, b.audience)
        if payload is None:  # the envelope lists no such channel: nothing rendered, nothing to send
            b.message = rendering.sms if b.channel == "SMS" else rendering.email
            b.status = SUPPRESSED_DRAFT
            b.sent_at = None
            continue
        b.message = ChannelRendering.text(payload)  # the approved wording replaces the draft
        b.status = QUEUED if payload.status == "OK" else SUPPRESSED_DRAFT
        b.sent_at = None
        if b.channel == "SMS":
            row = enqueue_payload(
                session,
                kind=outbox.SMS,
                idempotency_key=f"SMS:{seed}:{b.audience}",
                payload={**render_sms_payload(inc, payload.body, audience=b.audience, broadcast_id=b.id), **payload_provenance(payload)},
                rendered=payload,
                **common,
            )
            suppressed += row.status == outbox.SUPPRESSED
        elif email_payload is None:
            email_payload = payload
    if email_payload is not None:  # one real email per incident, from the approved wording
        row = enqueue_payload(
            session,
            kind=outbox.EMAIL,
            idempotency_key=f"EMAIL:{seed}:{RELEASED_EMAIL_AUDIENCE}",
            payload={
                **render_email_payload(
                    inc, email_text(email_payload), audience=RELEASED_EMAIL_AUDIENCE, broadcast_ids=[b.id for b in email_drafts]
                ),
                **payload_provenance(email_payload),
            },
            rendered=email_payload,
            **common,
        )
        suppressed += row.status == outbox.SUPPRESSED
    return suppressed


def _reviewed_audiences(previous: NocAlert | None, inc: IncidentRow, cfg: OperatorConfig) -> list[AudienceSpec] | None:
    """The audience set the approver reviewed, with recipient refs refreshed for the updated row.

    A priority override does not silently widen or narrow the recipient list (that is a
    product decision, reported, not taken here): the re-render goes to the audiences on
    the card. Refs are re-derived where the config still names the audience, so an MSP
    override lands on ``msp_contacts.<new code>``.
    """
    if previous is None:
        return None  # legacy task without an envelope: today's defaults for the current priority
    fresh = {spec.audience: spec for spec in default_audiences(inc, cfg)}
    return [fresh.get(spec.audience, spec) for spec in previous.audiences]


# --- reject: nothing HELD for this incident may ever be claimed ---------------------------------


def suppress_held_outbox(session: Session, incident_id: str, *, reason: str) -> int:
    """HELD outbox rows of the incident → SUPPRESSED (§6.5 reject). Returns the count.

    Today the HITL node enqueues nothing while gated, so this is usually a no-op; it is the
    guard for the §6.5 flow in which drafts are HELD in the outbox from the moment they are
    composed.
    """
    return session.execute(
        update(OutboxRow)
        .where(OutboxRow.incident_id == incident_id, OutboxRow.status == outbox.HELD)
        .values(status=outbox.SUPPRESSED, last_error=reason[:2000], updated_at=utcnow())
    ).rowcount
