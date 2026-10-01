"""Versioned, approval-aware message template registry (spec §6.3, §6.4).

Templates are **data**, not code. ``config/templates/*.yaml`` is the seed; the
``message_templates`` table is the registry; ``TemplateRegistry`` is the only thing that
writes it. Nothing here sends anything, and nothing on the hot path calls it yet — the
``registry`` parameter every renderer in ``services/render/`` already accepts is the seam
this module fills.

Why any of this exists
----------------------
A NOC has to be able to say, months later, **which exact words went out at 02:14 last
Tuesday** — to a regulator, to a customer, to its own post-incident review. That single
requirement drives every design choice below:

* **a template is never edited in place.** ``sync`` fingerprints the content it is about to
  write and compares it with the highest version already in the lineage. Same fingerprint:
  nothing is written at all (that is the idempotence). Different: the next version is
  INSERTed and the old row stays exactly as it was sent. So ``site_down_alert/SMS/en@1``
  is still readable after ``@2`` replaces it, and an outbox row that stamped ``@1`` still
  resolves to the words that actually left the building;
* **a new version is not approved by the old approval.** A changed body starts ``DRAFT``,
  whatever the YAML says, because the approval that existed described different words;
* **only ``APPROVED`` is sendable.** ``for_send`` and ``render`` raise ``TemplateNotApproved``
  for anything else. Rendering an unapproved body for a human to *look at* is fine and is
  what a HITL card does, so that is a separate, explicit ``allow_unapproved=True``;
* **``sync`` never changes the approval of a row that already exists.** The YAML approval
  block is the status a row is *created* with. Every later transition goes through
  :meth:`TemplateRegistry.set_status` — the service behind ``PUT /api/v1/templates/{id}/status``
  — so an approval made by a named human in the database can never be silently reverted by
  a redeploy of the config.

Kiswahili (§6.4)
----------------
**No ``sw`` row is seeded, on purpose.** Every template here declares its ``sw`` slot as
``translations_pending`` with the reason, so the gap is *data* the registry can report,
not a TODO comment nobody reads. Two independent reasons:

1. §6.4's hard rule: no ``sw`` template may be ``APPROVED`` until a named native reviewer
   has signed it off (role ``legal`` or ``management``, recorded in ``docs/SIGNOFF.md``).
   ``_check_approval`` enforces that at seed time, so an invented body could only ever sit
   in the table looking finished while being unsendable;
2. the messages that exist today are internal NOC traffic in a specific English register —
   "failure domain", "est.users", "M-PESA corridor risk", "ticket notes" — read by RNIOs
   and field engineers who work in English. There is no settled Kiswahili for that
   vocabulary in Kenyan telecom practice, and a wrong word reaches an engineer standing at
   a site at 02:00, or a subscriber who cannot ring back to ask what it meant.

``resolve`` still implements §6.4's fallback in full: a ``sw`` request with no approved
``sw`` row renders English and reports ``language_fallback="en"``, which is exactly what
``services/render/__init__.py:resolve_language`` does from code today.

Rendering
---------
Jinja2 ``SandboxedEnvironment`` with ``StrictUndefined`` and autoescape off (§6.3): a
missing variable **fails the render**, it never becomes a blank in a message about a
national outage. ``keep_trailing_newline=True`` because the v1 email body ends in one and
the byte contract is measured to the last character. The seeder additionally proves at
seed time that every variable a body references is declared in ``params``, and that every
declared param is one the envelope can actually supply — so ``StrictUndefined`` is a
backstop, not the plan.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml
from jinja2 import StrictUndefined, TemplateSyntaxError, meta
from jinja2.exceptions import UndefinedError
from jinja2.sandbox import SandboxedEnvironment
from sqlalchemy import select
from sqlalchemy.orm import Session

from noc_agents.config import CONFIG_DIR, OPERATORS_DIR, OperatorConfig
from noc_agents.db.models import MessageTemplateRow
from noc_agents.domain.alerts import Language, NocAlert
from noc_agents.services.clock import fmt_eat, utcnow
from noc_agents.services.gsm7 import is_gsm7

__all__ = [
    "ALLOWED_PARAMS",
    "APPROVAL_STATUSES",
    "DEFAULT_LANGUAGE",
    "HITL_NUDGE_PARAMS",
    "HITL_NUDGE_TEMPLATE_KEY",
    "REVIEWED_LANGUAGES",
    "SENDABLE_STATUSES",
    "SEED_DIR",
    "TEMPLATE_CHANNELS",
    "TEMPLATE_KEYS",
    "PendingTranslation",
    "RenderedMessage",
    "SeedTemplate",
    "SyncReport",
    "TemplateError",
    "TemplateNotApproved",
    "TemplateNotFound",
    "TemplateRegistry",
    "TemplateRenderError",
    "TemplateResolution",
    "TemplateSeedError",
    "content_fingerprint",
    "context_from_alert",
    "is_sendable",
    "load_seed_templates",
    "params_schema",
    "params_vocabulary",
    "render_body",
    "row_fingerprint",
    "seed_roots",
]

# ------------------------------------------------------------------------ vocabulary

#: Shared seed directory. ``config/operators/<op>/templates/`` (spec §6.3's path) overrides
#: it per operator and is searched second; nothing lives there today.
SEED_DIR = CONFIG_DIR / "templates"

#: §6.3's channel vocabulary. LEDGER/ICS/STATUSPAGE are not templated messages.
TEMPLATE_CHANNELS: tuple[str, ...] = ("EMAIL", "SMS", "WHATSAPP", "INAPP")

#: §6.3's ``template_key`` vocabulary, verbatim, plus the one key §6.5 names outside that
#: list: ``hitl_nudge``, the escalation ladder's internal "a decision is waiting" text.
#: Seeding a key outside this tuple is a seed error: a typo'd key silently produces a
#: template nothing will ever resolve.
TEMPLATE_KEYS: tuple[str, ...] = (
    "site_down_alert",
    "incident_update",
    "incident_restored",
    "assignment_notice",
    "chase_reminder",
    "vendor_notice",
    "handover",
    "regulatory_ca_24h",
    "maintenance_invite",
    "complaint_followup",
    "hitl_nudge",
)

#: The key whose variables are NOT envelope fields (§6.5). An unclaimed approval card may
#: have no envelope at all -- a task raised without an incident -- and the nudge must carry
#: no incident narrative, so its vocabulary is the card's, not the alert's.
HITL_NUDGE_TEMPLATE_KEY = "hitl_nudge"

#: §6.3, mirroring Meta's own template states so a WhatsApp row needs no second vocabulary.
APPROVAL_STATUSES: tuple[str, ...] = ("DRAFT", "SUBMITTED", "APPROVED", "REJECTED", "PAUSED")

#: The only status a real send may use. Everything else is refused by ``for_send``/``render``.
SENDABLE_STATUSES: frozenset[str] = frozenset({"APPROVED"})

DEFAULT_LANGUAGE: Language = "en"
LANGUAGES: tuple[str, ...] = ("en", "sw")

#: §6.4 hard rule: these languages may not reach ``APPROVED`` without a *named* human
#: reviewer and a sign-off reference. English is approved by the operator; Kiswahili needs
#: someone who can actually read it.
REVIEWED_LANGUAGES: frozenset[str] = frozenset({"sw"})
REVIEWER_ROLES: frozenset[str] = frozenset({"legal", "management"})

_JSON_TYPES: dict[str, str] = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "array": "array",
    "object": "object",
}

_SMS_ENCODINGS: tuple[str, ...] = ("GSM7", "UCS2")


# --------------------------------------------------------------------------- errors


class TemplateError(Exception):
    """Base class, so a caller can catch every registry failure in one clause."""


class TemplateSeedError(TemplateError):
    """The YAML is wrong, or asks for something §6.3/§6.4 forbids. Raised at seed time."""


class TemplateNotFound(TemplateError):
    """No row for that (operator, channel, key, language[, version])."""


class TemplateNotApproved(TemplateError):
    """A row exists but its ``approval_status`` is not sendable. Never downgraded to a warning."""


class TemplateRenderError(TemplateError):
    """The body could not be rendered — a missing variable under ``StrictUndefined``, usually."""


# ------------------------------------------------------------------- the Jinja sandbox


def _cut(value: object, n: int) -> str:
    """``value[:n]`` as a filter, so a template never needs subscript syntax to truncate."""
    return str(value)[:n]


def _make_env() -> SandboxedEnvironment:
    env = SandboxedEnvironment(
        undefined=StrictUndefined,  # §6.3: a missing variable FAILS the render, never a blank
        autoescape=False,  # §6.3: these are SMS and plain-text email, not HTML
        keep_trailing_newline=True,  # the v1 email body ends in exactly one newline
    )
    env.filters["cut"] = _cut
    return env


_ENV = _make_env()


def render_body(source: str, context: Mapping[str, object]) -> str:
    """Render one Jinja source in the sandbox. Raises ``TemplateRenderError``, never Jinja's."""
    try:
        return _ENV.from_string(source).render(**context)
    except UndefinedError as exc:
        raise TemplateRenderError(f"template needs a variable the caller did not supply: {exc}") from exc
    except TemplateSyntaxError as exc:  # pragma: no cover - seeding rejects these first
        raise TemplateRenderError(f"template does not compile: {exc}") from exc


def _variables(source: str) -> set[str]:
    """Every undeclared variable a source references, for the seed-time contract check."""
    try:
        return set(meta.find_undeclared_variables(_ENV.parse(source)))
    except TemplateSyntaxError as exc:
        raise TemplateSeedError(f"template does not compile: {exc}") from exc


# ------------------------------------------------------- what the envelope can supply

#: The variables a seeded template is allowed to name, i.e. exactly the keys
#: :func:`context_from_alert` produces. §6.3: "allowed variables = ``params_schema_json``
#: keys, validated at seed time against the keys the envelope can supply". Kept as an
#: explicit tuple rather than derived, so adding a context key is a deliberate act and the
#: test that compares the two fires when they drift.
ALLOWED_PARAMS: tuple[str, ...] = (
    # envelope identity and provenance
    "alert_id",
    "sender",
    "sequence",
    "msg_type",
    "alert_status",
    "scope",
    # the incident
    "incident_id",
    "incident_number",
    "fingerprint",
    # classification (§6.1)
    "category",
    "event",
    "urgency",
    "severity",
    "certainty",
    "priority",
    "lifecycle",
    # timing, already converted to EAT wall-clock for humans
    "sent_eat",
    "effective_eat",
    "onset_eat",
    "expires_eat",
    "next_update_eat",
    "restored_at_eat",
    # where
    "region_code",
    "region_label",
    "county",
    "site_id",
    "site_name",
    "site_type",
    "sites_affected",
    # facts
    "users_affected",
    "child_sites_down",
    "mpesa_risk",
    "failure_domain",
    "tt_category",
    "msp_code",
    "assignee_name",
    "assignee_role_token",
    "radio_oem",
    "planned_power",
    "weather_context",
    "service_affecting",
    "services_impacted",
    "root_cause_hypothesis",
    # content: the only block a model may draft
    "headline",
    "body",
    "instruction",
    # the precomputed v1 email subject (cfg.display_name is not an envelope field)
    "email_subject",
)

#: What a ``hitl_nudge`` template may name -- exactly the keys
#: ``services/hitl_escalation.nudge_context`` produces. Deliberately small and deliberately
#: free of narrative: the card's type, the priority, what the card is about (an incident
#: number, or ``<entity_type> <entity_id>`` for a card with no incident), how long it has
#: waited and who is being fetched. No headline, no body, no site, no person's name.
HITL_NUDGE_PARAMS: tuple[str, ...] = (
    "priority",
    "task_type",
    "subject",
    "unclaimed_minutes",
    "escalation_target",
)


def params_vocabulary(template_key: str) -> tuple[str, ...]:
    """The variables a seeded template with this key may declare (§6.3 seed-time contract)."""
    return HITL_NUDGE_PARAMS if template_key == HITL_NUDGE_TEMPLATE_KEY else ALLOWED_PARAMS


def context_from_alert(alert: NocAlert, language: str = DEFAULT_LANGUAGE) -> dict[str, object]:
    """Flatten a ``NocAlert`` into the render context, keys exactly :data:`ALLOWED_PARAMS`.

    Reads ONLY the envelope — never an ``IncidentRow``, a session or the config — so the
    same bytes come out whether it runs in the HITL node, on the approve re-render, or
    from the JSON copy in ``outbox.envelope_json``. Times are EAT wall-clock because
    "02:14" means Nairobi 02:14 to every person who will read the message, while the
    stored value is naive UTC.
    """
    lang = language if language in alert.content else DEFAULT_LANGUAGE
    content = alert.content[lang]  # type: ignore[index]  # Language literal; "en" is mandatory
    cls, timing, area, facts = alert.classification, alert.timing, alert.area, alert.facts
    email = alert.rendering.email
    return {
        "alert_id": alert.alert_id,
        "sender": alert.sender,
        "sequence": alert.sequence,
        "msg_type": alert.msg_type,
        "alert_status": alert.status,
        "scope": alert.scope,
        "incident_id": alert.incident.id,
        "incident_number": alert.incident.incident_number,
        "fingerprint": alert.incident.fingerprint,
        "category": cls.category,
        "event": cls.event,
        "urgency": cls.urgency,
        "severity": cls.severity,
        "certainty": cls.certainty,
        "priority": cls.priority,
        "lifecycle": cls.lifecycle,
        "sent_eat": fmt_eat(alert.sent),
        "effective_eat": fmt_eat(timing.effective),
        "onset_eat": fmt_eat(timing.onset),
        "expires_eat": fmt_eat(timing.expires),
        "next_update_eat": fmt_eat(timing.expires),  # the same instant, named the way a message says it
        "restored_at_eat": fmt_eat(timing.restored_at),
        "region_code": area.region_code,
        "region_label": area.region_label,
        "county": area.county,
        "site_id": area.site_id,
        "site_name": area.site_name,
        "site_type": area.site_type,
        "sites_affected": list(area.sites_affected),
        "users_affected": facts.users_affected,
        "child_sites_down": facts.child_sites_down,
        "mpesa_risk": facts.mpesa_risk,
        "failure_domain": facts.failure_domain,
        "tt_category": facts.tt_category,
        "msp_code": facts.msp_code,
        "assignee_name": facts.assignee_name,
        "assignee_role_token": facts.assignee_role_token,
        "radio_oem": facts.radio_oem,
        "planned_power": facts.planned_power,
        "weather_context": facts.weather_context,
        "service_affecting": facts.service_affecting,
        "services_impacted": list(facts.services_impacted),
        "root_cause_hypothesis": facts.root_cause_hypothesis,
        "headline": content.headline,
        "body": content.body,
        "instruction": content.instruction,
        "email_subject": email.subject if email is not None else "",
    }


# ---------------------------------------------------------------------- the seed shape


@dataclass(frozen=True)
class PendingTranslation:
    """A language a template deliberately does NOT have, and why (§6.4).

    Carried through ``sync`` into :class:`SyncReport` so "we have not translated this"
    is a reportable fact rather than a comment in a YAML file.
    """

    template_key: str
    channel: str
    language: str
    reason: str
    source: str = ""

    def __str__(self) -> str:
        return f"{self.template_key}/{self.channel}/{self.language}: {self.reason}"


@dataclass(frozen=True)
class SeedTemplate:
    """One ``(channel, template_key, language)`` block, as declared in YAML."""

    template_key: str
    channel: str
    language: str
    body: str
    params: tuple[tuple[str, str], ...]  # (name, json-schema type), sorted; tuple so this stays hashable
    version: int | None = None  # the version this wording was written as; None = "next"
    subject: str | None = None
    provider_template_name: str | None = None
    provider_language_code: str | None = None
    approval_status: str = "DRAFT"
    approved_by: str | None = None
    approved_at: datetime | None = None
    reviewer_role: str | None = None  # §6.4: legal | management, for a reviewed language
    signoff_ref: str | None = None  # where the sign-off is recorded (docs/SIGNOFF.md#...)
    encoding: str = "GSM7"  # SMS only; declaring UCS2 acknowledges the ~2.3x segment cost
    source: str = ""

    @property
    def lineage(self) -> tuple[str, str, str]:
        return (self.channel, self.template_key, self.language)

    @property
    def label(self) -> str:
        at = f"@{self.version}" if self.version is not None else ""
        return f"{self.channel}/{self.template_key}/{self.language}{at}"

    @property
    def params_schema_json(self) -> str:
        return params_schema(self.params)


@dataclass
class SyncReport:
    """What one ``sync`` did. Empty ``inserted`` and ``bumped`` means the table was already right."""

    operator_id: str = ""
    inserted: list[str] = field(default_factory=list)  # "SMS/site_down_alert/en@1"
    unchanged: list[str] = field(default_factory=list)
    bumped: list[str] = field(default_factory=list)  # content changed under a pinned version
    translations_pending: list[PendingTranslation] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.inserted)

    @property
    def rows_written(self) -> int:
        return len(self.inserted)

    def __str__(self) -> str:
        return (
            f"sync[{self.operator_id}] inserted={len(self.inserted)} unchanged={len(self.unchanged)} "
            f"bumped={len(self.bumped)} translations_pending={len(self.translations_pending)}"
        )


@dataclass(frozen=True)
class TemplateResolution:
    """Which row a request resolved to, and whether it may be used for a real send."""

    row: MessageTemplateRow | None
    requested_language: str
    language: str  # what was actually found
    language_fallback: str | None = None  # §6.4: "en" when sw was asked for and is unavailable
    reason: str | None = None  # why ``row`` is None or not sendable

    @property
    def ok(self) -> bool:
        return self.row is not None and self.reason is None

    @property
    def version(self) -> str:
        return str(self.row.version) if self.row is not None else ""


@dataclass(frozen=True)
class RenderedMessage:
    """One rendered message plus the provenance a regulator will ask for.

    ``template_version`` is a string because that is what ``governance.template_version``
    and ``ChannelPayload.template_version`` carry; the column is an integer.
    """

    channel: str
    template_key: str
    template_version: str
    language: str
    body: str
    subject: str | None = None
    language_fallback: str | None = None
    approval_status: str = "DRAFT"
    template_id: str = ""

    @property
    def sendable(self) -> bool:
        return self.approval_status in SENDABLE_STATUSES


# ------------------------------------------------------------------ content addressing


def params_schema(params: Iterable[tuple[str, str]]) -> str:
    """The ``params_schema_json`` column: a real JSON Schema of the allowed variables.

    Serialised with sorted keys so the same declaration always produces the same bytes —
    the fingerprint below depends on it.
    """
    names = sorted(dict(params).items())
    return json.dumps(
        {
            "type": "object",
            "additionalProperties": False,
            "required": [n for n, _ in names],
            "properties": {n: {"type": t} for n, t in names},
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def content_fingerprint(
    body: str,
    subject: str | None,
    params_schema_json: str,
    provider_template_name: str | None = None,
    provider_language_code: str | None = None,
) -> str:
    """A stable digest of everything a *version* means.

    Deliberately excludes approval, timestamps and ids: approving a template does not make
    it a different template, and re-running ``sync`` must not invent a version because a
    clock moved. There is no fingerprint column — the digest is recomputed from the stored
    fields, so the table stays exactly the §6.3 DDL.
    """
    parts = (body, subject or "", params_schema_json, provider_template_name or "", provider_language_code or "")
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def row_fingerprint(row: MessageTemplateRow) -> str:
    """:func:`content_fingerprint` of a stored row."""
    return content_fingerprint(
        row.body,
        row.subject,
        row.params_schema_json,
        row.provider_template_name,
        row.provider_language_code,
    )


def is_sendable(row: MessageTemplateRow | None) -> bool:
    """True only for an ``APPROVED`` row. The one place the send gate is spelled."""
    return row is not None and row.approval_status in SENDABLE_STATUSES


# ------------------------------------------------------------------------- the loader


def seed_roots(operator_id: str | None = None) -> list[Path]:
    """Where seeds are read from, in order: the shared dir, then the operator override."""
    roots = [SEED_DIR]
    if operator_id:
        roots.append(OPERATORS_DIR / operator_id / "templates")
    return [r for r in roots if r.is_dir()]


def load_seed_templates(
    operator_id: str | None = None,
    *,
    roots: Sequence[Path] | None = None,
) -> tuple[list[SeedTemplate], list[PendingTranslation]]:
    """Parse every seed YAML into ``(templates, pending translations)``.

    Validates as it goes and raises :class:`TemplateSeedError` on the first problem, with
    the file named: a template that cannot render is a problem for a developer now, not
    for whoever is on shift at 02:14.
    """
    search = list(roots) if roots is not None else seed_roots(operator_id)
    seeds: list[SeedTemplate] = []
    pending: list[PendingTranslation] = []
    seen: dict[tuple[str, str, str], str] = {}
    for root in search:
        for path in sorted(Path(root).glob("*.yaml")):
            file_seeds, file_pending = _load_seed_file(path)
            for seed in file_seeds:
                previous = seen.get(seed.lineage)
                if previous is not None and previous != seed.source:
                    # An operator override replacing a shared template is a real feature, but
                    # two files inside the SAME root claiming one lineage is a mistake.
                    if Path(previous).parent == path.parent:
                        raise TemplateSeedError(f"{path}: {seed.label} is already declared in {previous}")
                    seeds = [s for s in seeds if s.lineage != seed.lineage]
                seen[seed.lineage] = seed.source
                seeds.append(seed)
            pending.extend(file_pending)
    _check_english_coverage(seeds)
    return seeds, pending


def _load_seed_file(path: Path) -> tuple[list[SeedTemplate], list[PendingTranslation]]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise TemplateSeedError(f"{path}: not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise TemplateSeedError(f"{path}: expected a mapping at the top level")

    key = str(raw.get("template_key") or "").strip()
    if key not in TEMPLATE_KEYS:
        raise TemplateSeedError(f"{path}: template_key {key!r} is not one of §6.3's keys {list(TEMPLATE_KEYS)}")
    channels = raw.get("channels") or {}
    if not isinstance(channels, dict) or not channels:
        raise TemplateSeedError(f"{path}: no channels declared")

    seeds: list[SeedTemplate] = []
    pending: list[PendingTranslation] = []
    for channel_raw, block in channels.items():
        channel = str(channel_raw).upper()
        if channel not in TEMPLATE_CHANNELS:
            raise TemplateSeedError(f"{path}: channel {channel!r} is not one of {list(TEMPLATE_CHANNELS)}")
        if not isinstance(block, dict):
            raise TemplateSeedError(f"{path}: channel {channel} must be a mapping")
        seeds.extend(_load_channel(path, key, channel, block))
        for lang_raw, note in (block.get("translations_pending") or {}).items():
            language = str(lang_raw)
            reason = note.get("reason") if isinstance(note, dict) else note
            reason = " ".join(str(reason or "").split())
            if not reason:
                raise TemplateSeedError(
                    f"{path}: {channel}/{key}/{language} is listed as translations_pending with no reason; "
                    "an unexplained gap is indistinguishable from an oversight"
                )
            pending.append(PendingTranslation(key, channel, language, reason, str(path)))
    return seeds, pending


def _load_channel(path: Path, key: str, channel: str, block: dict) -> list[SeedTemplate]:
    version = block.get("version")
    if version is not None and (not isinstance(version, int) or isinstance(version, bool) or version < 1):
        raise TemplateSeedError(f"{path}: {channel}/{key} version must be a positive integer, got {version!r}")
    params = _load_params(path, key, channel, block.get("params") or {})
    encoding = str(block.get("encoding") or "GSM7").upper()
    if channel == "SMS" and encoding not in _SMS_ENCODINGS:
        raise TemplateSeedError(f"{path}: {channel}/{key} encoding must be one of {list(_SMS_ENCODINGS)}")
    provider_name = block.get("provider_template_name")
    provider_lang = block.get("provider_language_code")
    if channel == "WHATSAPP" and not (provider_name and provider_lang):
        raise TemplateSeedError(
            f"{path}: {channel}/{key} needs provider_template_name and provider_language_code "
            "(a WhatsApp send names Meta's own approved template, not our body)"
        )

    languages = block.get("languages") or {}
    if not isinstance(languages, dict) or not languages:
        raise TemplateSeedError(f"{path}: {channel}/{key} declares no languages")

    seeds: list[SeedTemplate] = []
    for lang_raw, lang_block in languages.items():
        language = str(lang_raw)
        if language not in LANGUAGES:
            raise TemplateSeedError(f"{path}: language {language!r} is not one of {list(LANGUAGES)}")
        if not isinstance(lang_block, dict):
            raise TemplateSeedError(f"{path}: {channel}/{key}/{language} must be a mapping")
        body = lang_block.get("body")
        if not isinstance(body, str) or not body.strip():
            raise TemplateSeedError(f"{path}: {channel}/{key}/{language} has no body")
        subject = lang_block.get("subject")
        if channel == "EMAIL" and not (isinstance(subject, str) and subject.strip()):
            raise TemplateSeedError(f"{path}: {channel}/{key}/{language} is EMAIL and needs a subject")
        if channel != "EMAIL" and subject is not None:
            raise TemplateSeedError(f"{path}: {channel}/{key}/{language} is {channel} and must not declare a subject")

        _check_variables(path, key, channel, language, body, subject, params)
        if channel == "SMS":
            _check_sms_encoding(path, key, language, body, encoding)

        status, by, at, role, ref = _check_approval(path, key, channel, language, lang_block.get("approval") or {})
        seeds.append(
            SeedTemplate(
                template_key=key,
                channel=channel,
                language=language,
                body=body,
                params=params,
                version=version,
                subject=subject if channel == "EMAIL" else None,
                provider_template_name=provider_name,
                provider_language_code=provider_lang,
                approval_status=status,
                approved_by=by,
                approved_at=at,
                reviewer_role=role,
                signoff_ref=ref,
                encoding=encoding,
                source=str(path),
            )
        )
    return seeds


def _load_params(path: Path, key: str, channel: str, raw: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, dict) or not raw:
        raise TemplateSeedError(f"{path}: {channel}/{key} declares no params")
    out: list[tuple[str, str]] = []
    allowed = params_vocabulary(key)
    for name_raw, type_raw in raw.items():
        name, type_name = str(name_raw), str(type_raw).lower()
        if name not in allowed:
            source = "the nudge context (services/hitl_escalation.nudge_context)" if key == HITL_NUDGE_TEMPLATE_KEY else "the envelope"
            raise TemplateSeedError(
                f"{path}: {channel}/{key} declares param {name!r}, which {source} cannot supply "
                f"(§6.3: allowed variables are validated against what the caller can supply). Known: {sorted(allowed)}"
            )
        if type_name not in _JSON_TYPES:
            raise TemplateSeedError(f"{path}: {channel}/{key} param {name!r} has type {type_raw!r}; use one of {sorted(_JSON_TYPES)}")
        out.append((name, _JSON_TYPES[type_name]))
    return tuple(sorted(out))


def _check_variables(
    path: Path,
    key: str,
    channel: str,
    language: str,
    body: str,
    subject: str | None,
    params: tuple[tuple[str, str], ...],
) -> None:
    """Body/subject variables must be exactly the declared params.

    Missing would fail the render under ``StrictUndefined`` at 02:14; extra would make
    ``params_schema_json`` a lie about the contract. Both are cheap to catch here.
    """
    declared = {name for name, _ in params}
    used = _variables(body) | (_variables(subject) if subject else set())
    missing = sorted(used - declared)
    if missing:
        raise TemplateSeedError(
            f"{path}: {channel}/{key}/{language} uses undeclared variable(s) {missing}; "
            "add them to params (StrictUndefined would fail the render at send time)"
        )
    unused = sorted(declared - used)
    if unused:
        raise TemplateSeedError(
            f"{path}: {channel}/{key}/{language} declares param(s) {unused} that the body never uses; "
            "params_schema_json is the contract and must describe what is actually rendered"
        )


def _check_sms_encoding(path: Path, key: str, language: str, body: str, encoding: str) -> None:
    """Static SMS text that leaves GSM-7 must say so.

    A GSM-7 segment holds 153 septets concatenated; a UCS-2 one holds 67. One pasted smart
    quote therefore roughly triples the bill and the out-of-order risk for every send of
    that template, forever, silently. ``site_down_alert@1`` legitimately declares UCS2
    (its em dash is part of the byte contract); anything else has to be deliberate too.
    """
    static = "".join(_static_text(body))
    if encoding == "GSM7" and not is_gsm7(static):
        offenders = sorted({ch for ch in static if not is_gsm7(ch)})
        raise TemplateSeedError(
            f"{path}: SMS/{key}/{language} declares encoding GSM7 but its literal text contains {offenders!r}, "
            "which forces UCS-2 (67 septets per segment instead of 153). Replace the character, "
            "or declare `encoding: UCS2` to accept the cost deliberately."
        )


def _static_text(body: str) -> Iterable[str]:
    """The literal (non-placeholder) characters of a template source."""
    from jinja2 import nodes

    for node in _ENV.parse(body).find_all(nodes.TemplateData):
        yield node.data


def _check_approval(
    path: Path,
    key: str,
    channel: str,
    language: str,
    raw: object,
) -> tuple[str, str | None, datetime | None, str | None, str | None]:
    """Validate the declared approval block and normalise it.

    §6.3: ``APPROVED`` names a human. §6.4 **hard rule**: a reviewed language (``sw``)
    additionally needs the reviewer's role (``legal`` or ``management``) and the place the
    sign-off is recorded. Enforced here, in the seeder, because SQLite cannot express it as
    a constraint the additive migration is allowed to add later.
    """
    if not isinstance(raw, dict):
        raise TemplateSeedError(f"{path}: {channel}/{key}/{language} approval must be a mapping")
    status = str(raw.get("status") or "DRAFT").upper()
    if status not in APPROVAL_STATUSES:
        raise TemplateSeedError(f"{path}: {channel}/{key}/{language} approval status {status!r} is not one of {list(APPROVAL_STATUSES)}")
    by = (str(raw.get("approved_by")).strip() if raw.get("approved_by") is not None else None) or None
    role = (str(raw.get("reviewer_role")).strip().lower() if raw.get("reviewer_role") is not None else None) or None
    ref = (str(raw.get("signoff_ref")).strip() if raw.get("signoff_ref") is not None else None) or None
    at = _naive_utc(raw.get("approved_at"))

    if status == "APPROVED":
        if not by:
            raise TemplateSeedError(
                f"{path}: {channel}/{key}/{language} is APPROVED with no approved_by. "
                "An approval that names nobody is not an approval (§6.3)."
            )
        if language in REVIEWED_LANGUAGES:
            if role not in REVIEWER_ROLES:
                raise TemplateSeedError(
                    f"{path}: {channel}/{key}/{language} is a reviewed language and may only be APPROVED by a "
                    f"reviewer whose role is one of {sorted(REVIEWER_ROLES)} (§6.4 hard rule); got {role!r}"
                )
            if not ref:
                raise TemplateSeedError(
                    f"{path}: {channel}/{key}/{language} is APPROVED without a signoff_ref. §6.4 requires the "
                    "sign-off to be recorded (docs/SIGNOFF.md) before any sw message reaches a real recipient."
                )
        at = at or utcnow()
    elif by or at:
        # A non-APPROVED row carrying an approver is the kind of half-state that later reads
        # as an approval. Refuse it rather than storing an ambiguous row.
        raise TemplateSeedError(
            f"{path}: {channel}/{key}/{language} is {status} but declares approved_by/approved_at; "
            "only an APPROVED template records an approver"
        )
    return status, by, at, role, ref


def _check_english_coverage(seeds: Sequence[SeedTemplate]) -> None:
    """Every (channel, template_key) must have an ``en`` body (§6.1: English is mandatory)."""
    by_channel_key: dict[tuple[str, str], set[str]] = {}
    for seed in seeds:
        by_channel_key.setdefault((seed.channel, seed.template_key), set()).add(seed.language)
    for (channel, key), languages in sorted(by_channel_key.items()):
        if DEFAULT_LANGUAGE not in languages:
            raise TemplateSeedError(
                f"{channel}/{key} has no {DEFAULT_LANGUAGE!r} body (languages: {sorted(languages)}). "
                "English is mandatory and is the fallback every other language relies on (§6.1, §6.4)."
            )


def _naive_utc(value: object) -> datetime | None:
    """YAML gives an aware or naive datetime; the storage contract is naive UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    raise TemplateSeedError(f"approved_at must be a timestamp, got {value!r}")


# ------------------------------------------------------------------------ the registry


class TemplateRegistry:
    """Reads and writes ``message_templates`` for ONE operator.

    Every query is operator-scoped through :meth:`_owned`, mirroring ``main.py:_owned`` —
    there is no unscoped ``select`` in this module, so a template can never leak across
    operator profiles.

    The registry does not commit. ``sync`` flushes so ids exist and the caller sees its own
    writes; the transaction belongs to whoever opened it.
    """

    def __init__(self, session: Session, operator_id: str) -> None:
        self.session = session
        self.operator_id = operator_id

    @classmethod
    def for_config(cls, session: Session, cfg: OperatorConfig) -> TemplateRegistry:
        return cls(session, cfg.operator_id)

    # -- scoping ---------------------------------------------------------------------

    def _owned(self):
        """``SELECT MessageTemplateRow`` restricted to this operator; add filters to it."""
        return select(MessageTemplateRow).where(MessageTemplateRow.operator_id == self.operator_id)

    # -- reading ---------------------------------------------------------------------

    def versions(self, channel: str, template_key: str, language: str) -> list[MessageTemplateRow]:
        """Every version of one lineage, oldest first."""
        stmt = (
            self._owned()
            .where(
                MessageTemplateRow.channel == channel,
                MessageTemplateRow.template_key == template_key,
                MessageTemplateRow.language == language,
            )
            .order_by(MessageTemplateRow.version)
        )
        return list(self.session.scalars(stmt))

    def all_rows(self) -> list[MessageTemplateRow]:
        stmt = self._owned().order_by(
            MessageTemplateRow.template_key,
            MessageTemplateRow.channel,
            MessageTemplateRow.language,
            MessageTemplateRow.version,
        )
        return list(self.session.scalars(stmt))

    def get(self, channel: str, template_key: str, language: str, version: int) -> MessageTemplateRow | None:
        """One exact version — how an outbox row from last Tuesday finds its wording again."""
        stmt = self._owned().where(
            MessageTemplateRow.channel == channel,
            MessageTemplateRow.template_key == template_key,
            MessageTemplateRow.language == language,
            MessageTemplateRow.version == version,
        )
        return self.session.scalar(stmt)

    def latest(self, channel: str, template_key: str, language: str) -> MessageTemplateRow | None:
        """The highest version, approved or not."""
        rows = self.versions(channel, template_key, language)
        return rows[-1] if rows else None

    def latest_approved(self, channel: str, template_key: str, language: str) -> MessageTemplateRow | None:
        """The highest **sendable** version. This, not :meth:`latest`, is what a send uses."""
        approved = [r for r in self.versions(channel, template_key, language) if is_sendable(r)]
        return approved[-1] if approved else None

    # -- resolving (§6.4 language fallback) ------------------------------------------

    def resolve(
        self,
        channel: str,
        template_key: str,
        language: str = DEFAULT_LANGUAGE,
        *,
        version: int | None = None,
        require_approved: bool = True,
    ) -> TemplateResolution:
        """Pick the row a send (or a preview) should use. Never raises — reports instead.

        §6.4: a missing or unapproved ``sw`` falls back to ``en`` and records
        ``language_fallback="en"`` on the payload. The fallback is to English *only*;
        English has no fallback, because there is nothing left to fall back to.
        """
        requested = language
        candidates = [language] if language == DEFAULT_LANGUAGE else [language, DEFAULT_LANGUAGE]
        last_reason: str | None = None
        for candidate in candidates:
            if version is not None:
                row = self.get(channel, template_key, candidate, version)
            elif require_approved:
                # Fall back to the unapproved head when there is no approved one, purely so
                # the reason can name the real problem. "No template" and "the template is
                # still a DRAFT" send an operator down completely different roads at 02:14.
                row = self.latest_approved(channel, template_key, candidate) or self.latest(channel, template_key, candidate)
            else:
                row = self.latest(channel, template_key, candidate)
            if row is None:
                last_reason = f"no {channel}/{template_key}/{candidate} template" + (f"@{version}" if version is not None else "")
                continue
            if require_approved and not is_sendable(row):
                last_reason = f"{channel}/{template_key}/{candidate}@{row.version} is {row.approval_status}, not APPROVED"
                continue
            return TemplateResolution(
                row=row,
                requested_language=requested,
                language=candidate,
                language_fallback=DEFAULT_LANGUAGE if candidate != requested else None,
            )
        return TemplateResolution(row=None, requested_language=requested, language=requested, reason=last_reason or "no template")

    def for_send(
        self,
        channel: str,
        template_key: str,
        language: str = DEFAULT_LANGUAGE,
        *,
        version: int | None = None,
    ) -> TemplateResolution:
        """Like :meth:`resolve`, but **raises** rather than returning an unusable answer.

        The send path must not be able to continue past a missing or unapproved template by
        ignoring a return value.
        """
        resolution = self.resolve(channel, template_key, language, version=version, require_approved=True)
        if resolution.row is None:
            reason = resolution.reason or "no template"
            existing = self.latest(channel, template_key, language) or self.latest(channel, template_key, DEFAULT_LANGUAGE)
            if existing is not None:
                raise TemplateNotApproved(f"refusing to send: {reason}")
            raise TemplateNotFound(f"refusing to send: {reason}")
        return resolution

    # -- rendering -------------------------------------------------------------------

    def render(
        self,
        alert: NocAlert,
        channel: str,
        *,
        template_key: str | None = None,
        language: str = DEFAULT_LANGUAGE,
        version: int | None = None,
        allow_unapproved: bool = False,
    ) -> RenderedMessage:
        """Render one channel of one alert from the registry.

        ``allow_unapproved=True`` is for showing a human a draft (a HITL card, a preview in
        the template admin). It is a separate argument and not a fallback, so a send path
        cannot reach an unapproved body by accident.
        """
        key = template_key or _template_key_for(alert, channel)
        if allow_unapproved:
            resolution = self.resolve(channel, key, language, version=version, require_approved=False)
            if resolution.row is None:
                raise TemplateNotFound(resolution.reason or f"no {channel}/{key} template")
        else:
            resolution = self.for_send(channel, key, language, version=version)
        row = resolution.row
        assert row is not None  # both branches above raise when it is None
        context = context_from_alert(alert, resolution.language)
        return self._rendered(row, resolution, context)

    def render_context(
        self,
        channel: str,
        template_key: str,
        context: Mapping[str, object],
        *,
        language: str = DEFAULT_LANGUAGE,
        version: int | None = None,
        allow_unapproved: bool = False,
    ) -> RenderedMessage:
        """Render one channel of one template from a caller-supplied context.

        For the templates whose variables are not envelope fields (``hitl_nudge``): the
        caller builds the context, and the seed-time check already proved the body names
        only what :func:`params_vocabulary` allows for that key. Same approval rule as
        :meth:`render`: ``allow_unapproved`` is an explicit preview switch, never a fallback.
        """
        if allow_unapproved:
            resolution = self.resolve(channel, template_key, language, version=version, require_approved=False)
            if resolution.row is None:
                raise TemplateNotFound(resolution.reason or f"no {channel}/{template_key} template")
        else:
            resolution = self.for_send(channel, template_key, language, version=version)
        row = resolution.row
        assert row is not None
        return self._rendered(row, resolution, context)

    @staticmethod
    def _rendered(row: MessageTemplateRow, resolution: TemplateResolution, context: Mapping[str, object]) -> RenderedMessage:
        return RenderedMessage(
            channel=row.channel,
            template_key=row.template_key,
            template_version=str(row.version),  # the version this message used: the provenance
            language=resolution.language,
            language_fallback=resolution.language_fallback,
            body=render_body(row.body, context),
            subject=render_body(row.subject, context) if row.subject else None,
            approval_status=row.approval_status,
            template_id=row.id,
        )

    # -- approval --------------------------------------------------------------------

    def set_status(
        self,
        row: MessageTemplateRow | str,
        status: str,
        *,
        actor: str | None = None,
        reviewer_role: str | None = None,
        signoff_ref: str | None = None,
        now: datetime | None = None,
    ) -> MessageTemplateRow:
        """The approval transition — the service behind ``PUT /api/v1/templates/{id}/status``.

        This is the *model*, not a UI: who may call it is ``api/auth.require_role``'s job and
        the route's, both outside this module. What is enforced here is what must be true of
        the row whatever the caller is:

        * the status is one of §6.3's five;
        * ``APPROVED`` names an actor, always;
        * a reviewed language (``sw``) reaching ``APPROVED`` needs a reviewer role of
          ``legal``/``management`` **and** a recorded sign-off (§6.4 hard rule);
        * moving *out* of ``APPROVED`` keeps ``approved_by``/``approved_at``. They are the
          history of who once approved it, and history is not erased by a later pause;
          ``approval_status`` alone decides what may be sent.
        """
        target = self._row(row)
        status = (status or "").upper()
        if status not in APPROVAL_STATUSES:
            raise TemplateError(f"approval status {status!r} is not one of {list(APPROVAL_STATUSES)}")
        actor = (actor or "").strip() or None
        if status == "APPROVED":
            if not actor:
                raise TemplateError("APPROVED needs a named approver (§6.3); refusing an anonymous approval")
            if target.language in REVIEWED_LANGUAGES:
                role = (reviewer_role or "").strip().lower()
                if role not in REVIEWER_ROLES:
                    raise TemplateError(
                        f"{target.language!r} may only be approved by a reviewer whose role is one of "
                        f"{sorted(REVIEWER_ROLES)} (§6.4 hard rule); got {reviewer_role!r}"
                    )
                if not (signoff_ref or "").strip():
                    raise TemplateError(f"{target.language!r} approval must record a sign-off reference (§6.4, docs/SIGNOFF.md)")
            target.approved_by = actor
            target.approved_at = now or utcnow()
        target.approval_status = status
        target.updated_at = now or utcnow()
        self.session.flush()
        return target

    def _row(self, row: MessageTemplateRow | str) -> MessageTemplateRow:
        if isinstance(row, MessageTemplateRow):
            if row.operator_id != self.operator_id:
                raise TemplateNotFound("template not found")  # never confirm another operator's id
            return row
        found = self.session.scalar(self._owned().where(MessageTemplateRow.id == row))
        if found is None:
            raise TemplateNotFound("template not found")
        return found

    # -- seeding ---------------------------------------------------------------------

    def sync(
        self,
        seeds: Sequence[SeedTemplate] | None = None,
        *,
        pending: Sequence[PendingTranslation] | None = None,
        now: datetime | None = None,
    ) -> SyncReport:
        """Load the seed YAML into the table, idempotently and versioned.

        For each ``(channel, template_key, language)`` lineage:

        * **same content as the highest stored version** → nothing is written. Not an
          UPDATE, not a touched ``updated_at``: a second ``sync`` is a pure read;
        * **different content** → the next version is INSERTed and the old row is left
          untouched, so what was sent stays readable;
        * the new row's approval comes from the YAML only when the YAML pinned the version
          being written. If the seeder had to bump past a stale pin, the declared approval
          described different words and the row starts ``DRAFT``;
        * an existing row's approval is **never** changed here — see :meth:`set_status`.
        """
        if seeds is None:
            loaded, loaded_pending = load_seed_templates(self.operator_id)
            seeds = loaded
            pending = loaded_pending if pending is None else pending
        stamp = now or utcnow()
        report = SyncReport(operator_id=self.operator_id, translations_pending=list(pending or []))

        for seed in seeds:
            existing = self.versions(seed.channel, seed.template_key, seed.language)
            schema_json = seed.params_schema_json
            fingerprint = content_fingerprint(
                seed.body, seed.subject, schema_json, seed.provider_template_name, seed.provider_language_code
            )
            head = existing[-1] if existing else None
            if head is not None and row_fingerprint(head) == fingerprint:
                report.unchanged.append(f"{seed.channel}/{seed.template_key}/{seed.language}@{head.version}")
                continue

            version, bumped = self._next_version(seed, existing)
            if bumped:
                report.bumped.append(
                    f"{seed.channel}/{seed.template_key}/{seed.language}: content changed under pinned "
                    f"version {seed.version} -> wrote @{version} as DRAFT"
                )
            status, approved_by, approved_at = ("DRAFT", None, None) if bumped else (seed.approval_status, seed.approved_by, seed.approved_at)
            self.session.add(
                MessageTemplateRow(
                    operator_id=self.operator_id,
                    channel=seed.channel,
                    template_key=seed.template_key,
                    language=seed.language,
                    version=version,
                    body=seed.body,
                    subject=seed.subject,
                    params_schema_json=schema_json,
                    provider_template_name=seed.provider_template_name,
                    provider_language_code=seed.provider_language_code,
                    approval_status=status,
                    approved_by=approved_by,
                    approved_at=approved_at,
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
            report.inserted.append(f"{seed.channel}/{seed.template_key}/{seed.language}@{version}")
        self.session.flush()
        return report

    @staticmethod
    def _next_version(seed: SeedTemplate, existing: Sequence[MessageTemplateRow]) -> tuple[int, bool]:
        """``(version, bumped_past_a_stale_pin)`` for content that is not already stored.

        A pinned version that is still free is honoured — that is the intended workflow when
        a human edits a body *and* bumps ``version:`` in the same change. A pin that is
        already taken by different content is stale, and inventing an approval for the row
        that replaces it would be exactly the mutation this registry exists to prevent.
        """
        taken = {row.version for row in existing}
        if seed.version is not None and seed.version not in taken:
            return seed.version, False
        return (max(taken) + 1 if taken else 1), seed.version is not None


def _template_key_for(alert: NocAlert, channel: str) -> str:
    """The template key the envelope asks for on this channel, or the alert's default.

    ``rendering.sms.template_key`` / ``rendering.email.template_key`` are set by
    ``build_alert``; anything else falls back to the lifecycle-derived key so a caller that
    has not filled ``rendering`` still gets a sensible template rather than a crash.
    """
    if channel == "SMS" and alert.rendering.sms is not None and alert.rendering.sms.template_key:
        return alert.rendering.sms.template_key
    if channel == "EMAIL" and alert.rendering.email is not None and alert.rendering.email.template_key:
        return alert.rendering.email.template_key
    if alert.classification.lifecycle in ("MONITORING", "RESOLVED"):
        return "incident_restored"
    return "site_down_alert" if alert.sequence <= 1 else "incident_update"
