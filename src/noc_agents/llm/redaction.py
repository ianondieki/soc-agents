"""Redaction before every external model call (DPA 2019: only network data leaves the box).

Three groups of incident fields:

* ``ALLOWLIST`` — network/operational facts (site ids, alarm codes, counts, timestamps,
  flags, MSP company names). Several of them are typed by people (``vendor_tt_ref``,
  ``msp_name``...), so string values still pass through the scrubber below (company-name
  fields for e-mails/phones only, see ``COMPANY_FIELDS``); numbers, booleans and
  timestamps go as-is.
* ``PSEUDONYMISED`` — person names replaced by stable tokens (``<PERSON_1>``); the
  token→name map stays local so the caller can put names back into the draft.
* ``SCRUBBED_TEXT`` — free text that passes through the scrubber: e-mail addresses,
  Kenyan MSISDNs and every pseudonymised name are replaced. A registered name is also
  matched by its parts ("James" / "Mwangi" for "James Mwangi") so first-name-only
  mentions in notes are caught; parts shorter than four letters are skipped so initials
  and short words are not eaten, and role codes such as ``RNIO-NBI-E`` / ``FE-MTK-01``
  are matched whole only, so the ordinary NOC word "RNIO" survives.

Fields not listed are never sent. Two known limitations, both on the safe side of the
DPA line: a person who appears ONLY inside free text (an MSP technician named in
``resolution_summary``) is not registered and is not recognised — there is no NER here;
keep names in the structured fields. And part-matching can over-redact: an assignee
called "Moses Power" turns "power restored" into "<PERSON_n> restored", which
``restore_names`` renders back as the name. Nothing leaks; the draft just reads oddly.
Pure functions over already-loaded rows.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable

ALLOWLIST: tuple[str, ...] = (
    "incident_number", "priority", "status", "site_id", "site_type", "site_class", "region_code",
    "county", "failure_domain", "alarm_code", "tt_category", "tt_category_label", "technology",
    "users_affected", "child_sites_down", "mpesa_risk", "is_hub_major", "created_at",
    "outage_start_at", "sla_ack_due", "sla_restore_due", "restored_at", "recurrence_count",
    "msp_name", "responsible_msp", "radio_oem", "vendor_tt_ref", "msp_percent_complete",
)
# Company identifiers inside the allowlist. They are scrubbed for e-mails/phones only, never
# for person tokens: the assignee is often the MSP company itself ("EGYPRO"), and tokenising
# the company here would hide network data the model needs.
COMPANY_FIELDS: tuple[str, ...] = ("msp_name", "responsible_msp", "radio_oem")
PSEUDONYMISED: tuple[str, ...] = ("assignee_name", "fe_name", "rnio_name")
# ``title`` and ``site_name`` are a deliberate extension beyond the design-spec list: they are
# location/network data (already implied by site_id) but are free text, so they go through the scrubber.
SCRUBBED_TEXT: tuple[str, ...] = (
    "title", "site_name", "access_notes", "description", "narrative", "resolution_summary",
    "msp_root_cause", "msp_action_taken", "root_cause_hypothesis", "impact_summary",
)

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Kenyan MSISDN: +254 7xx / +254 1xx / 07xx / 01xx followed by 8 digits, not inside a longer number.
# Tightened beyond the spec's ``(\+?254|0)[17]\d{8}``: NOC staff type numbers with spaces, dots,
# hyphens or underscores ("+254 712 345 678", "0712-345-678"), a bracketed trunk zero
# ("+254 (0)733 123456", "254 0712...") or a bracketed local prefix ("(0722) 000111"), and those
# must never leave the box either.
_SEP = r"[\s.\-_]"
PHONE_RE = re.compile(
    rf"(?<!\d)(?:\+?254{_SEP}*(?:\(0\)|0)?|\(?0){_SEP}*[17](?:{_SEP}?\d){{2}}\)?(?:{_SEP}?\d){{6}}(?!\d)"
)

EMAIL_TOKEN = "<EMAIL>"
PHONE_TOKEN = "<PHONE>"
PERSON_PREFIX = "<PERSON_"


# A name part worth matching on its own: letters only, at least four of them.
_NAME_PART_RE = re.compile(r"[^\W\d_]{4,}")


def _looks_like_role_code(name: str) -> bool:
    """``RNIO-NBI-E`` / ``FE-MTK-01`` style values: a digit, or ALL-CAPS with a hyphen."""
    return any(ch.isdigit() for ch in name) or (name == name.upper() and "-" in name)


class NameMap:
    """Stable name→token assignment for one redaction pass.

    ``token_to_name`` maps each token to the full name it was created for; the private
    alias table also maps every long-enough part of that name to the same token so that
    "Kevin took over" is scrubbed when "Kevin Ochieng" is the assignee.
    """

    def __init__(self) -> None:
        self.token_to_name: dict[str, str] = {}
        self._name_to_token: dict[str, str] = {}

    def token_for(self, name: str | None) -> str | None:
        clean = (name or "").strip()
        if not clean:
            return None
        if clean not in self._name_to_token:
            token = f"{PERSON_PREFIX}{len(self.token_to_name) + 1}>"
            self._name_to_token[clean] = token
            self.token_to_name[token] = clean
            if not _looks_like_role_code(clean):  # a role code is matched whole, never by its parts
                for part in _NAME_PART_RE.findall(clean):
                    self._name_to_token.setdefault(part, token)  # first registration wins
        return self._name_to_token[clean]

    def names_longest_first(self) -> list[tuple[str, str]]:
        """Full names and their parts, longest first so "James Mwangi" wins over "James"."""
        return sorted(self._name_to_token.items(), key=lambda kv: -len(kv[0]))


def scrub_contacts(text: str | None) -> str | None:
    """Replace e-mails and MSISDNs only (used for company-name fields)."""
    if text is None:
        return None
    return PHONE_RE.sub(PHONE_TOKEN, EMAIL_RE.sub(EMAIL_TOKEN, text))


def scrub_text(text: str | None, names: NameMap) -> str | None:
    """Replace e-mails, MSISDNs and every known person name inside free text."""
    if text is None:
        return None
    out = scrub_contacts(text)
    for name, token in names.names_longest_first():
        # Whole words only: a short name such as "ATC" or "Ann" must not eat "dispatched" /
        # "Announcement". The boundary is a LETTER, not ``\w``: ``\w`` also counts "_" and
        # digits as word characters, so "peter_kamau", "Kamau_ok" and "Wanjiku2" — handles,
        # usernames, spelled-out e-mails — kept a known name verbatim (memory review M04).
        # ``[^\W\d_]`` is "a letter" in any script, so only an adjacent letter blocks a match.
        out = re.sub(rf"(?<![^\W\d_]){re.escape(name)}(?![^\W\d_])", token, out, flags=re.IGNORECASE)
    return out


def _plain(value: Any) -> Any:
    """Datetimes to ISO strings; everything else JSON-friendly as-is."""
    return value.isoformat() if hasattr(value, "isoformat") else value


def redact_incident(inc: Any, notes: Iterable[Any] = (), *, notes_limit: int = 20) -> tuple[dict[str, Any], dict[str, str]]:
    """Build the payload the model may see. Returns ``(payload, token_to_name)``.

    ``notes`` are already-loaded work-note rows (author pseudonymised, body scrubbed);
    only the newest ``notes_limit`` are included.
    """
    names = NameMap()
    payload: dict[str, Any] = {}
    for key in PSEUDONYMISED:
        payload[key] = names.token_for(getattr(inc, key, None))
    note_rows = sorted(list(notes), key=lambda n: getattr(n, "created_at", None) or datetime.min, reverse=True)[:notes_limit]
    for note in note_rows:
        names.token_for(getattr(note, "author", None))
    # Every person is registered by now, so typed allowlist strings (vendor_tt_ref, msp_name...)
    # can be scrubbed too; only non-strings (counts, flags, timestamps) go verbatim.
    for key in ALLOWLIST:
        value = getattr(inc, key, None)
        if not isinstance(value, str):
            payload[key] = _plain(value)
        elif key in COMPANY_FIELDS:
            payload[key] = scrub_contacts(value)
        else:
            payload[key] = scrub_text(value, names)
    for key in SCRUBBED_TEXT:
        payload[key] = scrub_text(getattr(inc, key, None), names)
    payload["notes"] = [
        {
            "author": names.token_for(getattr(note, "author", None)),
            "author_role": getattr(note, "author_role", None),
            "created_at": _plain(getattr(note, "created_at", None)),
            "body": scrub_text(getattr(note, "body", None), names),
        }
        for note in note_rows
    ]
    return payload, dict(names.token_to_name)


def restore_names(text: str, mapping: dict[str, str]) -> str:
    """Put real names back into model output (local only; never leaves the box)."""
    out = text
    for token, name in mapping.items():
        out = out.replace(token, name)
    return out
