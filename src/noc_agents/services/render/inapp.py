"""In-app renderer (§6.2, §6.7): the dashboard / inbox card, JSON-serialisable with a pinned key set.

The card is the §6.7 ``broadcast.queued`` payload::

    {incident_number, alert_id, priority, lifecycle, headline, region_code,
     next_update_at, channels, requires_hitl}

``channels`` is the union of every audience's channels in first-appearance order (§6.7.1 lists
``["SMS", "WHATSAPP", "EMAIL", "INAPP"]`` for RNIO→FE→MSP→MANAGEMENT; §6.7.2 ``["SMS", "INAPP"]``).
``next_update_at`` is ``timing.expires`` spelled exactly as the envelope JSON spells it.

The key set is data the UI binds to, so it is fixed here (``INAPP_KEYS``) and asserted by the
renderer test; §6.2 says the same set is pinned in ``tests/system/test_contracts.py`` once the
WS event exists. The AI disclosure is not a card key: the card travels with its envelope, whose
``governance.ai_assisted`` the inbox already shows, and adding a key would move the pinned set.

``ChannelPayload.body`` carries the serialised card (sorted keys, UTF-8 preserved) so the
generic outbox/broadcast columns can store it like any other body; ``inapp_card`` returns the
dict for callers that publish it as a realtime event.
"""

from __future__ import annotations

import json
from typing import Any

from noc_agents.domain.alerts import AudienceSpec, NocAlert
from noc_agents.services.alerts import V1_TEMPLATE_KEY
from noc_agents.services.render import ChannelPayload, audience_for, idempotency_key, next_update_iso, resolve_language
from noc_agents.services.validators import validate_inapp

__all__ = ["INAPP_KEYS", "inapp_card", "render_inapp"]

INAPP_KEYS: tuple[str, ...] = (
    "incident_number",
    "alert_id",
    "priority",
    "lifecycle",
    "headline",
    "region_code",
    "next_update_at",
    "channels",
    "requires_hitl",
)


def _channels_in_order(alert: NocAlert) -> list[str]:
    seen: list[str] = []
    for spec in alert.audiences:
        for channel in spec.channels:
            if channel not in seen:
                seen.append(channel)
    return seen


def inapp_card(alert: NocAlert, language: str = "en") -> dict[str, Any]:
    """The §6.7 card dict. ``language`` selects the headline; ``en`` is always present."""
    content = alert.content.get(language) or alert.content["en"]  # type: ignore[call-overload]
    return {
        "incident_number": alert.incident.incident_number,
        "alert_id": alert.alert_id,
        "priority": alert.classification.priority,
        "lifecycle": alert.classification.lifecycle,
        "headline": content.headline,
        "region_code": alert.area.region_code,
        "next_update_at": next_update_iso(alert),
        "channels": _channels_in_order(alert),
        "requires_hitl": alert.governance.requires_hitl,
    }


def render_inapp(alert: NocAlert, aud: AudienceSpec | None = None) -> ChannelPayload:
    """Render the card for one audience (the first that lists INAPP when ``aud`` is omitted)."""
    spec = audience_for(alert, "INAPP", aud)
    language, fallback, warnings = resolve_language(spec)
    template_key, template_version = V1_TEMPLATE_KEY, alert.governance.template_version
    card = inapp_card(alert, fallback or language)
    result = validate_inapp(card, required_keys=INAPP_KEYS)
    warnings.extend(str(w) for w in result.warnings)
    return ChannelPayload(
        channel="INAPP",
        audience=spec.audience,
        language=language,
        language_fallback=fallback,
        recipient_ref=spec.recipients_ref,
        idempotency_key=idempotency_key(alert, "INAPP", spec.audience, spec.recipients_ref, template_key, template_version),
        template_key=template_key,
        template_version=template_version,
        body=json.dumps(card, ensure_ascii=False, sort_keys=True),
        status="OK" if result.ok else "SUPPRESSED",
        suppress_reason=result.suppress_reason,
        warnings=warnings,
    )
