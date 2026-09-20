"""Cited contract answers over custom-content documents (spec §7.8.3), parallel to ``structured.py``.

WHY THIS IS NOT ``parse_structured``
------------------------------------
Citations and structured outputs are **mutually exclusive on the API (HTTP 400)** — §7.8,
https://platform.claude.com/docs/en/build-with-claude/citations. ``structured.parse_structured``
sends ``output_format`` / ``output_config`` and validates into a Pydantic model; a cited
answer cannot use any of that. So this module calls ``client.messages.create`` directly with
``document`` blocks that have ``citations: {"enabled": True}`` and **never** sets
``output_config.format`` (pinned by ``tests/unit/test_cited_answers.py``). The "structure" of
a cited answer is recovered in Python from the response's text blocks and their citation
objects, not asked of the model.

WHY CUSTOM CONTENT DOCUMENTS
----------------------------
``llm/port.py``'s ``CitedDocument`` is a plain-text document: the API then cites by
character offsets (``char_location``), which tells us *where in the file* a quote came from
but not *which clause*. §7.8.3 wants ``block_index = clause ordinal``, so the documents here
are ``source: {type: "content", content: [{type:"text", text: clause}, ...]}`` — one block
per clause — and the API cites by ``content_block_location`` (``start_block_index`` /
``end_block_index``). ``Citation.block_index`` therefore maps straight back to a clause, and
``Citation.clause_number`` is filled from the caller's ``block_refs``. The two dataclass
families are different shapes on purpose; this module does not import the port's.

WHY ``validate_citations`` IS STRICT
------------------------------------
Stanford measured >17 % incorrect answers from commercial legal AI, including *misgrounded*
citations — a real clause number attached to a claim the clause does not make (§7.8). The
API guarantees ``cited_text`` points at a real span of the supplied document, which rules out
invented clauses but not misgrounding. So the product is not the model's prose: the caller
(``services/contracts.py``) renders a fixed template from the **validated verbatim quotes**,
and when validation fails it refuses. The validator is the gate on that: every citation must
quote its block verbatim (whitespace-normalised, because the API may re-wrap lines), and
every sentence of the answer must be covered by at least one citation. A paraphrase in
``cited_text`` fails; a sentence with no citation fails. Refusal is the designed outcome for
both, not an error.

Never raises. Returns ``(CitedAnswer | None, LlmCallRecord)`` like every other call in this
package, so the caller always has the deterministic clause list to fall back to.
``stop_reason`` is checked before any content block is read (a refusal's content is never
touched).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from noc_agents.llm.client import MODEL_DRAFTING, note_spend_limit_error, timeout_s
from noc_agents.llm.structured import LlmCallRecord, describe_error

__all__ = [
    "CITED_MAX_TOKENS",
    "CITED_MODEL",
    "Citation",
    "CitedAnswer",
    "CitedDocument",
    "SYSTEM_CITED",
    "cited_answer",
    "document_blocks",
    "validate_citations",
]

#: §7.8.4: ``claude-opus-5`` for cited answers — ZDR-*eligible*, as is the Citations feature.
#: Never Fable 5.1 here: it is a Covered Model with mandatory 30-day retention, and the text
#: leaving the building is contract clause text.
CITED_MODEL = MODEL_DRAFTING
CITED_MAX_TOKENS = 4096

SYSTEM_CITED = (
    "You are a contracts assistant for a telecom network operations centre. Answer ONLY from "
    "the supplied contract documents, quoting the clauses that govern the question. Every "
    "sentence you write must be supported by a citation to the supplied text. If the documents "
    "do not answer the question, say exactly: NO GOVERNING CLAUSE. Do not infer terms that are "
    "not written, do not compute figures the clauses do not state, and never state a service "
    "credit or monetary amount that is not quoted verbatim. Keep the answer short."
)

#: What the model is told to say when nothing applies. The validator does not special-case it:
#: an uncited sentence fails, so a refusal from the model becomes a refusal from us.
NO_GOVERNING_CLAUSE = "NO GOVERNING CLAUSE"

#: Sentence boundary for the coverage check: a full stop, question or exclamation mark
#: followed by whitespace. Clause numbers ("4.1") have no whitespace after the dot and are
#: not split; decimals ("99.3%") likewise.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
#: A sentence shorter than this many words is a fragment ("Yes.", "See below:") and is not
#: required to carry its own citation — it would otherwise force a refusal on connective text
#: the API emits as a separate, uncited block between two cited ones.
_MIN_SENTENCE_WORDS = 3
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class CitedDocument:
    """One document for the model: ``blocks`` is one entry per clause, in ordinal order.

    ``block_refs`` (optional) gives the clause number of each block, so a citation can be
    reported as "clause 4.1" rather than "block 7". Same length as ``blocks`` when given.
    """

    id: str
    title: str
    blocks: list[str]
    block_refs: list[str] = field(default_factory=list)

    def ref_for(self, block_index: int) -> str:
        if 0 <= block_index < len(self.block_refs):
            return str(self.block_refs[block_index])
        return str(block_index)


@dataclass(frozen=True)
class Citation:
    """One quoted span: which document, which block (= clause ordinal), and the model's quote."""

    document_id: str
    block_index: int
    cited_text: str
    clause_number: str


@dataclass
class CitedAnswer:
    """The model's answer as ``(sentence_text, citations)`` pairs plus the joined prose.

    "Sentence" here is one text block of the response: the API emits a new text block for each
    differently-cited span, so a block is the natural unit of "this text, those citations".
    ``raw_text`` is the concatenation, used for the sentence-coverage check and never shown to
    the asker (the caller renders a template from the citations instead).
    """

    sentences: list[tuple[str, list[Citation]]]
    raw_text: str

    @property
    def citations(self) -> list[Citation]:
        return [c for _text, cits in self.sentences for c in cits]

    @property
    def is_grounded(self) -> bool:
        return bool(self.raw_text.strip()) and bool(self.citations)


def document_blocks(docs: list[CitedDocument]) -> list[dict[str, Any]]:
    """The ``document`` content blocks for the request: custom content, citations enabled.

    Each clause is its own ``{"type": "text"}`` entry, so the API's ``content_block_location``
    indexes ARE clause ordinals. An empty block is sent as a single space rather than dropped:
    dropping it would shift every later index and misattribute every later citation.
    """
    blocks: list[dict[str, Any]] = []
    for doc in docs or []:
        blocks.append(
            {
                "type": "document",
                "source": {
                    "type": "content",
                    "content": [{"type": "text", "text": (block or " ")} for block in doc.blocks],
                },
                "title": doc.title,
                "citations": {"enabled": True},
            }
        )
    return blocks


def cited_answer(
    client: Any,
    *,
    question: str,
    docs: list[CitedDocument],
    model: str = CITED_MODEL,
    timeout: float | None = None,
    max_tokens: int = CITED_MAX_TOKENS,
) -> tuple[CitedAnswer | None, LlmCallRecord]:
    """One ``messages.create`` call with citations enabled; parsed in Python. Never raises.

    No ``output_config``, no ``output_format``, no ``tools`` — the request must stay eligible
    for citations (see the module docstring). ``stop_reason == "refusal"`` is checked before
    content is read; a response with no text block returns ``None`` with the reason in
    ``rec.error``. A spend-limit 429 opens the circuit in ``client.py`` exactly as the
    adapters do.
    """
    budget = timeout if timeout is not None else timeout_s()
    rec = LlmCallRecord(model_requested=model, max_tokens=max_tokens, timeout_s=budget)
    rec.models_tried.append(model)
    rec.model_used = model
    started = time.perf_counter()
    answer: CitedAnswer | None = None
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM_CITED,
            messages=[{"role": "user", "content": [*document_blocks(docs), {"type": "text", "text": question}]}],
            timeout=budget,
        )
        rec.model_used = getattr(resp, "model", None) or model
        rec.input_tokens += _usage(resp, "input_tokens")
        rec.output_tokens += _usage(resp, "output_tokens")
        stop = getattr(resp, "stop_reason", None)
        if stop == "refusal":  # before any content block is touched
            rec.refused = True
            rec.error = "refusal"
        else:
            answer = _read_answer(resp, docs)
            if answer is None:
                rec.error = f"no text content (stop_reason={stop!r})"
            elif stop == "max_tokens":
                # Parsed, but the last sentence may be cut; the coverage check in
                # validate_citations decides whether what survived is usable.
                rec.error = "truncated (stop_reason='max_tokens')"
    except Exception as exc:  # noqa: BLE001 — an unusable answer degrades to the clause list
        note_spend_limit_error(exc)
        rec.error = describe_error(exc)
        answer = None
    rec.latency_ms = int((time.perf_counter() - started) * 1000)
    rec.ok = answer is not None
    return answer, rec


def validate_citations(answer: CitedAnswer | None, docs: list[CitedDocument]) -> bool:
    """True only when every citation quotes its block verbatim AND every sentence is cited.

    Verbatim means the whitespace-normalised ``cited_text`` is a substring of the
    whitespace-normalised block (the API may re-wrap the text it returns). A paraphrase,
    a quote from a different block, an index outside the document, or an unknown document
    id all fail. Then every sentence of ``raw_text`` with at least ``_MIN_SENTENCE_WORDS``
    words must overlap a cited span; connective fragments between cited spans are tolerated,
    a substantive uncited sentence is not — that is the misgrounding case §7.8 is built
    against, where a true quote is followed by an unsupported conclusion.
    """
    if answer is None or not answer.raw_text.strip():
        return False
    by_id = {doc.id: doc for doc in docs or []}
    citations = answer.citations
    if not citations:
        return False
    for cit in citations:
        doc = by_id.get(cit.document_id)
        if doc is None or not (0 <= cit.block_index < len(doc.blocks)):
            return False
        quote = _norm(cit.cited_text)
        if not quote or quote not in _norm(doc.blocks[cit.block_index]):
            return False
    return _every_sentence_cited(answer)


# ------------------------------------------------------------------------------ parsing


def _read_answer(resp: Any, docs: list[CitedDocument]) -> CitedAnswer | None:
    sentences: list[tuple[str, list[Citation]]] = []
    for block in getattr(resp, "content", None) or []:
        if _get(block, "type") != "text":
            continue
        text = str(_get(block, "text") or "")
        cits: list[Citation] = []
        for raw in _get(block, "citations") or []:
            cit = _read_citation(raw, docs)
            if cit is not None:
                cits.append(cit)
        sentences.append((text, cits))
    raw_text = "".join(text for text, _c in sentences).strip()
    if not raw_text:
        return None
    return CitedAnswer(sentences=sentences, raw_text=raw_text)


def _read_citation(raw: Any, docs: list[CitedDocument]) -> Citation | None:
    """A ``content_block_location`` citation → :class:`Citation`; anything else is dropped.

    A ``char_location`` or ``page_location`` citation cannot happen for a custom-content
    document, and a citation we cannot map to a clause is worse than none — the validator
    would then have to trust it blind — so unknown shapes are dropped and the sentence they
    belonged to fails the coverage check.
    """
    index = _as_int(_get(raw, "document_index"))
    start = _as_int(_get(raw, "start_block_index"))
    if index is None or start is None or not (0 <= index < len(docs or [])):
        return None
    doc = docs[index]
    return Citation(
        document_id=doc.id,
        block_index=start,
        cited_text=str(_get(raw, "cited_text") or ""),
        clause_number=doc.ref_for(start),
    )


def _every_sentence_cited(answer: CitedAnswer) -> bool:
    """Sentence coverage over ``raw_text`` using the char spans each text block occupies."""
    spans: list[tuple[int, int]] = []
    pos = 0
    for text, cits in answer.sentences:
        end = pos + len(text)
        if cits:
            spans.append((pos, end))
        pos = end
    joined = "".join(text for text, _c in answer.sentences)
    offset = 0
    for sentence in _SENTENCE_SPLIT.split(joined):
        start = joined.find(sentence, offset)
        if start < 0:  # cannot happen for a split of the same string, but never crash a validator
            return False
        end = start + len(sentence)
        offset = end
        if len(sentence.split()) < _MIN_SENTENCE_WORDS:
            continue
        if not any(s < end and e > start for s, e in spans):
            return False
    return True


def _norm(text: str | None) -> str:
    return _WS.sub(" ", (text or "")).strip()


def _get(obj: Any, name: str) -> Any:
    """Read ``name`` off an SDK object or a plain dict (tests inject dicts)."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _usage(resp: Any, attr: str) -> int:
    usage = getattr(resp, "usage", None)
    value = getattr(usage, attr, 0) if usage is not None else 0
    return int(value or 0)


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
