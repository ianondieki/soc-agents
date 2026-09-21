"""Per-channel hard validators — spec §6.2 ("violation → SUPPRESSED, never 'send anyway'").

A renderer produces a body; this module decides whether that body may leave the building.
The rule the spec is emphatic about is that a failed check **suppresses** the payload
(``status="SUPPRESSED"``, ``suppress_reason=<code>``) — it never downgrades to "send it
anyway and hope". So every validator returns a :class:`ValidationResult` whose
``suppress_reason`` is the code the renderer stamps on the payload.

Two severities, because the spec needs both:

* ``ERROR``   — suppress. The message must not be transmitted as written.
* ``WARNING`` — report only. §6.2's fidelity rule is explicit that while
  ``ALERT_ENVELOPE_V2`` is false the v1 SMS template's em dash (which forces UCS-2) must be
  *reported and still sent*, because today's golden messages are byte-frozen. Flipping the
  flag turns that same finding into an ERROR. ``enforce_encoding`` carries that switch, and
  it reads the environment only as a fallback so a caller can always be explicit.

**Personal data.** The instruction is to reuse the DPA-2019 scrubber rather than grow a
second implementation, so every check here is built on ``llm/redaction.py``: ``PHONE_RE``
(Kenyan MSISDNs — a strict superset of §6.2's ``\\+?254\\d{9}`` / ``0[17]\\d{8}``),
``EMAIL_RE``, and ``NameMap`` + ``scrub_text`` for names, which brings the part-matching,
role-code and word-boundary behaviour along for free. Names are split in two at the call
site, deliberately, because the two cases have opposite consequences:

* ``forbidden_names`` — must NOT appear (a customer name on any channel; any name on a
  channel that leaves Kenya). A hit is an ERROR.
* ``disclosed_names`` — allowed to appear but recorded (the assignee on an internal SMS,
  which §6.1 treats as ``governance.contains_personal_data=true``, a HITL input, not a
  suppression). A hit is a WARNING and sets ``result.contains_personal_data``.

Both default to empty, so a validator can never suppress a message because of a name the
caller did not name. Which staff names an operator considers disclosable on which audience
is a product decision (§12), not something this module guesses.

Pure functions over strings and mappings: no DB, no config objects, no channel enums (the
envelope types land with §6.1 in another file), so renderers can call these with whatever
they have in hand.

**Envelope content (§6.1).** ``validate_content(alert)`` is the one check that reads a whole
``NocAlert`` rather than a rendered channel body: it judges the ``content{}`` blocks — the
only part a model may draft — BEFORE any renderer sees them. It is duck-typed over the
envelope (the type is imported for annotations only) and reaches the operator clock only to
format the next-update time exactly as the email renderer does, so the two cannot disagree.
``services/alerts.build_alert`` calls it at the ``ai_content`` seam.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

if TYPE_CHECKING:  # annotations only: this module stays importable without the envelope models
    from noc_agents.domain.alerts import Facts, NocAlert

from noc_agents.services.gsm7 import (
    GSM7_CONCAT_LIMIT,
    GSM7_SINGLE_LIMIT,
    SmsCost,
    sms_cost,
)
from noc_agents.services.redaction import EMAIL_RE, PHONE_RE, NameMap, scrub_text

# The scrubber's own role-code rule (``RNIO-NBI-E`` / ``FE-MTK-01`` are matched whole, never by
# part), so "is this a name" is decided ONE way here and in ``NameMap``. It is private to
# ``llm/redaction.py`` and not re-exported by the ``services/redaction`` shim (not this lane's file).
from noc_agents.llm.redaction import _looks_like_role_code

__all__ = [
    "EMAIL_BODY_MAX_BYTES",
    "EMAIL_RECIPIENTS_PER_MESSAGE",
    "EMAIL_SUBJECT_MAX",
    "FALLBACK_REASON_CONTENT_INVALID",
    "WHATSAPP_BODY_MAX",
    "WHATSAPP_BUTTON_MAX",
    "WHATSAPP_FOOTER_MAX",
    "WHATSAPP_HEADER_MAX",
    "ValidationResult",
    "Violation",
    "contains_personal_data",
    "content_fallback_reason",
    "content_names",
    "personal_data_violations",
    "validate_content",
    "validate_email",
    "validate_inapp",
    "validate_sms",
    "validate_whatsapp",
]

ERROR = "ERROR"
WARNING = "WARNING"

# §6.2 channel limits. Email: Gmail SMTP caps a message at 100 recipients; 20 KB keeps the
# body inside every gateway's inline limit. WhatsApp: Meta's template component limits.
EMAIL_SUBJECT_MAX = 200
EMAIL_BODY_MAX_BYTES = 20 * 1024
EMAIL_RECIPIENTS_PER_MESSAGE = 100
WHATSAPP_HEADER_MAX = 60
WHATSAPP_BODY_MAX = 1024
WHATSAPP_FOOTER_MAX = 60
WHATSAPP_BUTTON_MAX = 25

# A tag, not a stray "<" in "<5 min": a name, a closing slash or a declaration must follow.
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(\s[^<>]*)?/?>|<!--")


@dataclass(frozen=True)
class Violation:
    """One finding. ``code`` is what lands in ``ChannelPayload.suppress_reason``."""

    code: str
    message: str
    severity: str = ERROR
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def fatal(self) -> bool:
        return self.severity == ERROR

    def __str__(self) -> str:  # pragma: no cover - convenience for logs and HITL cards
        return f"[{self.severity}] {self.code}: {self.message}"


@dataclass(frozen=True)
class ValidationResult:
    """Verdict for one channel payload."""

    channel: str
    findings: tuple[Violation, ...] = ()
    cost: SmsCost | None = None                 # SMS only: the measured encoding/segments
    contains_personal_data: bool = False        # §6.1 governance input, not a verdict

    @property
    def violations(self) -> tuple[Violation, ...]:
        """Fatal findings, in the order they were checked."""
        return tuple(f for f in self.findings if f.fatal)

    @property
    def warnings(self) -> tuple[Violation, ...]:
        return tuple(f for f in self.findings if not f.fatal)

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def suppress_reason(self) -> str | None:
        """The code to stamp on a suppressed payload — the first fatal finding, or None."""
        violations = self.violations
        return violations[0].code if violations else None

    def codes(self) -> tuple[str, ...]:
        return tuple(f.code for f in self.findings)

    def explain(self) -> str:
        return "; ".join(str(f) for f in self.findings) or "ok"


# ------------------------------------------------------------------- personal data


def _name_hits(text: str, names: Iterable[str]) -> list[str]:
    """Names from ``names`` that appear in ``text``, via the shared scrubber.

    ``scrub_text`` replaces a registered name — and any part of it four letters or longer —
    with its ``<PERSON_n>`` token, honouring word boundaries and the role-code rule. If the
    token survives into the scrubbed copy, that name is in the text.
    """
    registry = NameMap()
    for name in names:
        registry.token_for(name)
    if not registry.token_to_name:
        return []
    scrubbed = scrub_text(text, registry) or ""
    return [full for token, full in registry.token_to_name.items() if token in scrubbed]


def personal_data_violations(
    text: str,
    *,
    forbidden_names: Iterable[str] = (),
    disclosed_names: Iterable[str] = (),
    email_severity: str = WARNING,
) -> list[Violation]:
    """MSISDN / e-mail / name findings for one piece of text, using the shared scrubber.

    An MSISDN is always fatal: §6.2 bans it outright on SMS, and a subscriber or staff
    number in any channel payload is a DPA 2019 disclosure the dispatcher must not make.
    """
    findings: list[Violation] = []
    msisdns = PHONE_RE.findall(text)
    if msisdns:
        findings.append(
            Violation(
                code="personal_data_msisdn",
                message=f"body contains {len(msisdns)} MSISDN-shaped string(s)",
                detail={"count": len(msisdns)},   # the numbers themselves are NOT logged
            )
        )
    emails = EMAIL_RE.findall(text)
    if emails:
        findings.append(
            Violation(
                code="personal_data_email",
                message=f"body contains {len(emails)} e-mail address(es)",
                severity=email_severity,
                detail={"count": len(emails)},
            )
        )
    forbidden_hits = _name_hits(text, forbidden_names)
    if forbidden_hits:
        findings.append(
            Violation(
                code="personal_data_name",
                message=f"body contains {len(forbidden_hits)} forbidden personal name(s)",
                detail={"count": len(forbidden_hits)},
            )
        )
    disclosed_hits = _name_hits(text, disclosed_names)
    if disclosed_hits:
        findings.append(
            Violation(
                code="personal_data_disclosed_name",
                message=f"body names {len(disclosed_hits)} person(s) (governance: contains_personal_data)",
                severity=WARNING,
                detail={"count": len(disclosed_hits)},
            )
        )
    return findings


def contains_personal_data(
    text: str,
    *,
    forbidden_names: Iterable[str] = (),
    disclosed_names: Iterable[str] = (),
) -> bool:
    """True when the text holds an MSISDN, an e-mail address or any supplied name.

    This is the §6.1 ``governance.contains_personal_data`` question, so it does not care
    about severity: a disclosed assignee still counts.
    """
    return bool(
        personal_data_violations(
            text,
            forbidden_names=forbidden_names,
            disclosed_names=disclosed_names,
            email_severity=WARNING,
        )
    )


def _personal_data_flag(findings: Sequence[Violation]) -> bool:
    return any(f.code.startswith("personal_data_") for f in findings)


# ---------------------------------------------------------------------------- SMS


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def validate_sms(
    body: str,
    *,
    incident_number: str | None = None,
    priority: str | None = None,
    max_segments: int = 1,
    enforce_encoding: bool | None = None,
    forbidden_names: Iterable[str] = (),
    disclosed_names: Iterable[str] = (),
) -> ValidationResult:
    """§6.2 SMS validator: alphabet, segment budget, required tokens, no ``@``, no MSISDN.

    ``max_segments`` is ``rendering.sms.max_segments`` from the envelope (default 1). The
    budget is ``160`` for a single segment and ``153 × max_segments`` beyond that, which is
    why 161 characters do not fit in "1 segment plus a bit": the concatenation header costs
    7 septets of every part.

    ``enforce_encoding`` decides what a UCS-2 body means. ``None`` (the default) reads
    ``ALERT_ENVELOPE_V2`` from the environment and defaults to false, which is the fidelity
    rule of §6.2: while the flag is off, the v1 template's em dash is *reported* and the
    message still goes.
    """
    findings: list[Violation] = []
    cost = sms_cost(body)
    if enforce_encoding is None:
        enforce_encoding = _env_flag("ALERT_ENVELOPE_V2", False)

    if not cost.is_gsm7:
        findings.append(
            Violation(
                code="sms_not_gsm7",
                message=(
                    "body leaves the GSM 03.38 alphabet, so the whole message is sent as UCS-2 "
                    f"({cost.per_segment} chars/segment): " + "; ".join(o.describe() for o in cost.offenders)
                ),
                severity=ERROR if enforce_encoding else WARNING,
                detail={
                    "offenders": [o.char for o in cost.offenders],
                    "codepoints": [o.codepoint for o in cost.offenders],
                    "fixable": cost.fixable,
                },
            )
        )

    budget = GSM7_SINGLE_LIMIT if max_segments <= 1 else GSM7_CONCAT_LIMIT * max_segments
    if cost.segments > max_segments:
        findings.append(
            Violation(
                code="sms_too_many_segments",
                message=(
                    f"{cost.units} {cost.encoding} units need {cost.segments} segments; "
                    f"max_segments={max_segments} (budget {budget if cost.is_gsm7 else 'UCS-2 ' + str(cost.per_segment)})"
                ),
                detail={
                    "segments": cost.segments,
                    "max_segments": max_segments,
                    "units": cost.units,
                    "encoding": cost.encoding,
                },
            )
        )

    if incident_number and incident_number not in body:
        findings.append(
            Violation(
                code="sms_missing_incident_number",
                message=f"body does not carry {incident_number}",
                detail={"incident_number": incident_number},
            )
        )
    if priority and priority not in body:
        findings.append(
            Violation(
                code="sms_missing_priority",
                message=f"body does not carry the priority token {priority}",
                detail={"priority": priority},
            )
        )
    if "@" in body:
        # Legal GSM-7 (code point 0x00) but banned by §6.2: an "@" in an alert body means an
        # address has leaked into a channel whose recipients are resolved by the dispatcher.
        findings.append(
            Violation(
                code="sms_contains_at_sign",
                message="body contains '@' (address leaked into an SMS)",
            )
        )
    findings.extend(
        personal_data_violations(
            body,
            forbidden_names=forbidden_names,
            disclosed_names=disclosed_names,
            email_severity=WARNING,   # the '@' rule above already suppresses on SMS
        )
    )
    return ValidationResult(
        channel="SMS",
        findings=tuple(findings),
        cost=cost,
        contains_personal_data=_personal_data_flag(findings),
    )


# -------------------------------------------------------------------------- EMAIL


def validate_email(
    subject: str,
    body: str,
    *,
    incident_number: str | None = None,
    priority: str | None = None,
    region_label: str | None = None,
    next_update: str | None = None,
    recipients: Sequence[str] | None = None,
    forbidden_names: Iterable[str] = (),
    disclosed_names: Iterable[str] = (),
) -> ValidationResult:
    """§6.2 e-mail validator: subject ≤ 200, required facts present, plain text, ≤ 20 KB.

    ``next_update`` is the rendered EAT string the body must carry (the renderer formats it
    with ``services/clock.py``; this module only checks that it survived into the text).
    """
    findings: list[Violation] = []
    if len(subject) > EMAIL_SUBJECT_MAX:
        findings.append(
            Violation(
                code="email_subject_too_long",
                message=f"subject is {len(subject)} chars (max {EMAIL_SUBJECT_MAX})",
                detail={"length": len(subject)},
            )
        )
    if not subject.strip():
        findings.append(Violation(code="email_subject_empty", message="subject is empty"))

    size = len(body.encode("utf-8"))
    if size > EMAIL_BODY_MAX_BYTES:
        findings.append(
            Violation(
                code="email_body_too_large",
                message=f"body is {size} bytes (max {EMAIL_BODY_MAX_BYTES})",
                detail={"bytes": size},
            )
        )
    if _HTML_TAG_RE.search(body):
        findings.append(
            Violation(
                code="email_contains_html",
                message="body carries HTML markup; §6.2 requires it stripped",
            )
        )
    for label, token, code in (
        ("incident number", incident_number, "email_missing_incident_number"),
        ("priority", priority, "email_missing_priority"),
        ("region label", region_label, "email_missing_region_label"),
        ("next update (EAT)", next_update, "email_missing_next_update"),
    ):
        if token and token not in body:
            findings.append(
                Violation(code=code, message=f"body does not carry the {label} {token!r}", detail={"token": token})
            )
    if recipients is not None and len(recipients) > EMAIL_RECIPIENTS_PER_MESSAGE:
        findings.append(
            Violation(
                code="email_too_many_recipients",
                message=(
                    f"{len(recipients)} recipients in one message (max {EMAIL_RECIPIENTS_PER_MESSAGE}); "
                    "batch them"
                ),
                detail={"count": len(recipients)},
            )
        )
    findings.extend(
        personal_data_violations(
            body,
            forbidden_names=forbidden_names,
            disclosed_names=disclosed_names,
            # A staff address in an e-mail body is ordinary; an MSISDN never is.
            email_severity=WARNING,
        )
    )
    return ValidationResult(
        channel="EMAIL",
        findings=tuple(findings),
        contains_personal_data=_personal_data_flag(findings),
    )


# ----------------------------------------------------------------------- WHATSAPP


def validate_whatsapp(
    body: str,
    *,
    header: str | None = None,
    footer: str | None = None,
    buttons: Sequence[str] = (),
    params: Mapping[str, Any] | None = None,
    required_params: Iterable[str] = (),
    forbidden_names: Iterable[str] = (),
    disclosed_names: Iterable[str] = (),
) -> ValidationResult:
    """§6.2 WhatsApp validator: component lengths and every NAMED parameter present.

    Template approval and opt-in are register/DB questions the renderer answers (they
    produce their own ``no_approved_template`` / ``no_opt_in`` suppressions); this module
    only judges the rendered components.
    """
    findings: list[Violation] = []
    for label, text, limit, code in (
        ("header", header, WHATSAPP_HEADER_MAX, "whatsapp_header_too_long"),
        ("body", body, WHATSAPP_BODY_MAX, "whatsapp_body_too_long"),
        ("footer", footer, WHATSAPP_FOOTER_MAX, "whatsapp_footer_too_long"),
    ):
        if text is not None and len(text) > limit:
            findings.append(
                Violation(
                    code=code,
                    message=f"{label} is {len(text)} chars (max {limit})",
                    detail={"length": len(text), "limit": limit},
                )
            )
    for index, label in enumerate(buttons):
        if len(label) > WHATSAPP_BUTTON_MAX:
            findings.append(
                Violation(
                    code="whatsapp_button_too_long",
                    message=f"button {index} label is {len(label)} chars (max {WHATSAPP_BUTTON_MAX})",
                    detail={"index": index, "length": len(label)},
                )
            )
    supplied = dict(params or {})
    missing = [
        name
        for name in required_params
        if name not in supplied or supplied[name] is None or str(supplied[name]).strip() == ""
    ]
    if missing:
        findings.append(
            Violation(
                code="whatsapp_missing_param",
                message="NAMED parameter(s) missing or blank: " + ", ".join(sorted(missing)),
                detail={"missing": sorted(missing)},
            )
        )
    findings.extend(
        personal_data_violations(
            body,
            forbidden_names=forbidden_names,
            disclosed_names=disclosed_names,
            email_severity=WARNING,
        )
    )
    return ValidationResult(
        channel="WHATSAPP",
        findings=tuple(findings),
        contains_personal_data=_personal_data_flag(findings),
    )


# -------------------------------------------------------------------------- IN-APP


def validate_inapp(
    payload: Mapping[str, Any],
    *,
    required_keys: Iterable[str] = (),
    forbidden_names: Iterable[str] = (),
    disclosed_names: Iterable[str] = (),
) -> ValidationResult:
    """§6.2 in-app validator: JSON-serialisable and every required key present.

    The key set itself is pinned in ``tests/system/test_contracts.py``; this checks the
    payload the renderer produced against whatever set the caller passes.
    """
    findings: list[Violation] = []
    try:
        serialised = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        serialised = ""
        findings.append(
            Violation(
                code="inapp_not_json_serialisable",
                message=f"payload is not JSON-serialisable: {exc}",
            )
        )
    missing = [key for key in required_keys if key not in payload]
    if missing:
        findings.append(
            Violation(
                code="inapp_missing_key",
                message="missing key(s): " + ", ".join(sorted(missing)),
                detail={"missing": sorted(missing)},
            )
        )
    findings.extend(
        personal_data_violations(
            serialised,
            forbidden_names=forbidden_names,
            disclosed_names=disclosed_names,
            email_severity=WARNING,
        )
    )
    return ValidationResult(
        channel="INAPP",
        findings=tuple(findings),
        contains_personal_data=_personal_data_flag(findings),
    )


# ------------------------------------------------------------- ENVELOPE CONTENT (§6.1)

#: ``llm_calls.fallback_reason`` when model-drafted ``content{}`` fails ``validate_content`` and
#: the deterministic template goes out instead. Same column, same style as ``llm/client.py``'s
#: ``spend_cap`` / ``budget_exhausted``; ``content_fallback_reason`` appends the finding codes.
FALLBACK_REASON_CONTENT_INVALID = "content_invalid"

# "caused by" / "due to" as words, any case, any run of whitespace between them.
_CAUSE_TRIGGER_RE = re.compile(r"\b(?:caused\s+by|due\s+to)\b", re.IGNORECASE)
# Where a cause clause ends. Deliberately coarse ("3.5 km" also ends it): a clause cut short
# can only make the check STRICTER, and a false positive costs the AI draft, never the message.
_CLAUSE_END_RE = re.compile(r"[.!?;\n]")


def _cause_clauses(text: str) -> list[str]:
    """The words that FOLLOW each ``caused by`` / ``due to``, up to the end of the clause.

    Only what follows counts, because that is where the cause is named: "POWER outage due to a
    fibre cut" mentions POWER and still invents a fibre cut.
    """
    clauses = []
    for match in _CAUSE_TRIGGER_RE.finditer(text):
        rest = text[match.end():]
        end = _CLAUSE_END_RE.search(rest)
        clauses.append(rest[: end.start()] if end else rest)
    return clauses


# Priority and incident-number tokens, WHOLE: a word character or a hyphen on either side makes
# it a different token — "P10" is not P1, "SFC-P20" is not P2, "INC0001234" is not INC000123.
_PRIORITY_TOKEN_RE = re.compile(r"(?<![\w-])P[1-4](?![\w-])", re.IGNORECASE)
_INCIDENT_TOKEN_RE = re.compile(r"(?<![\w-])INC\d+(?![\w-])", re.IGNORECASE)


def _has_token(text: str, token: str, *, identifier: bool = False) -> bool:
    """``token`` present as a whole token, not as part of a longer one ("14:17 EATS" is not "14:17 EAT")."""
    edge = r"[\w-]" if identifier else r"\w"
    return re.search(rf"(?<!{edge}){re.escape(token)}(?!{edge})", text) is not None


def _blank_spans(text: str, spans: Iterable[str]) -> str:
    """Each verbatim occurrence of each span replaced by spaces (offsets kept)."""
    for span in spans:
        if span:
            text = text.replace(span, " " * len(span))
    return text


def _blank_words(text: str, words: Iterable[str]) -> str:
    """Whole-word, any-case occurrences of each word replaced by spaces."""
    for word in words:
        if word:
            text = re.sub(rf"(?<!\w){re.escape(word)}(?!\w)", lambda m: " " * len(m.group(0)), text, flags=re.IGNORECASE)
    return text


def _is_person_value(value: str | None) -> bool:
    """Whether a name-ish field value can be a person's name at all.

    Not a person: a role code by the redaction module's own rule (``FE-NBI-E-01``,
    ``RNIO-NBI-E`` — the rule ``NameMap`` uses to decide what it matches whole), a single
    all-caps token (the role and vendor vocabulary: ``MSP``, ``FIELD_ENGINEER``, ``EGYPRO``,
    what a restore by role writes into ``restored_by``), an id with a colon (``policy:…``)
    or an address, or an agent's author name (``…Agent``). Everything else is treated as a
    person — the conservative side, since a false positive costs only the AI draft.
    """
    v = (value or "").strip()
    if not v or _looks_like_role_code(v):
        return False
    if v.upper() == v and not any(ch.isspace() for ch in v):
        return False
    return ":" not in v and "@" not in v and not v.endswith("Agent")


def _is_vendor_name(name: str, msp_code: str | None) -> bool:
    """``EGYPRO`` or a desk named after the vendor (``EGYPRO Power Desk``): never a person."""
    code = (msp_code or "").strip()
    return bool(code) and re.match(rf"{re.escape(code)}(?!\w)", name.strip(), re.IGNORECASE) is not None


def content_names(
    *,
    assignee_name: str | None,
    assignee_is_person: bool,
    msp_code: str | None,
    people: Iterable[str | None] = (),
) -> tuple[list[str], list[str]]:
    """``(people, whole_names)`` a draft must not contain (§6.1 "NameMap tokens only").

    A model drafts from redacted input — every value in ``redaction.PSEUDONYMISED``
    (``assignee_name``, ``fe_name``, ``rnio_name``) arrives as a ``<PERSON_n>`` token — so a real
    name in its output was un-redacted or invented. Two strengths, because the two cases differ:

    * **people** (``fe_name``, ``rnio_name``, ``restored_by``, and the assignee when the
      assignment is to a person) are matched like the scrubber matches them: the full name AND
      each part of four letters or more, so "Kevin is on site" is caught for "Kevin Ochieng";
    * **a non-person assignee** (an MSP or NOC queue) is matched as a WHOLE name only. Its parts
      are words like "Power" or "Desk" that a compliant draft needs; but the whole value stays
      forbidden because a HITL override (``main._apply_overrides``) can type a person's name into
      ``assignee_name`` without changing ``assignee_type``. A vendor code or a desk named after
      the vendor (``EGYPRO``, ``EGYPRO Power Desk``) is exempt entirely: ``Facts.msp_code`` is
      "never a person".

    Role codes and role vocabulary are never names (``_is_person_value``).
    """
    persons = [str(v).strip() for v in people if _is_person_value(v)]
    whole: list[str] = []
    name = (assignee_name or "").strip()
    if name and not _is_vendor_name(name, msp_code) and _is_person_value(name):
        (persons if assignee_is_person else whole).append(name)
    return list(dict.fromkeys(persons)), list(dict.fromkeys(whole))


def _names_from_facts(facts: Facts) -> tuple[list[str], list[str]]:
    """``content_names`` from the envelope alone, for a caller that has no incident row.

    The envelope carries one name field; whether the assignee is a person is read from
    ``facts.assignee_role_token`` (``MSP-…`` and ``NOC-…`` are queues). ``build_alert`` passes
    the incident's FE, RNIO and restorer names as well — see ``alerts.content_validation_context``.
    """
    token = (facts.assignee_role_token or "").upper()
    return content_names(
        assignee_name=facts.assignee_name,
        assignee_is_person=not token.startswith(("MSP-", "NOC-")),
        msp_code=facts.msp_code,
    )


def validate_content(
    alert: NocAlert,
    *,
    next_update: str | None = None,
    people: Iterable[str] | None = None,
    whole_names: Iterable[str] | None = None,
    quoted: Iterable[str] = (),
) -> list[str]:
    """§6.1 envelope-level check of every ``content{}`` block. Empty list = acceptable.

    Each block (headline + body + instruction, per language) must:

    * **carry the four facts** a reader acts on — the incident number, the priority, the region
      label and the next-update time in EAT (``"14:02 EAT"``, formatted by
      ``services/clock.fmt_eat`` exactly as the email renderer prints it; ``next_update``
      overrides). Each is matched as a WHOLE token: "P10" does not carry P1, "INC0001234" does
      not carry INC000123, "14:17 EATS" does not carry 14:17 EAT. A fact the envelope does not
      have (no ``timing.expires``) is not required;
    * **state no other priority and no other incident number** — a draft that says "P1" on a P2
      incident is wrong even if it also says "P2" somewhere. The one exception is ``quoted``:
      verbatim deterministic text the draft may repeat, whose tokens are not claims. At the
      ``build_alert`` seam that is the severity engine's rationale, which names the intermediate
      steps by construction (``users=3200→P4; site_type=HUB floor=P2; final=P2``) and is part of
      the template narrative;
    * **not invent a cause**: a clause introduced by ``caused by`` / ``due to`` must name the
      incident's own ``facts.failure_domain``. "due to a fibre cut" on a POWER incident is a
      model stating a root cause nobody observed, in a sentence that would reach customers
      and the regulator. The rule is literal on purpose; it has no synonyms to argue about;
    * **carry no personal data**: no MSISDN, no e-mail address, and none of the names from
      ``content_names`` — ``people`` by full name and by part, ``whole_names`` whole. Default:
      what the envelope itself carries (``_names_from_facts``). Before the name scan the tokens
      the draft is REQUIRED or entitled to carry — incident number, priority, region label and
      its words, next-update time, failure domain, vendor code — are blanked, so a name part can
      never collide with them ("Power" in a desk name must not forbid the POWER the cause rule
      demands).

    ``content["en"]`` must exist (§6.1: English is mandatory). Findings are strings of the form
    ``"<code> (<lang>): <reason>"``, and never quote the drafted text — they are stored in
    ``llm_calls.fallback_reason``, and an audit record must not repeat what it found (§9.5).
    """
    findings: list[str] = []
    content = alert.content or {}
    if "en" not in content:
        findings.append("content_missing_en (en): the English block is mandatory (§6.1)")
    if next_update is None:
        from noc_agents.services.clock import fmt_eat  # lazy: the operator clock, only when needed

        next_update = fmt_eat(alert.timing.expires)
    facts = alert.facts
    number = alert.incident.incident_number
    priority = alert.classification.priority
    region = alert.area.region_label
    domain = (facts.failure_domain or "").strip()
    domain_re = re.compile(rf"\b{re.escape(domain)}\b", re.IGNORECASE) if domain else None
    if people is None and whole_names is None:
        people, whole_names = _names_from_facts(facts)
    people, whole_names, quoted = list(people or ()), list(whole_names or ()), [q for q in quoted if q]
    entitled = [number, priority, region, *(region or "").split(), next_update, domain, facts.msp_code or ""]
    required = (
        ("content_missing_incident_number", "incident number", number, True),
        ("content_missing_priority", "priority", priority, True),
        ("content_missing_region_label", "region label", region, False),
        ("content_missing_next_update", "next update (EAT)", next_update, False),
    )
    for lang, block in content.items():
        if block is None:
            continue
        text = "\n".join(part for part in (block.headline, block.body, block.instruction) if part)
        for code, label, token, identifier in required:
            if token and not _has_token(text, token, identifier=identifier):
                findings.append(f"{code} ({lang}): does not carry the {label} {token!r}")
        claims = _blank_spans(text, quoted)
        other_priorities = sorted({m.upper() for m in _PRIORITY_TOKEN_RE.findall(claims)} - {priority.upper()})
        if other_priorities:
            findings.append(
                f"content_conflicting_priority ({lang}): states {', '.join(other_priorities)} on a {priority} incident"
            )
        other_incidents = {m.upper() for m in _INCIDENT_TOKEN_RE.findall(claims)} - {number.upper()}
        if other_incidents:
            findings.append(
                f"content_conflicting_incident_number ({lang}): names {len(other_incidents)} incident number(s) "
                f"other than {number}"
            )
        for clause in _cause_clauses(text):
            if domain_re is None or not domain_re.search(clause):
                findings.append(
                    f"content_invented_cause ({lang}): states a cause (caused by / due to) that does not "
                    f"name the failure domain {domain or '(none)'!r}"
                )
                break  # one is enough to refuse the block; the rest would say the same thing
        # Contacts on the text as written; names on the text with the entitled tokens blanked.
        for violation in personal_data_violations(text, email_severity=ERROR):
            if violation.fatal:
                findings.append(f"content_{violation.code} ({lang}): {violation.message}")
        names_text = _blank_words(text, entitled)
        named = len(_name_hits(names_text, people)) + sum(
            1 for n in whole_names if re.search(rf"(?<!\w){re.escape(n)}(?!\w)", names_text, re.IGNORECASE)
        )
        if named:
            findings.append(f"content_personal_data_name ({lang}): body names {named} person(s) (§6.1: tokens only)")
    return findings


def content_fallback_reason(findings: Sequence[str]) -> str:
    """``"content_invalid: <code> (<lang>), …"`` for ``llm_calls.fallback_reason``: codes only."""
    codes: list[str] = []
    for finding in findings:
        code = finding.split(":", 1)[0].strip()
        if code not in codes:
            codes.append(code)
    if not codes:
        return FALLBACK_REASON_CONTENT_INVALID
    return f"{FALLBACK_REASON_CONTENT_INVALID}: " + ", ".join(codes)
