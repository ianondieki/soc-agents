"""Email renderer (§6.2): subject + body per ``rendering.email``, today's wording byte for byte.

``rendering.email.subject`` is precomputed by ``build_alert`` from the same expression
``compose_email`` uses (operator display name and site type are not envelope fields);
``v1_email_body`` is the rest of ``compose_email`` read from the envelope, promoted from the
reference renderer in ``tests/unit/test_alerts_envelope.py``. ``email_text(payload)`` joins the
two exactly as the composer does — ``"Subject: …\\n\\n" + body`` — and that string is what the
fidelity test compares to ``compose_email(inc, cfg)`` byte for byte.

§6.2 checks are ``validators.validate_email``. Two of them — the body must carry the region
label and the next-update EAT time — cannot pass on ``site_down_alert@1`` (today's email has no
"Next update" line; the label reaches the body only through the narrative), so while
``ALERT_ENVELOPE_V2`` is off they are reported as warnings and the message stays ``OK``, the
same rule §6.2 states for the SMS em dash. With the flag on they are fatal.

The AI footer is appended whenever ``governance.ai_assisted`` (§6.2 email row: "AI footer when
ai_assisted"). ``provider_params["list_unsubscribe"]`` is a hint, not a header: §6.2 wants the
header only for external audiences, and the mailbox it would point at is dispatcher config.
"""

from __future__ import annotations

from noc_agents.domain.alerts import AudienceSpec, NocAlert
from noc_agents.services.alerts import alert_envelope_v2_enabled
from noc_agents.services.clock import fmt_eat
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
from noc_agents.services.validators import validate_email

__all__ = ["email_text", "next_update_eat", "render_email", "v1_email_body"]


def v1_email_body(alert: NocAlert) -> str:
    """Everything ``compose_email`` writes after ``"Subject: …\\n\\n"``, read from the envelope."""
    en, f = alert.content["en"], alert.facts
    return (
        f"Service affecting: {'YES' if f.service_affecting else 'NO'}\n"
        f"Est. users: {f.users_affected}\n"
        f"Services: {', '.join(f.services_impacted)}\n"
        f"Failure domain: {f.failure_domain}\n"
        f"M-PESA corridor risk: {'YES' if f.mpesa_risk else 'NO'}\n\n"
        f"Summary: {en.headline}\n\n"
        f"Narrative:\n{en.body}\n\n"
        f"Owner: {f.assignee_name}\n"
        f"Hypothesis: {f.root_cause_hypothesis}\n\n"
        f"{en.instruction}\n"
    )


def email_text(payload: ChannelPayload) -> str:
    """The single-string form the pipeline stores and ``adapters/email_smtp.parse_subject_body`` splits."""
    return f"Subject: {payload.subject}\n\n{payload.body}"


def next_update_eat(alert: NocAlert) -> str:
    """``"14:02 EAT"`` for ``timing.expires`` (operator zone via services/clock), ``""`` when unset."""
    return fmt_eat(alert.timing.expires)


def render_email(alert: NocAlert, aud: AudienceSpec | None = None, registry: object | None = None) -> ChannelPayload:
    """Render the email for one audience (the first that lists EMAIL when ``aud`` is omitted).

    ``registry`` is the TemplateRegistry seam (§6.2 signature); until that wave lands only
    ``site_down_alert@1`` renders, from code, and the argument is not consulted.
    """
    spec = audience_for(alert, "EMAIL", aud)
    rendering = alert.rendering.email
    template_key = rendering.template_key if rendering is not None else ""
    template_version = alert.governance.template_version
    language, fallback, warnings = resolve_language(spec)
    key = idempotency_key(alert, "EMAIL", spec.audience, spec.recipients_ref, template_key, template_version)

    if rendering is None or not is_v1_template(template_key, template_version):
        return ChannelPayload(
            channel="EMAIL",
            audience=spec.audience,
            language=language,
            language_fallback=fallback,
            recipient_ref=spec.recipients_ref,
            idempotency_key=key,
            template_key=template_key,
            template_version=template_version,
            body="",
            subject=rendering.subject if rendering is not None else None,
            status="SUPPRESSED",
            suppress_reason="no_template",
            warnings=warnings
            + [warning("no_template", f"email template {template_key or '<none>'}@{template_version} is not renderable from code; templates are data (§6.3)")],
        )

    subject = rendering.subject
    body = v1_email_body(alert)
    footer = ai_disclosure(alert)
    if footer:
        body = f"{body}\n{footer}\n"
        if alert.governance.approved_by is None:
            warnings.append(warning("ai_disclosure_pending", "AI footer names no approver yet; the approve re-render fills it in"))

    strict = alert_envelope_v2_enabled()
    incident_number = alert.incident.incident_number
    priority = alert.classification.priority
    region_label = alert.area.region_label or None
    next_update = next_update_eat(alert) or None
    suppress_reason: str | None
    if strict:
        result = validate_email(
            subject,
            body,
            incident_number=incident_number,
            priority=priority,
            region_label=region_label,
            next_update=next_update,
        )
        suppress_reason = result.suppress_reason
    else:
        # §6.2 fidelity rule: today's wording must not be suppressed. The v1 email carries the
        # INC number and priority in the subject line, not the body, and has no next-update
        # line; so the structural checks run as written, the four token checks are made against
        # the whole stored message, and a token missing from the *body* is reported as the
        # warning it becomes fatal on once the flag is on.
        result = validate_email(subject, body)
        whole = f"Subject: {subject}\n\n{body}"
        fatal: list[str] = []
        for label, token, code in (
            ("incident number", incident_number, "email_missing_incident_number"),
            ("priority", priority, "email_missing_priority"),
            ("region label", region_label, "email_missing_region_label"),
            ("next update (EAT)", next_update, "email_missing_next_update"),
        ):
            if not token or token in body:
                continue
            if token in whole:
                warnings.append(warning(code, f"body does not carry the {label} {token!r} (subject does; fatal once ALERT_ENVELOPE_V2 is on)"))
            elif code in ("email_missing_incident_number", "email_missing_priority"):
                fatal.append(code)  # absent from subject AND body: broken on any template
            else:
                warnings.append(warning(code, f"body does not carry the {label} {token!r} (fatal once ALERT_ENVELOPE_V2 is on)"))
        suppress_reason = result.suppress_reason or (fatal[0] if fatal else None)
    warnings.extend(str(w) for w in result.warnings)

    return ChannelPayload(
        channel="EMAIL",
        audience=spec.audience,
        language=language,
        language_fallback=fallback,
        recipient_ref=spec.recipients_ref,
        idempotency_key=key,
        template_key=template_key,
        template_version=template_version,
        body=body,
        subject=subject,
        provider_params={"list_unsubscribe": is_external(spec.audience)},
        status="SUPPRESSED" if suppress_reason else "OK",
        suppress_reason=suppress_reason,
        warnings=warnings,
    )
