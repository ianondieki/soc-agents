"""SMS renderer (§6.2): today's wording, measured by ``services/gsm7.py``, gated on segments.

The body for ``site_down_alert@1`` is ``services/composition.py:compose_sms`` re-read from the
envelope — the same expression, character for character, promoted from the reference renderer
in ``tests/unit/test_alerts_envelope.py`` that proved the envelope carries everything today's
message needs. It is not "the SMS template" in any abstract sense; it is a byte contract.

What this module adds on top of the string:

* **measurement, never a guess** — ``encoding`` and ``segments`` on the payload come from
  ``gsm7.sms_cost`` (ESC pairs cost two septets, pairs never straddle a part), so the number
  the approver sees is the number the SMSC bills;
* **the segment gate** — a body needing more than ``rendering.sms.max_segments`` parts is
  ``SUPPRESSED sms_too_many_segments``. An alert that silently becomes four segments costs
  four times as much and can arrive out of order; refusing it is cheaper than sending it;
* **the flag-off exemption** — the v1 template's em dash forces UCS-2 (67 chars per part). With
  ``ALERT_ENVELOPE_V2`` off that is a warning on the payload (§6.2: "the validator only
  reports it; it must not suppress today's messages"); with the flag on it is fatal. The switch
  lives in ``validators.validate_sms`` and is read there, so there is one spelling of it;
* **the AI footer, external audiences only** — §6.2's AI-disclosure row names REGULATOR /
  CUSTOMER / PUBLIC / VENDOR_MANAGEMENT. On an internal SMS the footer would cost a segment for
  no policy gain, so it is not appended there. If the footer pushes an external message over
  ``max_segments`` the gate refuses it — the right outcome for a disclosure that must not be
  dropped to make room.
"""

from __future__ import annotations

from noc_agents.domain.alerts import AudienceSpec, NocAlert
from noc_agents.services.render import (
    ChannelPayload,
    ai_disclosure,
    audience_for,
    idempotency_key,
    is_external,
    is_v1_template,
    resolve_language,
    warning,
)
from noc_agents.services.validators import validate_sms

__all__ = ["render_sms", "v1_sms_body"]


def v1_sms_body(alert: NocAlert) -> str:
    """``compose_sms`` read from the envelope. Byte-identical to today's message; do not "tidy" it."""
    c, f, a, ref = alert.classification, alert.facts, alert.area, alert.incident
    return (
        f"[{c.priority}] {ref.incident_number} {a.site_id} {a.region_code}\n"
        f"{f.failure_domain}|est.users {f.users_affected}\n"
        f"{alert.content['en'].headline[:80]}\n"
        # Hyphen, not an em dash: this is the last line of every outgoing SMS and a single
        # U+2014 forces the whole message from GSM-7 (160 chars/segment) into UCS-2 (70),
        # turning the standard site-down alert into 3 segments instead of 1 -- triple the
        # cost per recipient, and multipart SMS can arrive out of order. Measured with
        # services/gsm7.py. Changed on the owner's explicit approval (it alters bytes that
        # reach customers); template @2 makes the same substitution.
        f"Owner:{f.assignee_name} - ticket notes for updates"
    )


def render_sms(alert: NocAlert, aud: AudienceSpec | None = None, registry: object | None = None) -> ChannelPayload:
    """Render the SMS for one audience (the first that lists SMS when ``aud`` is omitted).

    ``registry`` is the TemplateRegistry seam (§6.2 signature); until that wave lands only
    ``site_down_alert@1`` renders, from code, and the argument is not consulted.
    """
    spec = audience_for(alert, "SMS", aud)
    rendering = alert.rendering.sms
    template_key = rendering.template_key if rendering is not None else ""
    template_version = alert.governance.template_version
    language, fallback, warnings = resolve_language(spec)
    key = idempotency_key(alert, "SMS", spec.audience, spec.recipients_ref, template_key, template_version)

    if rendering is None or not is_v1_template(template_key, template_version):
        return ChannelPayload(
            channel="SMS",
            audience=spec.audience,
            language=language,
            language_fallback=fallback,
            recipient_ref=spec.recipients_ref,
            idempotency_key=key,
            template_key=template_key,
            template_version=template_version,
            body="",
            status="SUPPRESSED",
            suppress_reason="no_template",
            warnings=warnings
            + [warning("no_template", f"SMS template {template_key or '<none>'}@{template_version} is not renderable from code; templates are data (§6.3)")],
        )

    body = v1_sms_body(alert)
    footer = ai_disclosure(alert) if is_external(spec.audience) else None
    if footer:
        body = f"{body}\n{footer}"
        if alert.governance.approved_by is None:
            warnings.append(warning("ai_disclosure_pending", "AI footer names no approver yet; the approve re-render fills it in"))

    result = validate_sms(
        body,
        incident_number=alert.incident.incident_number,
        priority=alert.classification.priority,
        max_segments=rendering.max_segments,
        enforce_encoding=None,  # the validator reads ALERT_ENVELOPE_V2: off = report, on = suppress
    )
    cost = result.cost
    assert cost is not None  # validate_sms always measures
    if rendering.encoding == "GSM7" and cost.encoding != "GSM7":
        warnings.append(
            warning("sms_encoding_mismatch", f"envelope declares GSM7 but the body measures {cost.encoding} ({cost.units} units)")
        )
    warnings.extend(str(w) for w in result.warnings)

    return ChannelPayload(
        channel="SMS",
        audience=spec.audience,
        language=language,
        language_fallback=fallback,
        recipient_ref=spec.recipients_ref,
        idempotency_key=key,
        template_key=template_key,
        template_version=template_version,
        body=body,
        encoding=cost.encoding,
        segments=cost.segments,
        status="OK" if result.ok else "SUPPRESSED",
        suppress_reason=result.suppress_reason,
        warnings=warnings,
    )
