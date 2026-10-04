"""The LLM port the desk may hand to triage, with the cross-border transfer recorded first.

A complaint is personal data: a customer's words about their own line, money and phone. With
``LLM_ENABLED`` the desk may put a low-confidence triage tie to a hosted model, and DPA 2019
General Regulations reg 41(2) requires a record of every such transfer -- written BEFORE the
bytes leave, refused outright when the §7.0.10 paperwork (DPIA/TIA) is missing outside the
demo. :class:`TransferRecordingPort` does exactly that, lazily: the record is written on the
first model call, not when the port is built, so a complaint triage settles on its own (the
common case) records nothing it did not send.

The record goes through the same door every other model call uses
(``services.external_calls.record_transfer``, recipient identity from
``orchestrator.outbox.llm_recipient_identity``) and is committed before the call, so no
SQLite write lock is held while the model thinks. A refusal raises out of ``draft``; triage's
tie-break treats any exception as "no tie-break" and keeps the deterministic result.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from noc_agents.llm.client import get_llm_port, llm_enabled

log = logging.getLogger(__name__)

JUSTIFICATION = "support complaint triage tie-break (advisory; the deterministic rules decide on any failure)"
DATA_DESCRIPTION = "customer complaint text with phone numbers and e-mail addresses scrubbed; no account data"


class TransferRecordingPort:
    """Wraps an ``LlmPort``; the first ``draft``/``cite`` writes and commits the transfer record."""

    def __init__(self, port: Any, session: Session, *, actor: str, actor_role: str) -> None:
        self._port = port
        self._session = session
        self._actor = actor
        self._actor_role = actor_role
        self._recorded = False

    @property
    def provider(self) -> str:
        return self._port.provider

    def _record(self) -> None:
        if self._recorded:
            return
        from noc_agents.orchestrator.outbox import llm_recipient_identity  # lazy: only when a call happens
        from noc_agents.services.external_calls import record_transfer

        recipient, country, residency = llm_recipient_identity()
        record_transfer(
            self._session, recipient=recipient, recipient_country=country, justification=JUSTIFICATION,
            data_description=DATA_DESCRIPTION, actor=self._actor, actor_role=self._actor_role,
            incident_id=None, residency=residency, enforce_gate=True,
        )
        self._session.commit()
        self._recorded = True

    def draft(self, **kwargs: Any) -> Any:
        self._record()
        return self._port.draft(**kwargs)

    def cite(self, **kwargs: Any) -> Any:
        self._record()
        return self._port.cite(**kwargs)


def triage_port(session: Session, *, actor: str, actor_role: str) -> TransferRecordingPort | None:
    """The port for this complaint's triage, or None (the default: ``LLM_ENABLED`` is off)."""
    if not llm_enabled():
        return None
    port = get_llm_port()
    if port is None:
        return None
    return TransferRecordingPort(port, session, actor=actor, actor_role=actor_role)
