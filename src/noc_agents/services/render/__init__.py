"""Per-channel renderers: one ``NocAlert`` in, one correctly formatted channel payload out (§6.2).

``services/alerts.py:build_alert`` turns an incident into the envelope; this package turns the
envelope into what each channel actually carries. A renderer reads ONLY the envelope — never
the ``IncidentRow``, the session or the config — so the same bytes come out whether it runs in
the HITL node, on the approve re-render, or from the JSON copy in ``outbox.envelope_json``.

Modules
    sms.py       ``render_sms``         body + measured encoding and segment count (services/gsm7.py)
    email.py     ``render_email``       subject + body; ``email_text`` joins them the way compose_email does
    inapp.py     ``render_inapp``       the dashboard card (§6.7 key set), serialised JSON in ``body``
    ledger.py    ``render_ledger_row``  the ShiftLedgerRow dict; ``render_ledger_cells`` reuses services/ledger.py
    whatsapp.py  ``render_whatsapp``    draft-only, behind ``WHATSAPP_ENABLED`` (default false); Phase 6 sends

**The v1 fidelity rule (§6.2).** With ``ALERT_ENVELOPE_V2`` off (the default) the envelope carries
``site_down_alert@1`` — today's wording — and ``render_sms`` / ``render_email`` reproduce
``services/composition.py:compose_sms`` / ``compose_email`` **byte for byte**, pinned by
``tests/unit/test_alert_renderers.py::test_v1_fidelity_*``. Consequences that look odd but are
deliberate:

* the v1 SMS template's em dash forces UCS-2. With the flag off the validator *reports* it (the
  payload's ``warnings``) and the message stays ``OK``; with the flag on the same body is
  ``SUPPRESSED`` — flipping the flag before the GSM-7 ``@2`` template exists fails closed;
* the v1 email carries no "Next update … EAT" line, so §6.2's next-update / region-label checks
  are warnings with the flag off and fatal with it on, for the same reason;
* only ``site_down_alert@1`` is renderable from code, because that template *is* today's
  composer. Any other (key, version) is ``SUPPRESSED no_template``: templates are data
  (``message_templates``, §6.3) and the TemplateRegistry wave serves them. The ``registry``
  parameter each renderer accepts is that seam; it is unused until then.

Every verdict comes from ``services/validators.py``. A fatal finding sets ``status="SUPPRESSED"``
and ``suppress_reason``; the payload is still returned, body and all, so the HITL card can show
*what* was refused and *why*. Nothing here sends anything.
"""

from __future__ import annotations

import hashlib
import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from noc_agents.domain.alerts import Audience, AudienceSpec, Channel, Language, NocAlert
from noc_agents.services.alerts import EXTERNAL_AUDIENCES, V1_TEMPLATE_KEY, V1_TEMPLATE_VERSION

__all__ = [
    "AI_DISCLOSURE",
    "AI_REVIEWER_PENDING",
    "ChannelPayload",
    "ai_disclosure",
    "audience_for",
    "env_flag",
    "idempotency_key",
    "is_v1_template",
    "next_update_iso",
    "render_alert",
    "render_email",
    "render_for_audience",
    "render_inapp",
    "render_ledger_cells",
    "render_ledger_row",
    "render_sms",
    "render_whatsapp",
    "resolve_language",
    "warning",
]

PayloadStatus = Literal["OK", "SUPPRESSED"]

# §6.2 AI disclosure row, verbatim. The approver of record is named on the artefact; while a
# HITL task is still open there is no approver yet, and the approve path re-renders (§6.5),
# so the draft the approver sees says so instead of naming nobody.
AI_DISCLOSURE = "Drafted with AI assistance; reviewed by {reviewer}."
AI_REVIEWER_PENDING = "pending approval"


class ChannelPayload(BaseModel):
    """What one channel carries for one audience (§6.2), with no address in it.

    ``recipient_ref`` is a config path; the dispatcher resolves it. ``warnings`` is the one
    additive field beyond the §6.2 listing: §6.2 says a UCS-2 v1 body is *reported* while the
    flag is off, and a report needs somewhere to live on the payload the approver reads.
    """

    model_config = ConfigDict(extra="forbid")
    channel: Channel
    audience: Audience
    language: Language  # what the audience asked for
    language_fallback: Language | None = None  # "en" when sw was requested but unavailable/unapproved
    recipient_ref: str
    idempotency_key: str
    template_key: str
    template_version: str
    body: str
    subject: str | None = None
    provider_params: dict = Field(default_factory=dict)  # WhatsApp NAMED components; email header hints
    encoding: Literal["GSM7", "UCS2"] | None = None  # SMS only, measured
    segments: int | None = None  # SMS only, measured
    status: PayloadStatus
    suppress_reason: str | None = None
    warnings: list[str] = Field(default_factory=list)  # §6.2: non-fatal findings, reported not enforced

    @property
    def rendered_language(self) -> Language:
        """The language ``body`` is actually in."""
        return self.language_fallback or self.language

    @property
    def ok(self) -> bool:
        return self.status == "OK"


# ------------------------------------------------------------------------- shared helpers


def env_flag(name: str, default: bool = False) -> bool:
    """A boolean environment switch: anything but an explicit truthy value is ``default``."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def audience_for(alert: NocAlert, channel: Channel, aud: AudienceSpec | None = None) -> AudienceSpec:
    """The audience a channel renders for: the one given, else the first that lists the channel.

    §6.7.2: a channel no audience selected is *not rendered* (not "suppressed"), so asking for
    one is a caller error and raises ``LookupError`` rather than inventing a recipient.
    """
    if aud is not None:
        if channel not in aud.channels:
            raise LookupError(f"audience {aud.audience} does not list channel {channel} (§6.7.2: not rendered)")
        return aud
    for spec in alert.audiences:
        if channel in spec.channels:
            return spec
    raise LookupError(f"no audience on alert {alert.alert_id} lists channel {channel} (§6.7.2: not rendered)")


def idempotency_key(alert: NocAlert, channel: str, audience: str, recipient_ref: str, template_key: str, template_version: str) -> str:
    """§6.2: ``sha256(f"{seed}|{channel}|{audience}|{recipient_ref}|{template_key}@{template_version}")[:40]``."""
    raw = f"{alert.idempotency_seed}|{channel}|{audience}|{recipient_ref}|{template_key}@{template_version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def is_v1_template(template_key: str | None, template_version: str | None) -> bool:
    """True for ``site_down_alert@1`` — the only template rendered from code (it is today's composer)."""
    return template_key == V1_TEMPLATE_KEY and template_version == V1_TEMPLATE_VERSION


def warning(code: str, message: str) -> str:
    """Same shape as ``str(validators.Violation)`` so a card can list both kinds in one column."""
    return f"[WARNING] {code}: {message}"


def resolve_language(spec: AudienceSpec) -> tuple[Language, Language | None, list[str]]:
    """``(language, language_fallback, warnings)`` for an audience under the v1 template.

    ``site_down_alert@1`` exists in English only, and §6.4 forbids sending Kiswahili that a named
    reviewer has not approved, so a ``sw`` request renders English and says so. Kiswahili
    rendering arrives with the template registry (D4), not by trusting ``content["sw"]``.
    """
    if spec.language == "en":
        return "en", None, []
    return (
        spec.language,
        "en",
        [warning("language_fallback", f"{spec.language} requested; {V1_TEMPLATE_KEY}@{V1_TEMPLATE_VERSION} is English-only, rendered en")],
    )


def ai_disclosure(alert: NocAlert) -> str | None:
    """The §6.2 footer when ``governance.ai_assisted``, else None."""
    if not alert.governance.ai_assisted:
        return None
    return AI_DISCLOSURE.format(reviewer=alert.governance.approved_by or AI_REVIEWER_PENDING)


def is_external(audience: str) -> bool:
    return audience in EXTERNAL_AUDIENCES


def next_update_iso(alert: NocAlert) -> str | None:
    """``timing.expires`` exactly as the envelope JSON spells it (aware UTC, ``Z``), or None."""
    return alert.timing.model_dump(mode="json")["expires"]


# ----------------------------------------------------------------------- the renderers
# Imported last: each module needs ChannelPayload and the helpers above, so the package must be
# bound before they load (a partially initialised package still exposes the names already set).

from noc_agents.services.render.email import render_email  # noqa: E402
from noc_agents.services.render.inapp import render_inapp  # noqa: E402
from noc_agents.services.render.ledger import render_ledger_cells, render_ledger_row  # noqa: E402
from noc_agents.services.render.sms import render_sms  # noqa: E402
from noc_agents.services.render.whatsapp import render_whatsapp  # noqa: E402

_PAYLOAD_RENDERERS = {
    "SMS": render_sms,
    "EMAIL": render_email,
    "INAPP": render_inapp,
    "WHATSAPP": render_whatsapp,
}


def render_for_audience(alert: NocAlert, aud: AudienceSpec) -> list[ChannelPayload]:
    """One payload per channel the audience lists, in the audience's order.

    LEDGER / ICS / STATUSPAGE are not per-audience payloads (the ledger row is one per alert,
    see ``render_ledger_row``) and are skipped here rather than suppressed.
    """
    payloads: list[ChannelPayload] = []
    for channel in aud.channels:
        renderer = _PAYLOAD_RENDERERS.get(channel)
        if renderer is not None:
            payloads.append(renderer(alert, aud))
    return payloads


def render_alert(alert: NocAlert) -> list[ChannelPayload]:
    """Every (audience, channel) payload for the alert, audiences in envelope order."""
    return [payload for aud in alert.audiences for payload in render_for_audience(alert, aud)]
