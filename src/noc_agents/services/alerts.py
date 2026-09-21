"""build_alert: compose a ``NocAlert`` envelope from an ``IncidentRow`` plus operator config.

Spec §6.1 "Who fills what": everything except ``content{}`` is set deterministically here
from the incident row and the YAML; ``content`` is the deterministic template unless the
caller hands in model-drafted ``ai_content``, which is checked HERE by
``services/validators.validate_content`` (see ``VALIDATE_AI_CONTENT`` for its switch).

**This module is inert today.** ``ALERT_ENVELOPE_V2`` defaults to false and nothing on
the hot path calls ``build_alert`` yet; the HITL/BROADCAST nodes still call
``services/composition.compose_sms`` / ``compose_email`` directly. The v1 contract this
module must honour, pinned by ``tests/unit/test_alerts_envelope.py::test_v1_fidelity_*``,
is that a renderer working only from the envelope reproduces those two composers **byte
for byte**. That is why:

* ``rendering.email.subject`` is precomputed here from the same expression
  ``compose_email`` uses (site type and operator display name are not envelope fields);
* the v1 SMS/email template is ``site_down_alert@1`` — today's wording, em dash included,
  hence ``encoding="UCS2"``; §6.7's GSM-7 wording is ``@2`` and belongs to the renderer wave;
* the audiences keep today's config names on the way out (``FIELD_ENGINEER`` ↔ ``FE``,
  see ``v1_audience_name``) so the ``BroadcastRow.audience`` strings the golden test pins
  can be written unchanged.

``build_alert`` is read-only on the row: it never assigns, flushes or commits.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import Callable

from noc_agents.config import OperatorConfig
from noc_agents.db.models import IncidentRow
from noc_agents.domain.alerts import (
    AlertStatus,
    Area,
    Audience,
    AudienceSpec,
    Channel,
    Classification,
    Content,
    EmailRendering,
    Facts,
    Governance,
    IncidentRef,
    Language,
    Lifecycle,
    MsgType,
    NocAlert,
    Rendering,
    Scope,
    Severity,
    SmsRendering,
    Timing,
    Urgency,
)
from noc_agents.domain.enums import Priority
from noc_agents.services.clock import utcnow
from noc_agents.services.composition import needs_hitl, region_label
from noc_agents.services.validators import content_fallback_reason, content_names, validate_content

log = logging.getLogger(__name__)

__all__ = [
    "AUDIENCE_BY_CONFIG_NAME",
    "CONFIG_NAME_BY_AUDIENCE",
    "EXTERNAL_AUDIENCES",
    "FLAG_NAME",
    "V1_INSTRUCTION",
    "V1_SMS_MAX_SEGMENTS",
    "V1_TEMPLATE_KEY",
    "V1_TEMPLATE_VERSION",
    "VALIDATE_AI_CONTENT",
    "alert_envelope_v2_enabled",
    "assignee_role_token",
    "build_alert",
    "channels_leave_kenya",
    "contains_personal_data",
    "content_validation_context",
    "default_audiences",
    "default_content",
    "default_sender",
    "email_subject",
    "event_for",
    "lifecycle_for",
    "next_update_due",
    "note_interval_minutes",
    "severity_for",
    "urgency_for",
    "v1_audience_name",
]

# --------------------------------------------------------------------------- the flag

FLAG_NAME = "ALERT_ENVELOPE_V2"
_TRUE = {"1", "true", "yes", "on"}


def alert_envelope_v2_enabled() -> bool:
    """``ALERT_ENVELOPE_V2`` — default **false**; anything but an explicit truthy value is off.

    Nothing reads this yet (the HITL node is wired in a later Phase 2 wave); it lives here
    so the switch has one home and one spelling.
    """
    return (os.getenv(FLAG_NAME) or "").strip().lower() in _TRUE


# ------------------------------------------------------- the ai_content seam (§6.1, C-07)

#: Whether ``build_alert`` runs ``validators.validate_content`` over model-drafted
#: ``ai_content`` before it can reach a renderer. On a violation the envelope carries the
#: deterministic template instead, ``governance.ai_assisted`` is false, and the reason goes to
#: the caller's ``on_content_fallback`` for ``llm_calls.fallback_reason``.
#:
#: ON, and it must stay on: this is what stops a model sentence such as "due to a fibre cut" on
#: a POWER incident from reaching a renderer. It exists as a name so a test can drive the
#: pass-through path explicitly, not so a caller can switch the rule off. The template path
#: (``ai_content=None``, every caller today) never reaches the validator, so the switch cannot
#: change a single byte of what the running system sends now.
VALIDATE_AI_CONTENT = True


# ------------------------------------------------------------------ v1 template facts

V1_TEMPLATE_KEY = "site_down_alert"
V1_TEMPLATE_VERSION = "1"  # today's compose_sms / compose_email wording; §6.7's GSM-7 wording is "2"
# The closing line of compose_email, verbatim (em dash and all); the renderer appends "\n".
V1_INSTRUCTION = "Do not call NOC for routine status — update ticket / wait for next brief."
# Today's SMS is uncapped and its em dash forces UCS-2 (67 chars per concatenated segment).
# 6 is the ceiling implied by the column widths that feed compose_sms (≈385 chars), not a policy.
V1_SMS_MAX_SEGMENTS = 6

# Today's BroadcastRow.audience / cfg.broadcast.*_audiences vocabulary ↔ the §6.1 Audience literal.
AUDIENCE_BY_CONFIG_NAME: dict[str, str] = {"FIELD_ENGINEER": "FE"}
CONFIG_NAME_BY_AUDIENCE: dict[str, str] = {v: k for k, v in AUDIENCE_BY_CONFIG_NAME.items()}
# Any of these in the audience list forces a human approval (§6.1 mapping table).
EXTERNAL_AUDIENCES: frozenset[str] = frozenset({"REGULATOR", "CUSTOMER", "PUBLIC", "VENDOR_MANAGEMENT"})
_DEFAULT_AUDIENCE_NAMES = ["RNIO", "FIELD_ENGINEER"]  # the fallback the BROADCAST node uses today
_DEFAULT_CHANNELS = ["EMAIL", "SMS"]

_SEVERITY_BY_PRIORITY: dict[str, Severity] = {"P1": "EXTREME", "P2": "SEVERE", "P3": "MODERATE", "P4": "MINOR"}
_LIFECYCLE_BY_STATUS: dict[str, Lifecycle] = {
    "NEW": "INVESTIGATING",
    "TRIAGED": "INVESTIGATING",
    "TICKETED": "INVESTIGATING",
    "ASSIGNED": "INVESTIGATING",
    "AWAITING_VENDOR": "INVESTIGATING",
    "IN_PROGRESS": "INVESTIGATING",  # IDENTIFIED once msp_root_cause is filled, see lifecycle_for
    "RESTORED": "MONITORING",
    "CLOSED": "RESOLVED",
}
_PAST_STATUSES = frozenset({"RESTORED", "CLOSED"})


def v1_audience_name(audience: str) -> str:
    """Envelope audience → the string today's rows and step summaries carry (``FE`` → ``FIELD_ENGINEER``)."""
    return CONFIG_NAME_BY_AUDIENCE.get(audience, audience)


# ------------------------------------------------------------ mapping rules (§6.1 table)


def severity_for(priority: str | None) -> Severity:
    """P1→EXTREME, P2→SEVERE, P3→MODERATE, P4→MINOR; anything else UNKNOWN."""
    return _SEVERITY_BY_PRIORITY.get((priority or "").upper(), "UNKNOWN")


def urgency_for(status: str | None) -> Urgency:
    """IMMEDIATE while the incident is open; PAST once RESTORED or CLOSED."""
    return "PAST" if (status or "").upper() in _PAST_STATUSES else "IMMEDIATE"


def lifecycle_for(inc: IncidentRow) -> Lifecycle:
    """Status → lifecycle. IN_PROGRESS reads as IDENTIFIED only once the MSP has named a root cause."""
    status = (inc.status or "").upper()
    if status == "IN_PROGRESS" and (inc.msp_root_cause or "").strip():
        return "IDENTIFIED"
    return _LIFECYCLE_BY_STATUS.get(status, "INVESTIGATING")


def event_for(inc: IncidentRow) -> str:
    """CAP ``event``: SITE_DOWN when service affecting, DEGRADED otherwise.

    The alarm code itself travels in ``incident.fingerprint``; a finer vocabulary
    (POWER_FAIL, FIBRE_CUT, …) is an open product decision, see the wave report.
    """
    return "SITE_DOWN" if _service_affecting(inc) else "DEGRADED"


def note_interval_minutes(inc: IncidentRow, cfg: OperatorConfig) -> int:
    """``sla_minutes[P].note_interval × region_sla_note_multiplier[region]``, floor 5 min.

    The same cadence ``services/worklog_monitor`` uses for its chase window (pinned equal
    by test); the YAML is the authority.
    """
    band = cfg.sla_minutes.get((inc.priority or "").upper())
    base = band.note_interval if band else 60
    mult = float((cfg.region_sla_note_multiplier or {}).get((inc.region_code or "").upper(), 1.0))
    return max(5, int(base * mult))


def next_update_due(inc: IncidentRow, cfg: OperatorConfig, *, now: datetime) -> datetime:
    """``timing.expires``: the stored ``next_update_at`` when there is one, else now + the cadence."""
    if inc.next_update_at is not None:
        return inc.next_update_at
    return now + timedelta(minutes=note_interval_minutes(inc, cfg))


def assignee_role_token(inc: IncidentRow, cfg: OperatorConfig) -> str | None:
    """A role, never a person: ``MSP-EGYPRO-POWER`` / ``FE-NBI-E-01`` / ``NOC-QUEUE``."""
    kind = (inc.assignee_type or "UNASSIGNED").upper()
    region = (inc.region_code or "").upper()
    if kind == "MSP":
        code = _msp_code(inc)
        return f"MSP-{code}-{_failure_domain(inc)}" if code else None
    if kind == "FIELD_ENGINEER":
        reg = cfg.regions.get(region)
        if reg is not None and reg.fe_oncall:
            return reg.fe_oncall
        return f"FE-{region}-ONCALL" if region else None
    if kind == "NOC":
        return "NOC-QUEUE"
    return None


def contains_personal_data(inc: IncidentRow) -> bool:
    """§6.1: true if any of assignee_name, fe_name, rnio_name, access_notes is non-empty."""
    return any((getattr(inc, field, None) or "").strip() for field in ("assignee_name", "fe_name", "rnio_name", "access_notes"))


def channels_leave_kenya(audiences: list[AudienceSpec]) -> bool:
    """Whether any selected channel is served from outside Kenya.

    Nothing in the config or the envelope says where a channel is served from today
    (email is the operator's own relay; SMS and WhatsApp adapters are later phases), so
    this is False for every envelope built now. The hook exists so the requires_hitl
    rule reads exactly as the spec writes it and Phase 6 has one place to change.
    """
    return False


def default_sender(cfg: OperatorConfig) -> str:
    """CAP ``sender`` for the demo profiles (§6.7: ``noc.safaricom-demo.ke``). No config key yet."""
    return f"noc.{cfg.operator_id}-demo.ke"


def email_subject(inc: IncidentRow, cfg: OperatorConfig) -> str:
    """The subject ``compose_email`` writes today, without the ``Subject: `` prefix."""
    return (
        f"[{inc.priority}] {inc.incident_number} | {inc.site_name} ({inc.site_type}) | "
        f"{region_label(cfg, inc.region_code or '')} | {cfg.display_name}"
    )


def default_content(inc: IncidentRow) -> Content:
    """The deterministic (non-LLM) content block: today's title / narrative / closing line."""
    return Content(
        headline=_cap(inc.title or "", 160),
        body=_cap(inc.narrative or "", 2000),
        instruction=V1_INSTRUCTION,
    )


def default_audiences(inc: IncidentRow, cfg: OperatorConfig) -> list[AudienceSpec]:
    """The audience list the BROADCAST node uses today (``cfg.broadcast.<p>_audiences``), every
    audience on every configured channel, with config-path recipient refs (never addresses)."""
    broadcast = cfg.broadcast or {}
    names = broadcast.get(f"{(inc.priority or '').lower()}_audiences") or list(_DEFAULT_AUDIENCE_NAMES)
    channels = [str(c).upper() for c in (broadcast.get("channels") or _DEFAULT_CHANNELS)]
    region = (inc.region_code or "").upper()
    specs: list[AudienceSpec] = []
    for raw in names:
        name = str(raw).upper()
        audience = AUDIENCE_BY_CONFIG_NAME.get(name, name)
        specs.append(
            AudienceSpec(
                audience=audience,  # type: ignore[arg-type]  # validated by the Audience literal
                channels=list(channels),  # type: ignore[arg-type]  # validated by the Channel literal
                language="en",
                recipients_ref=_recipients_ref(audience, region, _msp_code(inc)),
            )
        )
    return specs


# ------------------------------------------------------------------------ the builder


def build_alert(
    inc: IncidentRow,
    cfg: OperatorConfig,
    *,
    msg_type: MsgType = "ALERT",
    lifecycle: Lifecycle | None = None,
    audiences: list[AudienceSpec] | None = None,
    ai_content: dict[Language, Content] | None = None,
    sequence: int = 1,
    references: list[str] | None = None,
    status: AlertStatus = "ACTUAL",
    scope: Scope = "INTERNAL",
    sent: datetime | None = None,
    alert_id: str | None = None,
    hitl_task_id: str | None = None,
    sender: str | None = None,
    on_content_fallback: Callable[[str], None] | None = None,
    validate_ai_content: bool | None = None,
) -> NocAlert:
    """Compose the envelope for one incident (spec §6.1). Pure: reads the row, writes nothing.

    ``lifecycle`` and ``audiences`` default to the deterministic derivations
    (``lifecycle_for`` / ``default_audiences``); ``ai_content`` replaces the template
    content and flips ``governance.ai_assisted`` — IF it passes ``validate_content``. When it
    does not, the envelope keeps the deterministic template, ``ai_assisted`` stays false, and
    ``on_content_fallback(reason)`` is called with the ``llm_calls.fallback_reason`` text
    (``"content_invalid: <codes>"``); this function writes no row, so recording it is the
    caller's. ``validate_ai_content`` overrides ``VALIDATE_AI_CONTENT`` for one call.
    ``sent`` / ``alert_id`` are injectable for reproducible tests and default to now / a
    fresh uuid4.
    """
    now = sent if sent is not None else utcnow()
    audience_specs = list(audiences) if audiences is not None else default_audiences(inc, cfg)
    content = dict(ai_content) if ai_content else {"en": default_content(inc)}
    priority = (inc.priority or "").upper()

    classification = Classification(
        event=event_for(inc),
        urgency=urgency_for(inc.status),
        severity=severity_for(priority),
        certainty="OBSERVED",
        priority=priority,  # type: ignore[arg-type]  # validated by the PriorityCode literal
        lifecycle=lifecycle or lifecycle_for(inc),
    )
    timing = Timing(
        effective=now,
        onset=inc.outage_start_at or inc.failure_time,
        expires=next_update_due(inc, cfg, now=now),
        restored_at=inc.restored_at,
    )
    area = Area(
        region_code=inc.region_code or "",
        region_label=region_label(cfg, inc.region_code or ""),
        county=inc.county,
        site_id=inc.site_id or "",
        site_name=inc.site_name or "",
        site_type=inc.site_type or "BTS",
        sites_affected=_sites_affected(inc),
    )
    facts = Facts(
        users_affected=inc.users_affected if inc.users_affected is not None else 0,
        child_sites_down=inc.child_sites_down or 0,
        mpesa_risk=bool(inc.mpesa_risk),
        failure_domain=_failure_domain(inc),
        tt_category=inc.tt_category or "OTHER",
        msp_code=_msp_code(inc),
        assignee_name=inc.assignee_name,
        assignee_role_token=assignee_role_token(inc, cfg),
        radio_oem=inc.radio_oem or None,
        planned_power=False,  # planned KPLC interruptions are a Phase 3 signal
        weather_context=None,  # Phase 3 ENRICH cache
        service_affecting=_service_affecting(inc),
        services_impacted=list(inc.services_impacted),
        root_cause_hypothesis=inc.root_cause_hypothesis,
    )
    channels = {c for spec in audience_specs for c in spec.channels}
    rendering = Rendering(
        sms=SmsRendering(template_key=V1_TEMPLATE_KEY, max_segments=V1_SMS_MAX_SEGMENTS, encoding="UCS2")
        if "SMS" in channels
        else None,
        email=EmailRendering(template_key=V1_TEMPLATE_KEY, subject=email_subject(inc, cfg)) if "EMAIL" in channels else None,
        whatsapp=None,  # only reachable with WHATSAPP_ENABLED, renderer wave
    )
    personal = contains_personal_data(inc)
    requires_hitl = (
        needs_hitl(Priority(priority), cfg.autonomy_level)
        or (personal and channels_leave_kenya(audience_specs))
        or scope != "INTERNAL"
        or any(spec.audience in EXTERNAL_AUDIENCES for spec in audience_specs)
    )
    governance = Governance(
        requires_hitl=requires_hitl,
        hitl_task_id=hitl_task_id,
        # The auto-send path records the autonomy policy as approver, exactly as the BROADCAST
        # node stamps its outbox rows today (approved_by=f"policy:{cfg.autonomy_level}").
        approved_by=None if requires_hitl else f"policy:{cfg.autonomy_level}",
        approved_at=None if requires_hitl else now,
        contains_personal_data=personal,
        ai_assisted=bool(ai_content),
        template_version=V1_TEMPLATE_VERSION,
    )
    alert = NocAlert(
        alert_id=alert_id or str(uuid.uuid4()),
        sender=sender or default_sender(cfg),
        sent=now,
        status=status,
        msg_type=msg_type,
        references=list(references or []),
        scope=scope,
        sequence=sequence,
        incident=IncidentRef(id=inc.id, incident_number=inc.incident_number, fingerprint=inc.correlation_fingerprint or ""),
        classification=classification,
        timing=timing,
        area=area,
        facts=facts,
        content=content,
        audiences=audience_specs,
        rendering=rendering,
        governance=governance,
        idempotency_seed=f"{inc.id}|{msg_type}|{sequence}",
    )
    if not ai_content:
        return alert  # the template path — every caller today — is exactly what it was
    enforce = VALIDATE_AI_CONTENT if validate_ai_content is None else validate_ai_content
    return _vet_ai_content(alert, inc, enforce=enforce, on_content_fallback=on_content_fallback)


def content_validation_context(inc: IncidentRow) -> dict:
    """What ``validate_content`` needs from the ROW that the envelope does not carry.

    * the names a draft must not contain: ``fe_name`` and ``rnio_name`` (with
      ``assignee_name``, the fields ``redaction.PSEUDONYMISED`` turns into ``<PERSON_n>`` before
      a model sees them) and ``restored_by``. The assignee counts as a person only for a
      person assignment; an MSP or NOC queue name is matched whole (``validators.content_names``);
    * ``quoted``: the severity engine's rationale. ``ticket.py`` writes the same string into
      ``severity_rationale`` and the template narrative, and it names intermediate priorities by
      construction (``users=3200→P4; site_type=HUB floor=P2; final=P2``), so a draft that
      repeats it verbatim is not stating a second priority. It also keeps a stale ``final=P2``
      harmless after a HITL priority override, since the narrative is never rewritten.
    """
    people, whole_names = content_names(
        assignee_name=inc.assignee_name,
        assignee_is_person=(inc.assignee_type or "").upper() not in ("MSP", "NOC"),
        msp_code=_msp_code(inc),
        people=(inc.fe_name, inc.rnio_name, inc.restored_by),
    )
    rationale = (inc.severity_rationale or "").strip()
    return {"people": people, "whole_names": whole_names, "quoted": (rationale,) if rationale else ()}


def _vet_ai_content(
    alert: NocAlert, inc: IncidentRow, *, enforce: bool, on_content_fallback: Callable[[str], None] | None
) -> NocAlert:
    """§6.1: model-drafted content is judged on the COMPLETE envelope, before any renderer.

    It has to be the complete envelope, not the content alone, because the rules are about
    agreement with the deterministic fields: the incident number, priority, region label and
    next-update time the content must repeat, and the ``facts.failure_domain`` any stated cause
    must name. A draft that says "due to a fibre cut" on a POWER incident is a model inventing a
    root cause in a sentence addressed to customers and the regulator; it is replaced by the
    template here, where the envelope is built, so no later path can render it.

    The fallback is ``alert`` with ``content`` and ``governance.ai_assisted`` swapped back to
    exactly what ``build_alert(..., ai_content=None)`` produces — every other field was derived
    from the row, not from the draft, and is kept.
    """
    if not enforce:
        return alert
    problems = validate_content(alert, **content_validation_context(inc))
    if not problems:
        return alert
    reason = content_fallback_reason(problems)
    if on_content_fallback is not None:
        on_content_fallback(reason)
    else:  # nobody to record it in llm_calls: at least the log says the draft was refused
        log.warning("build_alert: ai_content for %s refused, template used (%s)", inc.incident_number, reason)
    return alert.model_copy(
        update={
            "content": {"en": default_content(inc)},
            "governance": alert.governance.model_copy(update={"ai_assisted": False}),
        }
    )


# ----------------------------------------------------------------------------- helpers


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


def _service_affecting(inc: IncidentRow) -> bool:
    return True if inc.service_affecting is None else bool(inc.service_affecting)


def _failure_domain(inc: IncidentRow) -> str:
    return (inc.failure_domain or "UNKNOWN").upper()


def _msp_code(inc: IncidentRow) -> str | None:
    code = inc.msp_name or inc.responsible_msp
    return code.strip().upper() if code and code.strip() else None


def _sites_affected(inc: IncidentRow) -> list[str]:
    """The site plus any recorded child sites, de-duplicated, order kept."""
    sites = [inc.site_id] if inc.site_id else []
    try:
        children = json.loads(inc.child_site_ids_json or "[]")
    except (TypeError, ValueError):
        children = []
    for child in children if isinstance(children, list) else []:
        if isinstance(child, str) and child and child not in sites:
            sites.append(child)
    return sites


def _recipients_ref(audience: str, region: str, msp_code: str | None) -> str:
    """A config path, never an address (§6.1). Resolved by the dispatcher only."""
    if audience == "RNIO":
        return f"regions.{region}.rnio"
    if audience == "FE":
        return f"regions.{region}.fe_oncall"
    if audience == "MSP" and msp_code:
        return f"msp_contacts.{msp_code}"
    return f"audiences.{audience}"
