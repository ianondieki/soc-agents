"""One structured-output call with model fallback, expressed without importing the SDK.

``parse_structured`` makes at most two attempts: the requested model, then — only when
the requested model is the reasoning model and the failure was an API error or an
observed refusal — the fallback model. Any other failure (including a
``pydantic.ValidationError`` raised inside ``parse()`` when the reply is not JSON) is
"unusable output": the caller falls back to its template. SDK error classes are matched
by class name along the MRO so this module works when ``anthropic`` is absent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from noc_agents.llm.client import MODEL_FALLBACK, MODEL_REASONING, timeout_s

BETAS = ["server-side-fallback-2026-07-01"]  # pairs with fallbacks="default" only
FALLBACKS = "default"

# anthropic error family that justifies one client-side retry on the fallback model.
_FALLBACK_ERROR_NAMES = frozenset(
    {
        "APIError",
        "APIConnectionError",
        "APITimeoutError",
        "RateLimitError",
        "APIStatusError",
        "InternalServerError",
        "OverloadedError",
        "ServiceUnavailableError",
    }
)
# Credential problems and malformed requests fail identically on every model: no point retrying.
# (NotFoundError deliberately still falls back: an unknown model id is what the fallback is for.)
_NO_FALLBACK_NAMES = frozenset(
    {"AuthenticationError", "PermissionDeniedError", "BadRequestError", "UnprocessableEntityError"}
)


@dataclass
class LlmCallRecord:
    """What the audit row keeps about one assist call. Never contains prompt or output text
    (``error`` is an exception class plus a content-free hint, see ``describe_error``).
    The budget fields let an operator read from the audit row why a call produced nothing."""

    model_requested: str
    model_used: str | None = None
    ok: bool = False
    refused: bool = False
    fallback_used: bool = False
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    models_tried: list[str] = field(default_factory=list)
    effort: str | None = None
    max_tokens: int | None = None
    timeout_s: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_requested": self.model_requested,
            "model_used": self.model_used,
            "ok": self.ok,
            "refused": self.refused,
            "fallback_used": self.fallback_used,
            "error": self.error,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "models_tried": list(self.models_tried),
            "effort": self.effort,
            "max_tokens": self.max_tokens,
            "timeout_s": self.timeout_s,
        }


def describe_error(exc: BaseException) -> str:
    """Exception class plus a bounded, content-free hint for the audit row.

    A ``pydantic.ValidationError`` raised inside ``parse()`` embeds the model's reply in its
    message (``input_value=...``), so for anything exposing ``errors()`` only the failing
    field paths are kept. Other messages (SDK transport/status errors) carry no model text.
    """
    name = type(exc).__name__
    errors_fn = getattr(exc, "errors", None)
    if callable(errors_fn):
        try:
            errs = list(errors_fn())
            fields = sorted({".".join(str(p) for p in e.get("loc", ())) or "<root>" for e in errs})
            return f"{name}: {len(errs)} validation error(s) at {', '.join(fields)}"[:500]
        except Exception:  # noqa: BLE001 — a hint must never be worse than no hint
            return name
    status = getattr(exc, "status_code", None)
    detail = f" (status {status})" if status else f": {exc}"
    return f"{name}{detail}"[:500]


def is_fallback_error(exc: BaseException) -> bool:
    """True for the SDK's transport/status errors (matched by name, no SDK import)."""
    names = {cls.__name__ for cls in type(exc).__mro__}
    if names & _NO_FALLBACK_NAMES:
        return False
    return bool(names & _FALLBACK_ERROR_NAMES)


def _usage(resp: Any, attr: str) -> int:
    usage = getattr(resp, "usage", None)
    value = getattr(usage, attr, 0) if usage is not None else 0
    return int(value or 0)


def _attempt(client: Any, model: str, kwargs: dict[str, Any], rec: LlmCallRecord) -> tuple[BaseModel | None, bool]:
    """One ``parse`` call. Returns ``(parsed, retry_on_fallback)``; failures are described in ``rec``."""
    rec.models_tried.append(model)
    rec.model_used = model  # always the last model that received data, even when the call fails
    rec.error = None  # ``refused`` is sticky: a refusal on the first model stays in the audit record
    try:
        resp = client.beta.messages.parse(model=model, **kwargs)
    except Exception as exc:  # noqa: BLE001 — includes pydantic.ValidationError raised inside parse()
        rec.error = describe_error(exc)
        return None, is_fallback_error(exc)
    rec.model_used = getattr(resp, "model", None) or model
    rec.input_tokens += _usage(resp, "input_tokens")
    rec.output_tokens += _usage(resp, "output_tokens")
    if getattr(resp, "stop_reason", None) == "refusal":
        rec.refused = True
        rec.error = "refusal"
        return None, True
    parsed = getattr(resp, "parsed_output", None)
    if parsed is None:
        rec.error = f"no parsed output (stop_reason={getattr(resp, 'stop_reason', None)!r})"
        return None, False
    return parsed, False


def parse_structured(
    client: Any,
    *,
    model: str,
    system: str,
    user: str,
    output_model: type[BaseModel],
    effort: str = "low",
    max_tokens: int = 2048,
    timeout: float | None = None,
) -> tuple[BaseModel | None, LlmCallRecord]:
    """Call ``client.beta.messages.parse`` and validate into ``output_model``.

    Never raises. Omits ``thinking``/``temperature``/``tool_choice`` (400 on these models);
    effort goes inside ``output_config``; the timeout is explicit per request (``timeout``
    overrides the drafting default from ``LLM_TIMEOUT_S``). The fallback attempt reuses
    the same budget.
    """
    budget = timeout if timeout is not None else timeout_s()
    rec = LlmCallRecord(model_requested=model, effort=effort, max_tokens=max_tokens, timeout_s=budget)
    kwargs: dict[str, Any] = {
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_format": output_model,
        "output_config": {"effort": effort},
        "betas": BETAS,
        "fallbacks": FALLBACKS,
        "timeout": budget,
    }
    started = time.perf_counter()
    parsed, retry = _attempt(client, model, kwargs, rec)
    if parsed is None and retry and model == MODEL_REASONING:
        rec.fallback_used = True
        parsed, _ = _attempt(client, MODEL_FALLBACK, kwargs, rec)
    rec.latency_ms = int((time.perf_counter() - started) * 1000)
    rec.ok = parsed is not None
    return parsed, rec
