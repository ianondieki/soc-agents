"""``LlmPort`` over the Anthropic SDK. Never imports ``anthropic`` at module level.

``draft()`` is a thin wrapper around ``structured.parse_structured`` — deliberately
UNCHANGED, so the model-fallback rules, the refusal check, the token accounting and
``tests/integration/test_llm_assist.py``'s injected ``FakeClient.beta.messages.parse``
all keep working exactly as they do today. The adapter adds two things around it:

1. ``cite()`` on ``client.messages.create`` with ``document`` blocks and
   ``citations: {"enabled": True}``, for answers that must be traceable to a clause.
2. A spend-limit watcher: an HTTP 429 whose ``error_code`` is
   ``enforced_spend_limit_reached`` opens the circuit in ``client.py`` (that 429 carries
   no ``retry-after`` and SDK retries keep failing until 00:00 UTC on the 1st, so
   retrying is pure latency — https://platform.claude.com/docs/en/api/rate-limits).
   The watcher wraps the client rather than editing ``parse_structured``.

Retention: traffic on this adapter is governed by the facts recorded in ``port.py``.
``claude-fable-5-1`` is a Covered Model and its prompts ARE retained for 30 days; nothing
here may be described as zero-retention unless ``LLM_ZDR_CONFIRMED`` is true AND
docs/COMPLIANCE.md records the dated Anthropic confirmation.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel

from noc_agents.llm.client import PROVIDER_ANTHROPIC, note_spend_limit_error, timeout_s
from noc_agents.llm.port import CitedAnswer, CitedDocument, Citation
from noc_agents.llm.structured import LlmCallRecord, describe_error, parse_structured

CITATIONS_ENABLED = {"enabled": True}


class _SpendCapWatch:
    """Delegates ``beta.messages.parse`` and opens the spend circuit on the 429 that means
    "monthly limit reached". Exists so ``parse_structured`` needs no change at all."""

    __slots__ = ("_inner",)

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    @property
    def beta(self) -> Any:
        return SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs: Any) -> Any:
        try:
            return self._inner.beta.messages.parse(**kwargs)
        except Exception as exc:  # noqa: BLE001 — re-raised untouched; we only observe it
            note_spend_limit_error(exc)
            raise


class AnthropicAdapter:
    """``LlmPort`` for the hosted Anthropic API."""

    provider = PROVIDER_ANTHROPIC

    def __init__(self, client: Any) -> None:
        self._client = client

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
        """Structured output, unchanged semantics: see ``structured.parse_structured``."""
        return parse_structured(
            _SpendCapWatch(self._client),
            model=model,
            system=system,
            user=user,
            output_model=output_model,
            effort=effort,
            max_tokens=max_tokens,
            timeout=timeout,
        )

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
        """Answer ``question`` over ``documents`` with citations enabled. Never raises.

        ``stop_reason == "refusal"`` is checked BEFORE any content block is read. An answer
        that quotes nothing comes back with ``citations == []``; the caller decides whether
        an ungrounded answer is usable (for a contract question it is not).
        """
        budget = timeout if timeout is not None else timeout_s()
        rec = LlmCallRecord(model_requested=model, max_tokens=max_tokens, timeout_s=budget)
        rec.models_tried.append(model)
        rec.model_used = model
        started = time.perf_counter()
        answer: CitedAnswer | None = None
        try:
            resp = self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": [*_document_blocks(documents), {"type": "text", "text": question}]}],
                timeout=budget,
            )
            rec.model_used = getattr(resp, "model", None) or model
            rec.input_tokens += _usage(resp, "input_tokens")
            rec.output_tokens += _usage(resp, "output_tokens")
            if getattr(resp, "stop_reason", None) == "refusal":  # checked before content is read
                rec.refused = True
                rec.error = "refusal"
            else:
                answer = _read_answer(resp, documents)
                if answer is None:
                    rec.error = f"no text content (stop_reason={getattr(resp, 'stop_reason', None)!r})"
        except Exception as exc:  # noqa: BLE001 — an unusable answer must degrade to the template
            note_spend_limit_error(exc)
            rec.error = describe_error(exc)
            answer = None
        rec.latency_ms = int((time.perf_counter() - started) * 1000)
        rec.ok = answer is not None
        return answer, rec


def _document_blocks(documents: list[CitedDocument]) -> list[dict[str, Any]]:
    """One ``document`` content block per source, each with citations enabled."""
    blocks: list[dict[str, Any]] = []
    for doc in documents or []:
        block: dict[str, Any] = {
            "type": "document",
            "source": {"type": "text", "media_type": "text/plain", "data": doc.text},
            "title": doc.title,
            "citations": CITATIONS_ENABLED,
        }
        if doc.context:
            block["context"] = doc.context
        blocks.append(block)
    return blocks


def _usage(resp: Any, attr: str) -> int:
    usage = getattr(resp, "usage", None)
    value = getattr(usage, attr, 0) if usage is not None else 0
    return int(value or 0)


def _read_answer(resp: Any, documents: list[CitedDocument]) -> CitedAnswer | None:
    """Join the text blocks and collect every citation they carry."""
    parts: list[str] = []
    citations: list[Citation] = []
    for block in getattr(resp, "content", None) or []:
        if _get(block, "type") != "text":
            continue
        parts.append(str(_get(block, "text") or ""))
        for cit in _get(block, "citations") or []:
            index = _get(cit, "document_index")
            title = _get(cit, "document_title")
            if title is None and isinstance(index, int) and 0 <= index < len(documents or []):
                title = documents[index].title
            citations.append(
                Citation(
                    document_title=title,
                    cited_text=str(_get(cit, "cited_text") or ""),
                    document_index=index if isinstance(index, int) else None,
                    start_char=_as_int(_get(cit, "start_char_index")),
                    end_char=_as_int(_get(cit, "end_char_index")),
                )
            )
    text = "".join(parts).strip()
    if not text:
        return None
    return CitedAnswer(text=text, citations=citations)


def _get(obj: Any, name: str) -> Any:
    """Read ``name`` off an SDK object or a plain dict (tests inject dicts)."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
