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
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from noc_agents.services.gsm7 import (
    GSM7_CONCAT_LIMIT,
    GSM7_SINGLE_LIMIT,
    SmsCost,
    sms_cost,
)
from noc_agents.services.redaction import EMAIL_RE, PHONE_RE, NameMap, scrub_text

__all__ = [
    "EMAIL_BODY_MAX_BYTES",
    "EMAIL_RECIPIENTS_PER_MESSAGE",
    "EMAIL_SUBJECT_MAX",
    "WHATSAPP_BODY_MAX",
    "WHATSAPP_BUTTON_MAX",
    "WHATSAPP_FOOTER_MAX",
    "WHATSAPP_HEADER_MAX",
    "ValidationResult",
    "Violation",
    "contains_personal_data",
    "personal_data_violations",
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
