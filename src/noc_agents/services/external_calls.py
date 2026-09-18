"""The cross-border transfer register: one audited row per byte that leaves the machine.

Kenya's Data Protection Act 2019 and the General Regulations 2021 make an outbound
transfer a recordable act, not an implementation detail: reg 41(2) wants the date and
time, the recipient, the justification and a description of the data, kept where the
ODPC can read it back. This module is that seam. Every adapter that leaves the box
(SMTP relay, SMS, WhatsApp, LLM, remote MCP, X, Meta Graph) is expected to call
``record_transfer`` and hang ``envelope.governance.transfer_record_id`` off the row it
returns, so the register can be reconstructed from ``audit_events`` alone.

Three things this file is deliberate about:

* **Kenya-domiciled recipients still get a row.** Africa's Talking and a locally hosted
  Ollama are recorded with ``recipient_country="KE"``; nothing about them is exempt from
  being written down, they are simply filtered out of the *cross-border* view of the
  register by their country code.
* **The paperwork gate.** Before the first live call to a recipient outside Kenya the
  operator needs the s.31 DPIA reference, a Transfer Impact Assessment reference and the
  recipient's contracting entity and country on file in
  ``config/operators/<op>/transfers.yaml``. Without a ``dpia_ref`` and a ``tia_ref`` this
  raises :class:`TransferPaperworkMissing`; callers treat that as fail-soft (fall back to
  the deterministic template path) — refusing to draft is cheaper than an unlawful
  transfer. ``NOC_ENV=demo`` is the one exception: it logs a single warning line and
  records ``tia_ref="DEMO-UNFILED"`` so the demo register is *honest* about the gap
  instead of silently clean.
* **No secret can reach the payload.** The row carries a fixed set of keys (never a
  caller-supplied dict), and every free-text field is run through the shared scrubber
  plus :func:`scrub_secrets`, which blanks the values of credential-shaped environment
  variables and the usual key/token/password shapes.

UNVERIFIED (spec §7.0.10, to be confirmed by Legal when the TIA is filed): the finality
of the ODPC Guidance Note on Cross-border Data Transfers (April 2026) keyed to reg 40,
and whether the ODPC-issued standard contractual clauses are the required instrument or
one of several. The ``scc_ref`` field is recorded but the gate does not require it.

Callers today: ``orchestrator/outbox.drain_once`` writes the record for every EMAIL row
that actually reaches the SMTP relay, before the bytes leave (§7.0.10 "every adapter that
leaves the machine"). The LLM, SMS, WhatsApp, X and remote-MCP adapters are wired in later
waves; each one is a ``record_transfer`` call on the same shape.

``enforce_gate`` exists because §7.0.10 scopes the *refusal* more narrowly than the
*record*: the TIA is a gating artefact for ``LLM_ENABLED=true`` on a hosted provider and
for any ``residency="abroad"`` MCP card. A channel outside that list (the SMTP relay)
passes ``enforce_gate=False`` — it still writes a row, marked ``paperwork_status="unfiled"``
so the register names the gap, rather than refusing to record a transfer that happened.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

from noc_agents.config import OPERATORS_DIR, AppSettings, get_settings
from noc_agents.db.models import AuditRow, new_id, utcnow
from noc_agents.services.redaction import scrub_contacts

log = logging.getLogger("noc_agents.services.external_calls")

ACTION = "external.call"
KENYA = "KE"
TRANSFERS_FILENAME = "transfers.yaml"
DEMO_UNFILED = "DEMO-UNFILED"
REDACTED = "<REDACTED>"

# Refs the gate insists on for a cross-border recipient (spec §7.0.10 / §9.2 first row).
REQUIRED_REFS: tuple[str, ...] = ("dpia_ref", "tia_ref")
PAPERWORK_FIELDS: tuple[str, ...] = (
    "entity", "country", "dpia_ref", "tia_ref", "scc_ref", "confirmed_by", "confirmed_at",
)

# Paperwork status recorded on every row, so the register says which rows are defensible.
STATUS_NOT_REQUIRED = "not_required"  # domestic recipient: no cross-border paperwork applies
STATUS_FILED = "filed"                # DPIA + TIA references on file for this recipient
STATUS_DEMO_UNFILED = "demo_unfiled"  # NOC_ENV=demo let it through; paperwork is NOT filed
STATUS_UNFILED = "unfiled"            # recorded outside demo with the paperwork NOT filed:
                                      # the caller is a channel the §7.0.10 gate does not
                                      # cover (enforce_gate=False), so the transfer is
                                      # written down WITH its gap instead of being refused.

MAX_PAYLOAD_CHARS = 2000  # same cap as the llm.call rows in llm/assist.py
_MAX_RECIPIENT = 160
_MAX_TEXT = 300
_MAX_REF = 80
_MAX_ACTOR = 120


class TransferPaperworkMissing(RuntimeError):
    """A cross-border recipient has no DPIA/TIA reference on file.

    Callers treat this as fail-soft: skip the external call and use the template path.
    Nothing is written to the register, because nothing left the machine.
    """

    def __init__(self, recipient: str, recipient_key: str, missing: tuple[str, ...]) -> None:
        self.recipient = recipient
        self.recipient_key = recipient_key
        self.missing = tuple(missing)
        super().__init__(
            f"cross-border transfer to {recipient!r} refused: {', '.join(missing)} missing for "
            f"'{recipient_key}' in config/operators/<op>/{TRANSFERS_FILENAME}"
        )


@dataclass(frozen=True)
class TransferPaperwork:
    """One row of ``transfers.yaml``, normalised to strings."""

    recipient_key: str
    entity: str = ""
    country: str = ""
    dpia_ref: str = ""
    tia_ref: str = ""
    scc_ref: str = ""
    confirmed_by: str = ""
    confirmed_at: str = ""

    @classmethod
    def from_raw(cls, recipient_key: str, raw: dict[str, Any] | None) -> "TransferPaperwork":
        raw = raw or {}
        values = {f: str(raw.get(f) or "").strip() for f in PAPERWORK_FIELDS}
        return cls(recipient_key=recipient_key, **values)

    def missing_refs(self) -> tuple[str, ...]:
        return tuple(f for f in REQUIRED_REFS if not getattr(self, f))

    def as_payload(self) -> dict[str, str]:
        """Register fields for the audit payload. ``country`` becomes ``entity_country``:
        it is where the *contracting entity* sits, which is not always the endpoint's
        ``recipient_country``."""
        out = {f: _cap(getattr(self, f), _MAX_REF if f.endswith("_ref") else _MAX_RECIPIENT) for f in PAPERWORK_FIELDS}
        out["entity_country"] = out.pop("country").upper()
        return out


# --------------------------------------------------------------------------- environment


def noc_env() -> str:
    """``NOC_ENV`` lowercased; anything unset reads as production (the strict side)."""
    return (os.getenv("NOC_ENV") or "production").strip().lower()


def is_demo() -> bool:
    return noc_env() == "demo"


# --------------------------------------------------------------------------- the register file


def transfers_path(operator_id: str) -> Path:
    """``config/operators/<op>/transfers.yaml`` — a directory beside the flat ``<op>.yaml`` profile."""
    return OPERATORS_DIR / operator_id / TRANSFERS_FILENAME


def load_transfers(operator_id: str) -> dict[str, dict[str, Any]]:
    """Read the operator's transfer register. A missing or unreadable file is an EMPTY
    register, not a crash: an empty register refuses cross-border calls (fail-soft) and
    still lets domestic ones through."""
    path = transfers_path(operator_id)
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as exc:  # a broken file must not take the NOC down
        log.warning("transfer register unreadable (%s): %s — treating as empty", path, type(exc).__name__)
        return {}
    if not isinstance(raw, dict):
        log.warning("transfer register at %s is not a mapping — treating as empty", path)
        return {}
    return {str(k): (v if isinstance(v, dict) else {}) for k, v in raw.items()}


def normalise_key(name: str) -> str:
    """``"Anthropic API"`` → ``anthropic_api``: the recipient key spelling used in the YAML.

    Apostrophes are dropped rather than turned into separators, so "Africa's Talking"
    keys as ``africas_talking`` the way a human would write it in the register.
    """
    plain = re.sub(r"['’ʼ]", "", (name or "").strip().lower())
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", plain)).strip("_")


def lookup_paperwork(register: dict[str, dict[str, Any]], recipient: str) -> TransferPaperwork:
    """Find ``recipient`` in the register by key, normalised key, or contracting entity."""
    key = normalise_key(recipient)
    if recipient in register:
        return TransferPaperwork.from_raw(recipient, register[recipient])
    by_norm = {normalise_key(k): k for k in register}
    if key in by_norm:
        return TransferPaperwork.from_raw(by_norm[key], register[by_norm[key]])
    for raw_key, entry in register.items():
        if normalise_key(str(entry.get("entity") or "")) == key and key:
            return TransferPaperwork.from_raw(raw_key, entry)
    return TransferPaperwork(recipient_key=key or "unknown")


def is_cross_border(recipient_country: str, residency: str) -> bool:
    """Outside Kenya, or an MCP card that declares ``residency="abroad"``.

    A recipient that claims ``KE`` while the card says ``abroad`` is treated as
    cross-border: the conservative reading is the one the ODPC would take.
    """
    country = (recipient_country or "").strip().upper()
    return (residency or "").strip().lower() == "abroad" or country != KENYA


# --------------------------------------------------------------------------- scrubbing


# Environment variables whose NAME looks like a credential; their VALUES must never appear
# in an audit payload, whatever a caller passes in.
_SECRET_ENV_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|CREDENTIAL|AUTH)")
_MIN_SECRET_LEN = 8

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),                                     # sk-ant-…, sk-…
    re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._\-+/=]{8,}", re.IGNORECASE),  # Authorization headers
    # key=value / key: value for the usual credential words (quoted or bare).
    #
    # The leading guard is (?<![A-Za-z0-9]) and NOT \b on purpose. "_" is a word
    # character, so \b never matches between "_" and the credential word — which meant
    # PREFIX_WORD=value slipped through entirely, and every credential name this system
    # documents is of exactly that shape: GMAIL_APP_PASSWORD, SMTP_PASSWORD, AT_API_KEY,
    # X_BEARER_TOKEN, NOC_SESSION_SECRET, WHATSAPP_APP_SECRET. Bare "password=" matched
    # while "SMTP_PASSWORD=" did not. This pattern is the fallback for values this
    # process does NOT hold — the rotated or third-party key an operator pastes into a
    # note — so it was failing in precisely the case it exists for.
    # Found by tests/unit/test_no_secrets.py.
    re.compile(
        r"(?i)(?<![A-Za-z0-9])(?:api[_-]?key|auth[_-]?token|access[_-]?token|app[_-]?password|token|secret|password|passwd|pwd)\b"
        r"\s*[:=]\s*[\"']?[^\s\"',;]{4,}[\"']?"
    ),
    re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{32,}={0,2}(?![A-Za-z0-9+/=])"),  # long opaque blob
)


def _secret_values() -> list[str]:
    """Credential-shaped environment values, longest first (so a prefix cannot survive).

    Each value is registered in BOTH the form it is stored in and the form with all
    whitespace removed, because those are not always the same string on the wire:
    ``adapters/email_smtp.py`` strips the spaces out of a Gmail app password
    (``"xxxx xxxx xxxx xxxx"``) before ``server.login()``, so the 16-character compact
    form is what actually travels. That form is under the 32-character opaque-blob
    threshold and matches no other pattern, so without this it survives scrubbing
    untouched. Found by tests/unit/test_no_secrets.py.
    """
    out: set[str] = set()
    for name, value in os.environ.items():
        if not _SECRET_ENV_NAME.search(name.upper()):
            continue
        stripped = (value or "").strip()
        if len(stripped) < _MIN_SECRET_LEN:
            continue
        out.add(stripped)
        compact = "".join(stripped.split())
        if len(compact) >= _MIN_SECRET_LEN:
            out.add(compact)
    return sorted(out, key=len, reverse=True)


def scrub_secrets(text: str | None) -> str:
    """Blank anything credential-shaped: known env values first, then generic shapes.

    Belt and braces on top of the fixed payload keys — the register is meant to be handed
    to a regulator, so a stray key pasted into a justification must not travel with it.
    """
    if not text:
        return ""
    out = str(text)
    for value in _secret_values():
        if value and value in out:
            out = out.replace(value, REDACTED)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(REDACTED, out)
    return scrub_contacts(out) or ""  # shared implementation: e-mails and Kenyan MSISDNs


def _clean(text: str | None, limit: int) -> str:
    return _cap(scrub_secrets(text), limit)


def _cap(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _payload_json(payload: dict[str, Any]) -> str:
    """Serialise, trimming the long free-text fields (never the register keys) if needed."""
    text = json.dumps(payload, ensure_ascii=False)
    for key in ("data_description", "justification", "recipient"):
        if len(text) <= MAX_PAYLOAD_CHARS:
            break
        over = len(text) - MAX_PAYLOAD_CHARS
        value = str(payload.get(key) or "")
        keep = max(0, len(value) - over - 1)
        payload[key] = (value[:keep].rstrip() + "…") if keep else ""
        text = json.dumps(payload, ensure_ascii=False)
    return text[:MAX_PAYLOAD_CHARS]


# --------------------------------------------------------------------------- the gate


def _gate(
    recipient: str, paper: TransferPaperwork, cross_border: bool, enforce: bool = True
) -> tuple[TransferPaperwork, str]:
    """Return the paperwork to record and its status, or raise for an unfiled transfer."""
    if not cross_border:
        return paper, STATUS_NOT_REQUIRED
    missing = paper.missing_refs()
    if not missing:
        return paper, STATUS_FILED
    if is_demo():
        log.warning(
            "NOC_ENV=demo: cross-border transfer to %r recorded with %s=%s — %s not filed for "
            "'%s' in %s; file the DPIA and TIA before any production call.",
            recipient, "/".join(missing), DEMO_UNFILED, ", ".join(missing),
            paper.recipient_key, TRANSFERS_FILENAME,
        )
        return replace(paper, **{f: DEMO_UNFILED for f in missing}), STATUS_DEMO_UNFILED
    if not enforce:
        # An ungated channel (§7.0.10 gates the hosted LLM and residency="abroad" MCP
        # cards, not the SMTP relay). The transfer is happening either way, so the only
        # useful thing this module can do is write it down and name the gap: a row with
        # paperwork_status="unfiled" is what a DPO filters the register on.
        log.warning(
            "cross-border transfer to %r recorded as %s — %s not filed for '%s' in %s; "
            "this channel is not gated by §7.0.10, so the transfer was recorded, not refused.",
            recipient, STATUS_UNFILED, ", ".join(missing), paper.recipient_key, TRANSFERS_FILENAME,
        )
        return paper, STATUS_UNFILED
    raise TransferPaperworkMissing(recipient, paper.recipient_key, missing)


# --------------------------------------------------------------------------- the seam


def record_transfer(
    session: Session,
    *,
    recipient: str,
    recipient_country: str,
    justification: str,
    data_description: str,
    actor: str,
    actor_role: str,
    incident_id: str | None,
    residency: str,
    settings: AppSettings | None = None,
    enforce_gate: bool = True,
) -> AuditRow:
    """Write the reg 41(2) transfer record for one outbound call and return the row.

    Call this *before* the bytes leave: a refusal here (``TransferPaperworkMissing``) means
    the call must not happen at all. The row is added and flushed, so ``row.id`` is usable
    as ``envelope.governance.transfer_record_id`` inside the caller's transaction; the
    caller owns the commit.

    ``settings`` is an optional override for tests and for callers that already hold the
    resolved profile; by default the active operator profile is used.

    ``enforce_gate`` (default True — the strict side) is the §7.0.10 refusal. Leave it on
    for the hosted LLM and for any ``residency="abroad"`` MCP card, which is exactly where
    the spec makes the TIA a gating artefact. A channel outside that list passes False:
    the row is still written, with ``paperwork_status="unfiled"``, because a transfer that
    happened and was not recorded is the reg 41(2) failure — refusing to write the row
    would only make the register read as clean.
    """
    settings = settings or get_settings()
    operator_id = settings.operator.operator_id
    country = (recipient_country or "").strip().upper() or "??"
    residency_value = (residency or "").strip().lower() or "unknown"
    cross_border = is_cross_border(country, residency_value)

    paper = lookup_paperwork(load_transfers(operator_id), recipient)
    paper, status = _gate(recipient, paper, cross_border, enforce_gate)

    payload: dict[str, Any] = {
        "ts": utcnow().isoformat(),
        "recipient": _clean(recipient, _MAX_RECIPIENT),
        "recipient_country": country,
        "justification": _clean(justification, _MAX_TEXT),
        "data_description": _clean(data_description, _MAX_TEXT),
        "residency": residency_value,
        "recipient_key": paper.recipient_key,
        "cross_border": cross_border,
        "paperwork_status": status,
        "actor_role": _clean(actor_role, _MAX_ACTOR),
        "incident_id": incident_id or None,
        "operator_id": operator_id,
        "env": noc_env(),
        **paper.as_payload(),
    }
    row = AuditRow(
        id=new_id(),
        ts=utcnow(),
        operator_id=operator_id,
        actor=_clean(actor, _MAX_ACTOR) or "system",
        action=ACTION,
        entity_type="incident" if incident_id else "external_call",
        entity_id=incident_id or paper.recipient_key,
        rationale=_clean(justification, _MAX_TEXT),
        payload_json=_payload_json(payload),
    )
    session.add(row)
    session.flush()  # give the caller a row id for envelope.governance.transfer_record_id
    return row
