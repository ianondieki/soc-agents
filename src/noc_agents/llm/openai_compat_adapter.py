"""``LlmPort`` over any OpenAI-compatible ``/chat/completions`` endpoint.

The point of this adapter is a NOC that costs nothing per token and keeps incident text
inside the country: ``OPENAI_COMPAT_BASE_URL`` defaults to ``http://127.0.0.1:11434/v1``
(Ollama on the NOC box) with ``OPENAI_COMPAT_MODEL`` ``qwen3:4b`` — 2.5 GB, 256K context,
https://ollama.com/library/qwen3. The same URL shape also reaches Groq or Gemini's
OpenAI-compatible endpoints, which need ``OPENAI_COMPAT_API_KEY``; those are hosted, so
the "data stays in Kenya" argument does NOT apply to them.

Structured output is JSON mode validated CLIENT-SIDE by the same Pydantic model the
Anthropic route uses (``output_model.model_validate_json``). A local model that returns
prose, a fenced block or a truncated object is unusable output: the record says so and the
caller keeps its deterministic template. The model's own JSON schema is appended to the
system prompt because JSON mode alone constrains syntax, not shape.

``cite()`` is NOT supported here and returns ``(None, record(error="cite_unsupported"))``:
a cited contract answer must be traceable to the clause it came from, and that stays on
Anthropic's Citations feature rather than being approximated by a local model.

Retention: nothing leaves the box on the default base URL. There is no provider to retain
anything — which is a property of the deployment, not a ZDR contract; see ``port.py``.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from pydantic import BaseModel

from noc_agents.llm.client import (
    PROVIDER_OPENAI_COMPAT,
    openai_compat_api_key,
    openai_compat_base_url,
    openai_compat_model,
    timeout_s,
)
from noc_agents.llm.port import CITE_UNSUPPORTED, CitedAnswer, CitedDocument, unsupported_record
from noc_agents.llm.structured import LlmCallRecord, describe_error

JSON_MODE = {"type": "json_object"}
CHAT_PATH = "/chat/completions"
MAX_SCHEMA_CHARS = 4000

# qwen3 and friends emit reasoning in <think>…</think> before the answer. A closed block is
# stripped; an unclosed one means the reply was cut off mid-thought and never reached JSON.
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


class OpenAiCompatAdapter:
    """``LlmPort`` for a local (or OpenAI-compatible hosted) endpoint."""

    provider = PROVIDER_OPENAI_COMPAT

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        post: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.base_url = (base_url or openai_compat_base_url()).rstrip("/")
        self.model = model or openai_compat_model()
        self._api_key = api_key if api_key is not None else openai_compat_api_key()
        self._post = post or _http_post  # injectable: the tests never open a socket

    # ------------------------------------------------------------------ draft

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
    ) -> tuple[BaseModel | None, LlmCallRecord]:
        """One JSON-mode call, validated into ``output_model``. Never raises.

        There is no model fallback: a local endpoint serves one model, and a second attempt
        on the same weights buys nothing but latency. ``effort`` is recorded for the audit
        row but not sent — it is an Anthropic ``output_config`` concept.
        """
        target = model or self.model
        budget = timeout if timeout is not None else timeout_s()
        rec = LlmCallRecord(model_requested=target, effort=effort, max_tokens=max_tokens, timeout_s=budget)
        rec.models_tried.append(target)
        rec.model_used = target
        payload = {
            "model": target,
            "messages": [
                {"role": "system", "content": _system_with_schema(system, output_model)},
                {"role": "user", "content": user},
            ],
            "response_format": JSON_MODE,
            "max_tokens": max_tokens,
            "stream": False,
        }
        started = time.perf_counter()
        parsed: BaseModel | None = None
        try:
            data = self._post(self.base_url + CHAT_PATH, payload, _headers(self._api_key), budget)
            parsed = self._read(data, output_model, rec)
        except Exception as exc:  # noqa: BLE001 — a dead endpoint must degrade to the template
            rec.error = describe_error(exc)
            parsed = None
        rec.latency_ms = int((time.perf_counter() - started) * 1000)
        rec.ok = parsed is not None
        return parsed, rec

    def _read(self, data: Any, output_model: type[BaseModel], rec: LlmCallRecord) -> BaseModel | None:
        if not isinstance(data, dict):
            rec.error = f"unexpected response type {type(data).__name__}"
            return None
        rec.model_used = data.get("model") or rec.model_used
        usage = data.get("usage") or {}
        if isinstance(usage, dict):
            rec.input_tokens += int(usage.get("prompt_tokens") or 0)
            rec.output_tokens += int(usage.get("completion_tokens") or 0)
        choices = data.get("choices") or []
        if not choices:
            rec.error = "no choices in response"
            return None
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message") or {}
        if message.get("refusal"):
            rec.refused = True
            rec.error = "refusal"
            return None
        finish = choice.get("finish_reason")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            rec.error = f"no text content (finish_reason={finish!r})"
            return None
        text = _strip_reasoning(content)
        if text is None:
            rec.error = f"unterminated <think> block (finish_reason={finish!r})"
            return None
        if finish == "length":
            rec.error = "truncated output (finish_reason='length')"
            return None
        body = _json_span(text)
        if body is None:
            rec.error = "no JSON object in reply"
            return None
        try:
            return output_model.model_validate_json(body)
        except Exception as exc:  # noqa: BLE001 — pydantic.ValidationError and anything json raises
            rec.error = describe_error(exc)
            return None

    # ------------------------------------------------------------------- cite

    def cite(
        self,
        *,
        model: str,
        system: str,
        question: str,
        documents: list[CitedDocument],
        max_tokens: int = 4096,
        timeout: float | None = None,
    ) -> tuple[CitedAnswer | None, LlmCallRecord]:
        """Not supported: cited contract answers stay on Anthropic. No call is made."""
        return None, unsupported_record(CITE_UNSUPPORTED, model or self.model)


# ---------------------------------------------------------------------- helpers


def _headers(api_key: str) -> dict[str, str]:
    """Bearer only when a key is configured: a local Ollama needs none and must not be sent one."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _http_post(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
    """POST JSON and return JSON. ``httpx`` is a core dependency; imported here so importing
    this module never costs a socket library."""
    import httpx

    response = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _system_with_schema(system: str, output_model: type[BaseModel]) -> str:
    """JSON mode constrains syntax, not shape, so the schema goes in the system prompt.

    Truncated schemas are dropped rather than sent half-written: a partial schema is worse
    than none. The instruction adds no incident data — only the shape of the reply.
    """
    try:
        schema = json.dumps(output_model.model_json_schema(), separators=(",", ":"))
    except Exception:  # noqa: BLE001 — a model without a schema still gets JSON mode
        schema = ""
    if not schema or len(schema) > MAX_SCHEMA_CHARS:
        return f"{system}\n\nReply with a single JSON object and nothing else."
    return (
        f"{system}\n\nReply with a single JSON object and nothing else — no prose, no markdown fence. "
        f"It must validate against this JSON Schema:\n{schema}"
    )


def _strip_reasoning(text: str) -> str | None:
    """Remove closed ``<think>`` blocks. ``None`` when a block was opened and never closed."""
    cleaned = _THINK.sub("", text)
    if "<think>" in cleaned.lower():
        return None
    return cleaned.strip()


def _json_span(text: str) -> str | None:
    """The JSON object in a reply that may still be fenced or prefixed with chatter."""
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    return text[start : end + 1]
