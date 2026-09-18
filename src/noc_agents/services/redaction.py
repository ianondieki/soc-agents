"""Shared redaction for every path that leaves the box — one implementation, re-exported.

``llm/redaction.py`` grew up next to the model client, but nothing in it is about models:
it is the DPA 2019 scrubber (allowlist, person tokens, e-mails, Kenyan MSISDNs). The
non-LLM paths — social signals, PIR prose, A2A envelopes, the transfer register in
``services/external_calls.py`` — import it from here so there is exactly ONE
implementation to review, test and fix. This module deliberately contains no logic: it is
a re-export shim. Change behaviour in ``noc_agents/llm/redaction.py``, never here.
"""

from __future__ import annotations

from noc_agents.llm.redaction import (
    ALLOWLIST,
    COMPANY_FIELDS,
    EMAIL_RE,
    EMAIL_TOKEN,
    PERSON_PREFIX,
    PHONE_RE,
    PHONE_TOKEN,
    PSEUDONYMISED,
    SCRUBBED_TEXT,
    NameMap,
    redact_incident,
    restore_names,
    scrub_contacts,
    scrub_text,
)

__all__ = [
    "ALLOWLIST",
    "COMPANY_FIELDS",
    "EMAIL_RE",
    "EMAIL_TOKEN",
    "PERSON_PREFIX",
    "PHONE_RE",
    "PHONE_TOKEN",
    "PSEUDONYMISED",
    "SCRUBBED_TEXT",
    "NameMap",
    "redact_incident",
    "restore_names",
    "scrub_contacts",
    "scrub_text",
]
