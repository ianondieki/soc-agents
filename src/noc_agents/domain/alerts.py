"""NocAlert v1 — the canonical message envelope every outbound NOC notification is built from.

Spec §6.1, followed field for field. The envelope is a *pure data model*: it holds what
an alert says (classification, timing, area, facts, content), who it goes to (audiences),
how each channel renders it (rendering) and who may release it (governance). It contains
no addresses, no secrets and no rendering logic. ``services/alerts.py:build_alert`` fills
every field deterministically from an ``IncidentRow`` plus the operator config; the
renderers (``services/render/*``, a later wave) turn one envelope into channel payloads.

Design rules pinned here:

* ``extra="forbid"`` on every model: an unknown key is a bug, never silently dropped.
* Every datetime is **aware UTC**. The database keeps naive UTC (the storage contract,
  see ``services/clock.py``); the ``UtcDateTime`` type stamps ``tzinfo=UTC`` on the way
  in so ``model_dump(mode="json")`` renders ``2026-09-16T10:47:10Z`` (spec §7.0.6) and
  a round trip through ``outbox.envelope_json`` is lossless.
* ``content["en"]`` is mandatory; other languages are optional additions.

Three fields go beyond the §6.1 listing, all additive and all required by the v1
fidelity rule (the envelope must reproduce today's ``compose_email`` byte for byte and
the §6.7 email rendering shows the same lines): ``Area.site_type``,
``Facts.service_affecting``, ``Facts.services_impacted`` and ``Facts.root_cause_hypothesis``.
They are marked ``# v1 fidelity`` below.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = 1

AlertStatus = Literal["ACTUAL", "EXERCISE", "TEST", "DRAFT"]
MsgType = Literal["ALERT", "UPDATE", "CANCEL", "ACK", "ERROR"]
Scope = Literal["INTERNAL", "RESTRICTED", "PUBLIC"]
Urgency = Literal["IMMEDIATE", "EXPECTED", "FUTURE", "PAST", "UNKNOWN"]
Severity = Literal["EXTREME", "SEVERE", "MODERATE", "MINOR", "UNKNOWN"]
Certainty = Literal["OBSERVED", "LIKELY", "POSSIBLE", "UNLIKELY", "UNKNOWN"]
Lifecycle = Literal["INVESTIGATING", "IDENTIFIED", "MONITORING", "RESOLVED"]
Channel = Literal["EMAIL", "SMS", "WHATSAPP", "INAPP", "LEDGER", "ICS", "STATUSPAGE"]
Language = Literal["en", "sw"]
Audience = Literal[
    "RNIO",
    "FE",
    "MSP",
    "MANAGEMENT",
    "NOC_SHIFT",
    "PLANNING",
    "VENDOR_MANAGEMENT",
    "REGULATOR",
    "CUSTOMER",
    "PUBLIC",
]
PriorityCode = Literal["P1", "P2", "P3", "P4"]
RedactionProfile = Literal["none", "role_tokens", "full"]


def _as_utc(dt: datetime) -> datetime:
    """Naive → aware UTC (the DB contract says naive means UTC); aware → converted to UTC.

    Same rule as ``services/clock.to_utc``; duplicated here because the domain layer must
    not import services.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


UtcDateTime = Annotated[datetime, AfterValidator(_as_utc)]


class IncidentRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    incident_number: str = Field(pattern=r"^INC\d{6}$")
    fingerprint: str


class Classification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["Infra"] = "Infra"
    event: str  # "SITE_DOWN" | "POWER_FAIL" | "FIBRE_CUT" | "DEGRADED" | ...
    urgency: Urgency
    severity: Severity
    certainty: Certainty
    priority: PriorityCode
    lifecycle: Lifecycle


class Timing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    effective: UtcDateTime  # UTC; renderers convert to EAT
    onset: UtcDateTime | None  # outage_start_at or failure_time
    expires: UtcDateTime | None  # next_update_at = now + sla_minutes[P].note_interval × region multiplier
    restored_at: UtcDateTime | None = None


class Area(BaseModel):
    model_config = ConfigDict(extra="forbid")
    region_code: str
    region_label: str
    county: str | None = None
    site_id: str
    site_name: str
    site_type: str = "BTS"  # v1 fidelity: the email subject and the ledger row carry "(HUB)"
    sites_affected: list[str] = Field(default_factory=list)


class Facts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    users_affected: int
    child_sites_down: int = 0
    mpesa_risk: bool = False
    failure_domain: str
    tt_category: str
    msp_code: str | None = None  # vendor code ("EGYPRO"), never a person
    assignee_name: str | None = None  # display name as today (personal data; drives contains_personal_data)
    assignee_role_token: str | None = None  # "FE-NBI-E-01" / "RNIO-NBI-E"; used on cross-border channels unless approved
    radio_oem: str | None = None
    planned_power: bool = False
    weather_context: str | None = None  # one deterministic sentence or None
    # v1 fidelity: today's email body prints these three lines; §6.7's rendering keeps them.
    service_affecting: bool = True
    services_impacted: list[str] = Field(default_factory=list)
    root_cause_hypothesis: str | None = None


class Content(BaseModel):  # the ONLY part an LLM may draft
    model_config = ConfigDict(extra="forbid")
    headline: str = Field(max_length=160)  # CAP headline target
    body: str = Field(max_length=2000)
    instruction: str | None = Field(default=None, max_length=500)


class NocAlertContent(BaseModel):
    """What a model may return (§6.1 "Who fills what"): English mandatory, Kiswahili optional."""

    model_config = ConfigDict(extra="forbid")
    en: Content
    sw: Content | None = None


class AudienceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    audience: Audience
    channels: list[Channel]
    language: Language = "en"
    recipients_ref: str  # config path or register id ("regions.NBI_W.rnio"); never raw addresses


class SmsRendering(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_key: str
    max_segments: int = 1
    encoding: Literal["GSM7", "UCS2"] = "GSM7"


class EmailRendering(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_key: str
    subject: str


class WhatsAppRendering(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_name: str  # Meta-approved template name
    language_code: str
    parameter_format: Literal["NAMED"] = "NAMED"
    params: dict[str, str] = Field(default_factory=dict)


class Rendering(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sms: SmsRendering | None = None
    email: EmailRendering | None = None
    whatsapp: WhatsAppRendering | None = None


class Governance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requires_hitl: bool
    hitl_task_id: str | None = None
    approved_by: str | None = None  # user id or "policy:L2_GUARDED"
    approved_at: UtcDateTime | None = None
    contains_personal_data: bool = False
    redaction_profile: RedactionProfile = "role_tokens"
    transfer_record_id: str | None = None  # AuditRow id (reg 41(2)) when a channel leaves Kenya
    ai_assisted: bool = False  # any content field came from a model
    template_version: str = "1"


class NocAlert(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    alert_id: str  # uuid4 = CAP identifier
    sender: str  # "noc.safaricom-demo.ke"
    sent: UtcDateTime
    status: AlertStatus
    msg_type: MsgType
    references: list[str] = Field(default_factory=list)  # alert_ids this supersedes
    scope: Scope
    sequence: int = 1  # per incident, monotonic
    incident: IncidentRef
    classification: Classification
    timing: Timing
    area: Area
    facts: Facts
    content: dict[Language, Content]  # "en" mandatory
    audiences: list[AudienceSpec]
    rendering: Rendering = Rendering()
    governance: Governance
    idempotency_seed: str  # f"{incident.id}|{msg_type}|{sequence}"

    @model_validator(mode="after")
    def _english_is_mandatory(self) -> NocAlert:
        if "en" not in self.content:
            raise ValueError('content["en"] is mandatory (spec §6.1)')
        return self


__all__ = [
    "SCHEMA_VERSION",
    "AlertStatus",
    "Area",
    "Audience",
    "AudienceSpec",
    "Certainty",
    "Channel",
    "Classification",
    "Content",
    "EmailRendering",
    "Facts",
    "Governance",
    "IncidentRef",
    "Language",
    "Lifecycle",
    "MsgType",
    "NocAlert",
    "NocAlertContent",
    "PriorityCode",
    "RedactionProfile",
    "Rendering",
    "Scope",
    "Severity",
    "SmsRendering",
    "Timing",
    "Urgency",
    "UtcDateTime",
    "WhatsAppRendering",
]
