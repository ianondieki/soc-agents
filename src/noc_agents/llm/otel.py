"""OpenTelemetry GenAI attribute NAMES for one model call (spec §10.4). Names, not a pipeline.

WHAT §10.4 ASKS FOR AND WHAT THIS IS
------------------------------------
"OpenTelemetry GenAI span attribute names on steps and ``llm_calls``
(``gen_ai.request.model``, ``gen_ai.usage.input_tokens/output_tokens``, ``error.type``) —
**attribute names only**; exporter only when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set;
message content never recorded."

No OpenTelemetry SDK is installed and none is a dependency of this project (``pyproject.toml``
has no ``opentelemetry-*``), so nothing here starts a span, batches a metric or opens a socket.
What there is:

* :func:`gen_ai_attributes` — the ONE place that spells the ``gen_ai.*`` names, built from the
  :class:`~noc_agents.llm.structured.LlmCallRecord` every call in this package already produces.
  A future exporter reads this function; no caller anywhere else types an attribute name.
* :func:`call_attributes` — those names plus the local ``noc.*`` fields of the ``llm_calls`` row
  (cost, fallback reason, incident/run ids). ``noc.*`` is a private namespace on purpose: only
  the ``gen_ai.*`` and ``error.type`` keys claim to be semantic convention.
* :func:`log_gen_ai_call` — the structured log line those attributes go to today. It is called
  from ``llm/port.record_llm_call``, so **every ``llm_calls`` row has exactly one such line**,
  from every path (outbox ``LLM_CALL``, contracts, the on-demand assist routes).
* :func:`exporter_state` — what ``OTEL_EXPORTER_OTLP_ENDPOINT`` actually does right now. Setting
  it must not silently do nothing: with no SDK installed this reports ``exporting=False`` with
  ``reason="otel_sdk_not_installed"``, and :func:`log_gen_ai_call` says so once at WARNING,
  naming the endpoint that is being ignored, so an operator who configured a collector learns
  that nothing is going to arrive at it.

NO MESSAGE CONTENT, NO CREDENTIALS
----------------------------------
The attributes are model ids, token counts, a duration, a finish reason and an error *type*.
``error.type`` is deliberately only the leading class/code token of ``rec.error``
(:func:`error_type`): ``structured.describe_error`` may append an SDK message, and §10.4's
"message content never recorded" plus §9.5 mean nothing that could carry a prompt, an answer
or a credential may be attached to a span. A low-cardinality ``error.type`` is also what the
convention itself asks for.

ATTRIBUTE NAME PROVENANCE (no network was available when this was written)
--------------------------------------------------------------------------
``gen_ai.request.model``, ``gen_ai.usage.input_tokens``, ``gen_ai.usage.output_tokens`` and
``error.type`` are quoted verbatim from §10.4 of the spec, which cites
https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/main/docs/gen-ai/gen-ai-agent-spans.md

The remaining names (``gen_ai.operation.name``, ``gen_ai.provider.name``,
``gen_ai.request.max_tokens``, ``gen_ai.response.model``, ``gen_ai.response.finish_reasons``,
``gen_ai.agent.name``) are from the same convention family but were **NOT verified against the
upstream document** — this machine has no network. They are collected in :data:`GEN_AI_KEYS`
precisely so that correcting one is a one-line change here and nowhere else. Note that
``gen_ai.provider.name`` replaced the older ``gen_ai.system`` in the convention; if an exporter
needs the older name, map it in one place below.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import threading
from typing import Any

from noc_agents.llm.structured import LlmCallRecord

log = logging.getLogger("noc_agents.llm.otel")

__all__ = [
    "ENDPOINT_ENV",
    "GEN_AI_KEYS",
    "OPERATION_CHAT",
    "REASON_NO_ENDPOINT",
    "REASON_NO_EXPORTER_WIRED",
    "REASON_NO_SDK",
    "call_attributes",
    "error_type",
    "exporter_state",
    "gen_ai_attributes",
    "log_gen_ai_call",
    "otlp_endpoint",
    "reset_exporter_warning",
]

# ------------------------------------------------------------------ attribute names

#: Verbatim from spec §10.4.
ATTR_REQUEST_MODEL = "gen_ai.request.model"
ATTR_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
ATTR_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
ATTR_ERROR_TYPE = "error.type"

#: Same convention family, NOT verified upstream (see the module docstring).
ATTR_OPERATION_NAME = "gen_ai.operation.name"
ATTR_PROVIDER_NAME = "gen_ai.provider.name"
ATTR_REQUEST_MAX_TOKENS = "gen_ai.request.max_tokens"
ATTR_RESPONSE_MODEL = "gen_ai.response.model"
ATTR_RESPONSE_FINISH_REASONS = "gen_ai.response.finish_reasons"
ATTR_AGENT_NAME = "gen_ai.agent.name"

#: Every convention key this module may emit. A test asserts nothing else is prefixed
#: ``gen_ai.`` and that every one of them appears here.
GEN_AI_KEYS = (
    ATTR_OPERATION_NAME,
    ATTR_PROVIDER_NAME,
    ATTR_REQUEST_MODEL,
    ATTR_REQUEST_MAX_TOKENS,
    ATTR_RESPONSE_MODEL,
    ATTR_RESPONSE_FINISH_REASONS,
    ATTR_USAGE_INPUT_TOKENS,
    ATTR_USAGE_OUTPUT_TOKENS,
    ATTR_AGENT_NAME,
)

#: ``gen_ai.operation.name`` for the two things this system does: both ``parse_structured``
#: and ``cited_answer`` are ``messages.create``-shaped chat completions.
OPERATION_CHAT = "chat"

#: Our provider ids -> the convention's well-known ``gen_ai.provider.name`` values. An id that
#: is not in the map is passed through unchanged rather than guessed at.
_PROVIDER_NAMES = {"anthropic": "anthropic", "openai_compat": "openai"}

#: ``gen_ai.response.finish_reasons`` is a list. The record keeps no ``stop_reason`` field, so
#: the only finish reason this system can state with certainty is a refusal; anything else is
#: omitted rather than invented.
FINISH_REASON_REFUSAL = "refusal"

#: The local, non-convention namespace. Never ``gen_ai.``: these are ours.
ATTR_PURPOSE = "noc.llm.purpose"
ATTR_FALLBACK_USED = "noc.llm.fallback_used"
ATTR_FALLBACK_REASON = "noc.llm.fallback_reason"
ATTR_VALIDATED = "noc.llm.validated"
ATTR_EST_COST_USD = "noc.llm.est_cost_usd"
ATTR_LATENCY_MS = "noc.llm.latency_ms"
ATTR_MODELS_TRIED = "noc.llm.models_tried"
ATTR_CACHE_READ_TOKENS = "noc.llm.cache_read_tokens"
ATTR_CALL_ID = "noc.llm.call_id"
ATTR_OPERATOR_ID = "noc.operator_id"
ATTR_INCIDENT_ID = "noc.incident_id"
ATTR_RUN_ID = "noc.run_id"
ATTR_AUDIT_ID = "noc.audit_id"

#: Fallback ``error.type`` when the call produced nothing usable but said nothing about why.
ERROR_TYPE_UNUSABLE = "unusable_output"
#: Longest ``error.type`` this module will emit. A convention attribute is a label, not a message.
_MAX_ERROR_TYPE_CHARS = 64


# ------------------------------------------------------------------ the mapping


def error_type(rec: LlmCallRecord) -> str | None:
    """``error.type`` for one call: ``None`` when it succeeded, else a low-cardinality token.

    ``rec.error`` is produced by ``structured.describe_error``, which is
    ``"<ExceptionClass>: <hint>"`` or ``"<ExceptionClass> (status 401)"`` — and the hint may be
    an SDK message. Only the leading token is kept, so a 401 whose message echoes the API key
    contributes ``"AuthenticationError"`` and nothing else. Also bounded in length, because an
    attribute value that grows without limit is neither a label nor safe.
    """
    if rec.ok:
        return None
    if rec.refused:
        return FINISH_REASON_REFUSAL
    head = (rec.error or "").split(":", 1)[0].split("(", 1)[0].strip()
    return (head or ERROR_TYPE_UNUSABLE)[:_MAX_ERROR_TYPE_CHARS]


def gen_ai_attributes(
    rec: LlmCallRecord,
    *,
    provider: str,
    agent: str | None = None,
    operation: str = OPERATION_CHAT,
) -> dict[str, Any]:
    """The ``gen_ai.*`` (plus ``error.type``) attributes for one call. No message content.

    Keys whose value is unknown are omitted rather than emitted as ``None``: an absent
    attribute and an attribute set to null are not the same statement.
    """
    attrs: dict[str, Any] = {
        ATTR_OPERATION_NAME: operation,
        ATTR_PROVIDER_NAME: _PROVIDER_NAMES.get(provider, provider),
        ATTR_REQUEST_MODEL: rec.model_requested,
        ATTR_USAGE_INPUT_TOKENS: int(rec.input_tokens or 0),
        ATTR_USAGE_OUTPUT_TOKENS: int(rec.output_tokens or 0),
    }
    if rec.max_tokens is not None:
        attrs[ATTR_REQUEST_MAX_TOKENS] = int(rec.max_tokens)
    if rec.model_used:
        attrs[ATTR_RESPONSE_MODEL] = rec.model_used
    if rec.refused:
        attrs[ATTR_RESPONSE_FINISH_REASONS] = [FINISH_REASON_REFUSAL]
    if agent:
        attrs[ATTR_AGENT_NAME] = agent
    err = error_type(rec)
    if err is not None:
        attrs[ATTR_ERROR_TYPE] = err
    return attrs


def call_attributes(
    rec: LlmCallRecord,
    *,
    provider: str,
    agent: str | None = None,
    purpose: str | None = None,
    operation: str = OPERATION_CHAT,
    row: Any = None,
) -> dict[str, Any]:
    """:func:`gen_ai_attributes` plus the local ``noc.*`` fields of the ``llm_calls`` row.

    ``row`` is read by ``getattr`` only (never imported, never queried), so this module stays
    importable with no database configured and a test can pass any object with the same field
    names. Nothing here is text the model wrote or was given.
    """
    attrs = gen_ai_attributes(rec, provider=provider, agent=agent, operation=operation)
    attrs[ATTR_LATENCY_MS] = int(rec.latency_ms or 0)
    attrs[ATTR_FALLBACK_USED] = bool(rec.fallback_used)
    if rec.models_tried:
        attrs[ATTR_MODELS_TRIED] = list(rec.models_tried)
    if purpose:
        attrs[ATTR_PURPOSE] = purpose
    for key, field in (
        (ATTR_CALL_ID, "id"),
        (ATTR_OPERATOR_ID, "operator_id"),
        (ATTR_INCIDENT_ID, "incident_id"),
        (ATTR_RUN_ID, "run_id"),
        (ATTR_AUDIT_ID, "audit_id"),
        (ATTR_EST_COST_USD, "est_cost_usd"),
        (ATTR_FALLBACK_REASON, "fallback_reason"),
        (ATTR_CACHE_READ_TOKENS, "cache_read_tokens"),
    ):
        value = getattr(row, field, None) if row is not None else None
        if value is not None:
            attrs[key] = value
    validated = getattr(row, "validated", None) if row is not None else None
    if validated is not None:
        attrs[ATTR_VALIDATED] = bool(validated)
    return attrs


# ------------------------------------------------------------------ the exporter question

ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"

REASON_NO_ENDPOINT = "no_endpoint_configured"
REASON_NO_SDK = "otel_sdk_not_installed"
REASON_NO_EXPORTER_WIRED = "no_exporter_wired"

_WARNED: set[str] = set()
_WARN_LOCK = threading.Lock()


def otlp_endpoint() -> str | None:
    """``OTEL_EXPORTER_OTLP_ENDPOINT``, or ``None`` when unset or blank."""
    return (os.getenv(ENDPOINT_ENV) or "").strip() or None


def _sdk_installed() -> bool:
    """True when ``opentelemetry.sdk`` is importable. Never raises, never imports it."""
    try:
        return importlib.util.find_spec("opentelemetry.sdk") is not None
    except (ImportError, ValueError):  # a broken or shadowed parent package
        return False


def exporter_state() -> dict[str, Any]:
    """What the endpoint setting does right now. ``exporting`` is the honest answer.

    Three cases, all of them ``exporting=False`` today:

    * no endpoint set — ``no_endpoint_configured``, nothing to explain;
    * endpoint set, no SDK — ``otel_sdk_not_installed``: the project deliberately does not
      depend on ``opentelemetry-sdk`` (§10.4 asks for the names, not a tracing backend);
    * endpoint set, SDK importable — ``no_exporter_wired``: this code still starts no spans,
      so the collector would receive nothing from it.
    """
    endpoint = otlp_endpoint()
    installed = _sdk_installed()
    if endpoint is None:
        reason = REASON_NO_ENDPOINT
    elif not installed:
        reason = REASON_NO_SDK
    else:
        reason = REASON_NO_EXPORTER_WIRED
    return {"endpoint": endpoint, "sdk_installed": installed, "exporting": False, "reason": reason}


def reset_exporter_warning() -> None:
    """Forget which endpoints have already been warned about (tests; a config reload)."""
    with _WARN_LOCK:
        _WARNED.clear()


def _warn_if_endpoint_is_inert() -> dict[str, Any]:
    """Say once, per endpoint value, that a configured collector will receive nothing.

    A documented environment variable that is read by nothing is the gap CONFORMANCE C-22
    names. It is now read: it does not create an exporter (there is none to create), but it
    can no longer be set in silence.
    """
    state = exporter_state()
    endpoint = state["endpoint"]
    if endpoint is None:
        return state
    with _WARN_LOCK:
        if endpoint in _WARNED:
            return state
        _WARNED.add(endpoint)
    log.warning(
        "%s=%s is set but nothing is exported (%s): no OpenTelemetry SDK is a dependency of this "
        "project, so the GenAI attributes below are written to this log only. Spec §10.4 asks for "
        "the attribute names; wiring an exporter is a separate, explicit decision.",
        ENDPOINT_ENV,
        endpoint,
        state["reason"],
    )
    return state


def log_gen_ai_call(attrs: dict[str, Any]) -> dict[str, Any]:
    """Emit one structured log line carrying ``attrs``; returns ``attrs`` for the caller's tests.

    ``extra={"gen_ai": attrs}`` keeps the dotted keys out of the ``LogRecord`` namespace and
    hands a JSON formatter (or an exporter, one day) the whole mapping in one field; the message
    itself repeats them as ``key=value`` so the default plain-text handler is not useless.
    Never raises: observability may not be able to break a model call that already happened.
    """
    try:
        _warn_if_endpoint_is_inert()
        if log.isEnabledFor(logging.INFO):
            rendered = " ".join(f"{key}={attrs[key]!r}" for key in sorted(attrs))
            log.info("gen_ai.call %s", rendered, extra={"gen_ai": attrs})
    except Exception:  # noqa: BLE001 — a log line must never fail a call that already happened
        log.debug("gen_ai.call could not be logged", exc_info=True)
    return attrs
