"""Provider-neutral LLM port (spec §7.0.9): the seam every caller talks to.

Two operations, one shape, no provider details above this line:

* ``draft()``  — structured output validated into a Pydantic model. Both adapters
  implement it (Anthropic via ``parse_structured``, OpenAI-compatible via JSON mode
  validated client-side by the *same* Pydantic model).
* ``cite()``   — an answer with citations tied to supplied documents. Anthropic only;
  the OpenAI-compatible adapter returns ``(None, record(error="cite_unsupported"))``
  because a cited contract answer must be traceable to the document that produced it.

Both return ``(answer_or_None, LlmCallRecord)`` and NEVER raise: the caller always has a
deterministic template to fall back to, and the record says why it had to.

``LlmCallRecord`` is imported from ``structured.py`` rather than redefined — one audit
shape for every provider.

RETENTION FACTS (https://platform.claude.com/docs/en/manage-claude/api-and-data-retention).
Nothing in this package may claim otherwise:
  * Prompts and responses are NOT retained by default on the API.
  * Covered Models (Fable 5/5.1, Mythos 5/5.1) REQUIRE 30-day retention — it cannot be
    switched off for them, so ``claude-fable-5-1`` traffic is retained for 30 days.
  * Content flagged by trust-and-safety systems may be retained for UP TO 2 YEARS.
  * Zero Data Retention is per-ORGANISATION, requested from Anthropic sales, and is NOT
    in force on a self-serve Console account. ``claude-opus-5`` and Citations are ZDR-
    *eligible*; Fable 5.1 is not.
Therefore every transfer record and DPIA assumes STANDARD retention until
``LLM_ZDR_CONFIRMED=true`` is backed by a dated confirmation in docs/COMPLIANCE.md —
see ``client.zdr_confirmed()`` and ``client.llm_port_status()["retention"]``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from noc_agents.llm.structured import LlmCallRecord

# Error string the OpenAI-compatible adapter puts in the record for cite(); callers match
# on it to explain "this answer needs Anthropic" without string-sniffing a message.
CITE_UNSUPPORTED = "cite_unsupported"

PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI_COMPAT = "openai_compat"


@dataclass(frozen=True)
class CitedDocument:
    """One source document handed to ``cite()``.

    ``title`` is what a citation names, ``text`` is the content the model may quote and
    ``context`` is metadata the model may read but must not cite (clause number, file
    name, effective date). Plain text only: the port never uploads files.
    """

    title: str
    text: str
    context: str | None = None


@dataclass(frozen=True)
class Citation:
    """One quoted span with the document it came from. ``cited_text`` is the model's quote."""

    document_title: str | None
    cited_text: str
    document_index: int | None = None
    start_char: int | None = None
    end_char: int | None = None


@dataclass
class CitedAnswer:
    """``cite()``'s answer: the prose plus every span it quoted.

    An answer with no citations is still returned, but ``citations == []`` is the caller's
    signal that nothing in it is traceable to a document — treat it as unusable for a
    contract question and fall back to the template.
    """

    text: str
    citations: list[Citation] = field(default_factory=list)

    @property
    def is_grounded(self) -> bool:
        return bool(self.text.strip()) and bool(self.citations)


@runtime_checkable
class LlmPort(Protocol):
    """What the rest of the system may assume about a model provider. Nothing more."""

    provider: str  # "anthropic" | "openai_compat"

    def draft(
        self,
        *,
        model: str,
        system: str,
        user: str,
        output_model: type[BaseModel],
        effort: str = "low",
        max_tokens: int = 2048,
        timeout: float | None = None,
    ) -> tuple[BaseModel | None, LlmCallRecord]: ...

    def cite(
        self,
        *,
        model: str,
        system: str,
        question: str,
        documents: list[CitedDocument],
        max_tokens: int = 4096,
        timeout: float | None = None,
    ) -> tuple[CitedAnswer | None, LlmCallRecord]: ...


def unsupported_record(operation: str, model: str) -> LlmCallRecord:
    """Record for an operation this provider cannot do: no call was made, so no tokens,
    no latency and ``model_used=None``. ``error`` is the reason code (``cite_unsupported``)."""
    rec = LlmCallRecord(model_requested=model)
    rec.error = operation
    rec.ok = False
    return rec


def record_llm_call(
    session: Any,
    *,
    operator_id: str,
    agent: str,
    purpose: str,
    provider: str,
    rec: LlmCallRecord,
    audit_id: str,
    run_id: str | None = None,
    incident_id: str | None = None,
    cache_read_tokens: int | None = None,
    fallback_reason: str | None = None,
    validated: bool | None = None,
) -> Any:
    """Add one ``llm_calls`` row for ``rec`` and return it (the caller owns the commit).

    The table is the Wave-1 one in ``db/models.py`` (``LlmCallRow``) — imported lazily so
    this module stays importable with no database configured. ``est_cost_usd`` comes from
    ``config/llm_prices.yaml`` and is NULL for a model that table does not price, never a
    guessed number. ``audit_id`` is the ``audit_events`` row carrying the reg 41(2)
    fields; a transfer is recorded there, this row is the engineering detail.

    No prompt or output text ever reaches this row — ``rec.error`` is already reduced to a
    class name plus a content-free hint by ``structured.describe_error``.
    """
    from noc_agents.db.models import LlmCallRow, new_id, utcnow
    from noc_agents.llm.client import estimate_cost_usd

    model_used = rec.model_used or None
    est = estimate_cost_usd(
        model_used or rec.model_requested,
        rec.input_tokens,
        rec.output_tokens,
        cache_read_tokens or 0,
    )
    row = LlmCallRow(
        id=new_id(),
        ts=utcnow(),
        operator_id=operator_id,
        agent=agent,
        purpose=purpose,
        provider=provider,
        model_requested=rec.model_requested,
        model_used=model_used,
        ok=1 if rec.ok else 0,
        refused=1 if rec.refused else 0,
        fallback_used=1 if rec.fallback_used else 0,
        fallback_reason=fallback_reason,
        input_tokens=rec.input_tokens,
        output_tokens=rec.output_tokens,
        cache_read_tokens=cache_read_tokens,
        latency_ms=rec.latency_ms,
        est_cost_usd=est,
        validated=None if validated is None else (1 if validated else 0),
        run_id=run_id,
        incident_id=incident_id,
        audit_id=audit_id,
    )
    session.add(row)
    return row
