"""WhatsApp renderer — PRESENT, DRAFT-ONLY, and reachable only behind ``WHATSAPP_ENABLED``.

**Nothing sends a WhatsApp message before Phase 6.** ``WHATSAPP_ENABLED`` defaults to false and
with it off every call returns ``SUPPRESSED whatsapp_disabled`` without building anything. With
it on, the renderer builds the Meta Cloud API template payload of §6.7 from
``rendering.whatsapp`` (template name, language code, NAMED parameters) into
``provider_params`` so the HITL card can show the draft — and still returns ``SUPPRESSED``:

* ``no_approved_template`` — §6.2 requires a ``message_templates`` row with
  ``channel=WHATSAPP`` and ``approval_status=APPROVED`` for ``(event, lifecycle, language)``;
  the ``TemplateRegistry`` that reads that table is a later wave, so approval cannot be
  confirmed here and is never assumed;
* ``no_opt_in`` — the recipient's live opt-in (``opt_in_register``, §7.9.4) is Phase 6.

``registry`` and ``register`` are the seams those waves fill; they are accepted for the §6.2
signature and not consulted. There is no outbox transmitter for WHATSAPP either
(``orchestrator/outbox.py:_TRANSMITTERS``), so even a hand-built OK payload could not leave.

The Meta body text is not in the envelope (it lives in Meta Business Manager and, later, in
``message_templates.body``), so ``body`` is a parameter listing for the card, not the message.
``to`` is deliberately absent from ``provider_params``: recipients are resolved in the
dispatcher from ``recipient_ref``, never carried on a payload (§6.1).
"""

from __future__ import annotations

from noc_agents.domain.alerts import AudienceSpec, NocAlert
from noc_agents.services.render import (
    ChannelPayload,
    audience_for,
    env_flag,
    idempotency_key,
    resolve_language,
    warning,
)
from noc_agents.services.validators import validate_whatsapp

__all__ = ["WHATSAPP_FLAG", "render_whatsapp", "whatsapp_enabled"]

WHATSAPP_FLAG = "WHATSAPP_ENABLED"


def whatsapp_enabled() -> bool:
    """``WHATSAPP_ENABLED`` — default **false**; anything but an explicit truthy value is off."""
    return env_flag(WHATSAPP_FLAG, False)


def _template_components(alert: NocAlert) -> dict:
    """The §6.7 Meta template object: NAMED body parameters in envelope order, no recipient."""
    wa = alert.rendering.whatsapp
    assert wa is not None
    return {
        "messaging_product": "whatsapp",
        "type": "template",
        "template": {
            "name": wa.template_name,
            "language": {"code": wa.language_code},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "parameter_name": name, "text": value} for name, value in wa.params.items()
                    ],
                }
            ],
        },
    }


def render_whatsapp(
    alert: NocAlert,
    aud: AudienceSpec | None = None,
    registry: object | None = None,
    register: object | None = None,
) -> ChannelPayload:
    """Draft the WhatsApp template payload for one audience; always SUPPRESSED until Phase 6."""
    spec = audience_for(alert, "WHATSAPP", aud)
    language, fallback, warnings = resolve_language(spec)
    wa = alert.rendering.whatsapp
    template_key = wa.template_name if wa is not None else ""
    template_version = alert.governance.template_version
    common = dict(
        channel="WHATSAPP",
        audience=spec.audience,
        language=language,
        language_fallback=fallback,
        recipient_ref=spec.recipients_ref,
        idempotency_key=idempotency_key(alert, "WHATSAPP", spec.audience, spec.recipients_ref, template_key, template_version),
        template_key=template_key,
        template_version=template_version,
    )

    if not whatsapp_enabled():
        return ChannelPayload(
            **common,
            body="",
            status="SUPPRESSED",
            suppress_reason="whatsapp_disabled",
            warnings=warnings + [warning("whatsapp_disabled", f"{WHATSAPP_FLAG} is off; WhatsApp is draft-only until Phase 6")],
        )
    if wa is None:
        return ChannelPayload(
            **common,
            body="",
            status="SUPPRESSED",
            suppress_reason="no_approved_template",
            warnings=warnings + [warning("no_approved_template", "envelope carries no rendering.whatsapp block")],
        )

    body = f"{wa.template_name} [{wa.language_code}] " + "; ".join(f"{name}={value}" for name, value in wa.params.items())
    result = validate_whatsapp(body, params=wa.params, required_params=wa.params.keys())
    warnings.extend(str(w) for w in result.warnings)
    warnings.append(warning("whatsapp_draft_only", "template approval and opt-in cannot be confirmed before Phase 6; draft shown, nothing sent"))
    return ChannelPayload(
        **common,
        body=body,
        provider_params=_template_components(alert),
        status="SUPPRESSED",
        suppress_reason=result.suppress_reason or "no_approved_template",
        warnings=warnings,
    )
