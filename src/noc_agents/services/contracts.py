"""Contract clause retrieval and cited advisory answers (spec §7.8, Phase 5 Lane 5B,
``CONTRACTS_ENABLED=false``).

THE CORPUS WAS MEASURED FIRST, AS §7.8 REQUIRES — READ THIS BEFORE TUNING RETRIEVAL
------------------------------------------------------------------------------------
The two synthetic sample contracts under ``data/seed/v2/contracts/`` total **96 numbered
clauses, 18,517 characters / 3,094 words of clause text, ≈ 4,663 tokens** on the chars/4 rule
of thumb, summed per clause (measured 2026-09-20 with :func:`measure_corpus`; the raw files
including banners and front matter are ~25.7k characters). Anthropic's own
guidance, cited in §7.8, is that below ~200,000 tokens you put the corpus in the prompt and
skip RAG. This corpus is ~2.3 % of that line. **The honest reading is that this corpus does
not need retrieval yet.** The retrieval path is built anyway because it is what this lane
exists to deliver for the day Supply Chain hands over the real MSA/SLA set — and because it
also drives the two things that do not depend on corpus size: the *deterministic* answer
with ``LLM_ENABLED=false`` and the *nearest clauses* offered with every refusal.

The two facts are reconciled in :func:`answer_question` rather than papered over: when the
asker's allowed corpus fits under :data:`PROMPT_CORPUS_TOKEN_CEILING` the model is handed
**every** allowed clause (prompt-stuffing, per the guidance), and only above the ceiling is
it handed the BM25 top-k. Either way BM25 ranks the clauses for the deterministic list and
the refusal's nearest-clauses, and the golden set measures that ranking
(``tests/unit/test_contract_eval.py`` — recall@20 and MRR are computed, not asserted by
hand).

THE TENANT FILTER IS STRUCTURALLY IMPOSSIBLE TO OMIT
----------------------------------------------------
Contracts are mutually confidential across MSPs. §7.8: an omitted partition filter "returns
everything". So:

* :func:`retrieve_clauses` takes ``allowed_contract_ids`` as a **required keyword argument
  with no default**, and an **empty** allow-set raises ``ValueError`` — never a silent full
  scan and never a silent empty result that a caller could mistake for "no match".
* The allow-set is computed **server-side only** by :func:`allowed_contracts_for` from the
  principal's role ∩ ``contracts.allowed_roles_json``, narrowed to the incident's vendor.
  ``POST /contracts/ask`` ignores any allow-set in the body (the router never reads one).
* The SQL carries ``contract_id IN (...)`` **and** an operator clause built from
  ``api.deps._owned`` (the one door for operator scoping, §8), and every returned row is
  re-checked against the allow-set in Python. The re-check can only *drop* rows — the
  fail-safe direction.

WHAT IS AND IS NOT "OFFICIAL"
-----------------------------
Only a hit on the Legal-curated ``contract_faq`` is ``official=True`` (``source="faq"``), and
it is returned before any generation is attempted. A model answer is rendered from its
*validated verbatim citations* through a fixed template with a disclosure line, never from
the model's prose (Stanford's >17 % misgrounding figure, §7.8). When no citation survives
:func:`~noc_agents.llm.cited.validate_citations` the answer is a **refusal** with the three
nearest clauses and ``escalated_to_legal=1`` — a first-class outcome, tested as such.

DATA LEAVING THE BUILDING
-------------------------
A clause reaches a hosted model only when its contract has
``third_party_processing_permitted=1``, after ``services.external_calls.record_transfer``
has written the reg 41(2) row (recipient "Anthropic API", justification "SLA clarification"),
after ``llm.client.spend_gate`` has said yes, and after :func:`redact_clause_text` has
scrubbed e-mails, MSISDNs and signature lines. There is no NER: a signatory's name inside a
clause body is not recognised (the same honest limit ``llm/redaction.py`` records), which is
one more reason Legal checks each contract before ingest. Nothing here writes a
money-bearing field or a regulator submission; no route exists that turns an answer into a
credit.

Every public read degrades to empty rather than raising (as ``services/memory.py`` does);
the two exceptions are the deliberate ``ValueError`` above and :class:`ContractIngestError`
from ingest, which the router turns into a 4xx.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml
from sqlalchemy import select
from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from noc_agents.api.deps import _owned, _settings
from noc_agents.db.models import AuditRow, IncidentRow, new_id, utcnow
from noc_agents.db.models_contracts import ContractClauseRow, ContractFaqRow, ContractQueryRow, ContractRow
from noc_agents.db.models_vendors import VendorRow
from noc_agents.llm import client as llm_client
from noc_agents.llm.cited import CITED_MODEL, CitedDocument, cited_answer, validate_citations
from noc_agents.llm.port import PROVIDER_ANTHROPIC, record_llm_call
from noc_agents.llm.redaction import scrub_contacts
from noc_agents.services.external_calls import TransferPaperworkMissing, record_transfer
from noc_agents.services.vendors import incident_as_of, incident_vendor_name, resolve_vendor

log = logging.getLogger("noc_agents.services.contracts")

__all__ = [
    "CONTRACTS_ENABLED_ENV",
    "CONTRACTS_DIR",
    "DISCLOSURE",
    "MAX_SOURCE_BYTES",
    "PROMPT_CORPUS_TOKEN_CEILING",
    "REFUSAL_TEXT",
    "SEED_CONTRACTS_DIR",
    "SOURCE_DETERMINISTIC",
    "SOURCE_FAQ",
    "SOURCE_LLM",
    "SOURCE_REFUSED",
    "Clause",
    "ClauseHit",
    "ContractAnswer",
    "ContractIngestError",
    "CorpusMeasurement",
    "EvalItem",
    "EvalReport",
    "IngestResult",
    "allowed_contracts_for",
    "answer_question",
    "answer_out",
    "chunk_clauses",
    "clause_hit_out",
    "contract_out",
    "contracts_enabled",
    "corpus_status",
    "create_faq",
    "ensure_fts",
    "estimate_tokens",
    "evaluate_retrieval",
    "faq_out",
    "fts_query",
    "ingest_contract",
    "ingest_seed_samples",
    "list_contracts_for",
    "match_faq",
    "measure_corpus",
    "query_out",
    "reciprocal_rank",
    "recall_at_k",
    "redact_clause_text",
    "resolve_source_path",
    "retrieve_clauses",
    "split_front_matter",
]

# --------------------------------------------------------------------------- flag

#: §7.8 / Appendix B. An environment variable like the other lane flags; default **false**.
CONTRACTS_ENABLED_ENV = "CONTRACTS_ENABLED"
_TRUE = frozenset({"1", "true", "yes", "on"})


def contracts_enabled() -> bool:
    """``CONTRACTS_ENABLED`` — default **false**. Read at call time so tests flip it with
    ``monkeypatch`` and the demo flips it from ``.env`` between runs."""
    return (os.getenv(CONTRACTS_ENABLED_ENV) or "").strip().lower() in _TRUE


# --------------------------------------------------------------------------- constants

_ROOT = Path(__file__).resolve().parents[3]  # .../src/noc_agents/services/contracts.py -> repo root
#: §7.8.2: ingest reads files from here. Nothing is ever read from an arbitrary path.
CONTRACTS_DIR = _ROOT / "data" / "contracts"
#: The two synthetic samples (``scripts/seed_v2.py`` owns their ``contracts`` rows; this
#: module owns their clauses).
SEED_CONTRACTS_DIR = _ROOT / "data" / "seed" / "v2" / "contracts"
#: §7.8.2: ≤ 20 MB per source file.
MAX_SOURCE_BYTES = 20 * 1024 * 1024
#: Anthropic's "below ~200k tokens, put it in the prompt" line (§7.8).
PROMPT_CORPUS_TOKEN_CEILING = 200_000
#: chars-per-token rule of thumb. There is no tokenizer dependency (rule 6: none added), and
#: ``count_tokens`` on the API is a network call. An estimate is enough to place a corpus on
#: one side or the other of a 200k line; ``CorpusMeasurement`` says it is an estimate.
CHARS_PER_TOKEN = 4

#: ``contract_queries.source`` vocabulary (§7.8.1).
SOURCE_FAQ = "faq"
SOURCE_LLM = "llm"
SOURCE_DETERMINISTIC = "deterministic"
SOURCE_REFUSED = "refused"

#: §7.8.3 fixed strings. The disclosure is on EVERY non-FAQ answer (acceptance §7.8.7).
DISCLOSURE = (
    "This is an AI-assisted reading, not a legal or commercial determination — "
    "confirm with Legal/Commercial before quoting externally."
)
REFUSAL_TEXT = "No governing clause found — escalate to Legal/Commercial."
REFUSAL_ACCESS_TEXT = "I could not find this in the contracts you have access to."
DETERMINISTIC_NOTE = (
    "Clause list only: no model was consulted, so no relevance judgement has been made — "
    "these are the closest clauses by lexical match, quoted verbatim."
)
FAQ_DISCLOSURE_PREFIX = "Official answer curated by Legal"

#: Retrieval defaults. 20 is the measured optimum §7.8 cites; the hard cap stops a query
#: string from turning a search into a table dump.
DEFAULT_K = 20
MAX_K = 100
#: How many clauses the deterministic answer and the refusal quote in the text body. The
#: full top-k is still returned as ``nearest_clauses``; the *answer text* stays short because
#: a twenty-clause wall of text read at 03:00 is not an answer.
DETERMINISTIC_LIST_SIZE = 5
REFUSAL_NEAREST = 3
#: bm25 column weights, in the FTS table's column order (clause_id, contract_id UNINDEXED →
#: 0). ``context_header`` is weighted highest: it is the §7.8 contextual prefix, and a clause
#: about response times says "Response Time" once while its heading says it every time.
_W_HEADING, _W_CONTEXT, _W_TEXT = 2.0, 3.0, 1.0

#: FAQ matching threshold on token coverage — see :func:`match_faq`.
FAQ_MATCH_THRESHOLD = 0.6

_FTS_TABLE = "contract_clauses_fts"
_FTS_DDL = (
    f"CREATE VIRTUAL TABLE IF NOT EXISTS {_FTS_TABLE} USING fts5("
    "clause_id UNINDEXED, contract_id UNINDEXED, heading, context_header, text, "
    "tokenize='porter unicode61')"
)

#: Recipient naming matches ``config/operators/<op>/transfers.yaml`` (key ``anthropic_api``).
TRANSFER_RECIPIENT = "Anthropic API"
TRANSFER_COUNTRY = "US"
TRANSFER_JUSTIFICATION = "SLA clarification"
TRANSFER_DATA_DESCRIPTION = "redacted clause text + question"
AGENT_NAME = "ContractsAssistant"
LLM_PURPOSE = "contract_cited_answer"
AUDIT_ACTION = "CONTRACT_QUERY"


# --------------------------------------------------------------------------- measurement


@dataclass(frozen=True)
class CorpusMeasurement:
    """§7.8's "measure the corpus first", as a value rather than a remark in a doc."""

    files: int
    clauses: int
    chars: int
    words: int
    est_tokens: int
    ceiling: int = PROMPT_CORPUS_TOKEN_CEILING

    @property
    def fits_in_prompt(self) -> bool:
        return self.est_tokens <= self.ceiling

    @property
    def fraction_of_ceiling(self) -> float:
        return round(self.est_tokens / float(self.ceiling), 4) if self.ceiling else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "files": self.files,
            "clauses": self.clauses,
            "chars": self.chars,
            "words": self.words,
            "est_tokens": self.est_tokens,
            "estimate_rule": f"chars/{CHARS_PER_TOKEN}",
            "ceiling_tokens": self.ceiling,
            "fraction_of_ceiling": self.fraction_of_ceiling,
            "fits_in_prompt": self.fits_in_prompt,
            "implication": (
                "below the ~200k-token line: prompt-stuffing suffices, retrieval is not yet needed for quality"
                if self.fits_in_prompt
                else "above the ~200k-token line: retrieval is required"
            ),
        }


def estimate_tokens(text: str | None) -> int:
    """chars/4, rounded up, never zero for non-empty text. An estimate, labelled as one."""
    n = len(text or "")
    return int(math.ceil(n / CHARS_PER_TOKEN)) if n else 0


def measure_corpus(paths: Iterable[Path]) -> CorpusMeasurement:
    """Measure the clause bodies of the given files (front matter and banners excluded)."""
    files = clauses = chars = words = est = 0
    for path in paths:
        meta, body = split_front_matter(Path(path).read_text(encoding="utf-8"))
        title = str(meta.get("title") or Path(path).stem)
        parts = chunk_clauses(body, title=title)
        files += 1
        clauses += len(parts)
        chars += sum(len(c.text) for c in parts)
        words += sum(len(c.text.split()) for c in parts)
        est += sum(estimate_tokens(c.text) for c in parts)
    return CorpusMeasurement(files=files, clauses=clauses, chars=chars, words=words, est_tokens=est)


# --------------------------------------------------------------------------- chunking

#: §7.8.3: one numbered clause or sub-clause per row, ``^\s*(\d+(\.\d+)*)\s+``.
_CLAUSE_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\s+(\S.*)$")
#: ``## 4. Service Levels — ...`` (markdown section with a leading number) or ``## Schedule``.
_SECTION_RE = re.compile(r"^#{1,6}\s+(?:(\d+)\.?\s+)?(.+?)\s*$")
_FENCE_RE = re.compile(r"^\s*```")
_RULE_RE = re.compile(r"^\s*-{3,}\s*$")
_WS = re.compile(r"\s+")
_GIST_WORDS = 12


@dataclass(frozen=True)
class Clause:
    """One chunk, before it is a row."""

    clause_number: str
    heading: str
    parent_path: str
    text: str
    context_header: str
    ordinal: int


def split_front_matter(raw: str) -> tuple[dict[str, Any], str]:
    """``(front matter, body)`` for a ``---`` fenced markdown file; ``({}, raw)`` without one.

    Same semantics as ``scripts/seed_v2._split_front_matter`` (stop at the FIRST line that is
    exactly ``---``, because both samples contain markdown horizontal rules further down),
    re-implemented here so a service module does not import from ``scripts``.
    """
    if not raw.startswith("---"):
        return {}, raw
    lines = raw[3:].split("\n")
    for i, line in enumerate(lines):
        if line.strip() == "---":
            try:
                meta = yaml.safe_load("\n".join(lines[:i])) or {}
            except yaml.YAMLError:
                return {}, raw
            if not isinstance(meta, dict):
                return {}, raw
            return meta, "\n".join(lines[i + 1 :]).lstrip("\n")
    return {}, raw


def chunk_clauses(body: str, *, title: str) -> list[Clause]:
    """Clause-boundary chunking (§7.8.3): one numbered clause per chunk, tables kept with it.

    A chunk starts at a line matching ``_CLAUSE_RE`` and runs to the next clause start or the
    next section heading. Everything in between — continuation prose, a markdown table, a
    blank line — belongs to the clause that introduced it, which is what keeps clause 2.2's
    "Rural, beyond 90 km | 240 minutes" row with clause 2.2 (golden q02). Fenced code blocks
    (the SYNTHETIC banners) and horizontal rules are skipped. Text before the first clause
    (title, banner prose) is not a clause and is dropped.

    ``heading`` is the enclosing section heading verbatim — including the "(SAMPLE —
    FICTIONAL)" marking the seed tests insist survives into ``context_header``.
    ``context_header`` is derived, not hand-written: ``"<title> §<n> <heading> — <first 12
    words>"``. §7.8.3 allows hand-written headers for small sets; a deterministic derivation
    was chosen instead so that re-ingesting the same file always produces the same index, and
    so no human has to keep 96 one-liners in sync with the text.
    """
    clauses: list[Clause] = []
    section_no = ""
    section_heading = ""
    current_no: str | None = None
    current_lines: list[str] = []
    in_fence = False

    def flush() -> None:
        nonlocal current_no, current_lines
        if current_no is not None:
            text = _clean_block(current_lines)
            if text:
                clauses.append(_make_clause(title, section_no, section_heading, current_no, text, len(clauses)))
        current_no, current_lines = None, []

    for line in body.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        section = _SECTION_RE.match(line)
        if section:
            flush()
            section_no = section.group(1) or ""
            section_heading = section.group(2).strip()
            continue
        if _RULE_RE.match(line):
            flush()
            continue
        clause = _CLAUSE_RE.match(line)
        if clause and not line.lstrip().startswith("|"):
            flush()
            current_no = clause.group(1)
            current_lines = [clause.group(2)]
            continue
        if current_no is not None:
            current_lines.append(line)
    flush()
    return clauses


def _clean_block(lines: Sequence[str]) -> str:
    out = [ln.rstrip() for ln in lines]
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out).strip()


def _make_clause(title: str, section_no: str, section_heading: str, number: str, text: str, ordinal: int) -> Clause:
    parts = number.split(".")
    path_bits = [".".join(parts[: i + 1]) for i in range(len(parts))]
    if section_no and path_bits[0] == section_no and section_heading:
        path_bits[0] = f"{section_no} {section_heading}"
    gist = " ".join(_WS.sub(" ", text.replace("|", " ")).split()[:_GIST_WORDS])
    heading_bit = f" {section_heading}" if section_heading else ""
    return Clause(
        clause_number=number,
        heading=section_heading,
        parent_path=" > ".join(path_bits),
        text=text,
        context_header=f"{title} §{number}{heading_bit} — {gist}",
        ordinal=ordinal,
    )


# --------------------------------------------------------------------------- ingest


class ContractIngestError(ValueError):
    """A source file the ingest refuses. ``status`` is the HTTP code the router should use."""

    def __init__(self, message: str, *, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class IngestResult:
    contract: ContractRow
    clauses: int
    est_tokens: int
    replaced: bool  # True when an existing row (seeded or previously ingested) was updated


def resolve_source_path(relative: str) -> Path:
    """A path under ``data/contracts/`` or ``data/seed/v2/contracts/``, or a 422.

    Resolved and then checked to be *inside* one of the two roots, so ``../`` and absolute
    paths cannot reach anything else on disk. Ingest is Legal/admin only, but the file system
    is still not something a request body gets to name freely.
    """
    rel = (relative or "").strip().replace("\\", "/")
    if not rel:
        raise ContractIngestError("path is required")
    candidates = [CONTRACTS_DIR, SEED_CONTRACTS_DIR]
    raw = Path(rel)
    if raw.is_absolute():
        raise ContractIngestError("path must be relative to data/contracts/ or data/seed/v2/contracts/")
    for root in candidates:
        for base in (root, _ROOT):
            candidate = (base / raw).resolve()
            try:
                candidate.relative_to(root.resolve())
            except ValueError:
                continue
            if candidate.is_file():
                return candidate
    raise ContractIngestError("path must name an existing file under data/contracts/ or data/seed/v2/contracts/", status=404)


def _check_source_bytes(raw: bytes, path: Path) -> None:
    """§7.8.2: ≤ 20 MB, type decided by magic bytes, not by extension.

    PDF is refused outright rather than half-supported: text extraction would need a new
    dependency (rule 6), and a PDF whose text layer is missing would silently index nothing.
    Convert to Markdown/TXT first; that is also what makes the clause regex meaningful.
    """
    if len(raw) > MAX_SOURCE_BYTES:
        raise ContractIngestError(f"{path.name} is larger than 20 MB", status=413)
    if raw.startswith(b"%PDF"):
        raise ContractIngestError("PDF is not indexed here — convert to Markdown/TXT (no PDF text-extraction dependency)", status=415)
    if b"\x00" in raw[:4096]:
        raise ContractIngestError("binary content refused; only Markdown/TXT is indexed", status=415)


def ingest_contract(
    session: Session,
    *,
    path: Path,
    operator_id: str | None = None,
    contract_id: str | None = None,
    counterparty_vendor_id: str | None = None,
    title: str | None = None,
    effective_date: date | str | None = None,
    version: str | None = None,
    confidentiality_checked_by: str | None = None,
    third_party_processing_permitted: bool | int | None = None,
    allowed_roles: Sequence[str] | None = None,
) -> IngestResult:
    """Chunk one Markdown/TXT contract into ``contract_clauses`` and rebuild its FTS rows.

    Explicit arguments win; the file's YAML front matter fills what is missing (the two
    samples carry everything). Refuses (``ContractIngestError``) when: the file is too large
    or not text; ``confidentiality_checked_by`` is empty — Legal has not looked at it; no
    ``counterparty_vendor_id`` can be found — a contract that belongs to no vendor cannot be
    narrowed by incident and would leak to every asker; or no clause was found. Upserts by
    ``id`` (front matter ``id`` or the argument), else by ``(operator_id, source_file)``, so a
    row that ``noc-seed-v2`` already wrote is completed rather than duplicated. Flushes; the
    caller commits.
    """
    raw = path.read_bytes()
    _check_source_bytes(raw, path)
    meta, body = split_front_matter(raw.decode("utf-8", errors="replace"))
    op_id = operator_id or _settings().operator.operator_id

    resolved_title = (title or str(meta.get("title") or "")).strip() or path.stem
    vendor = (counterparty_vendor_id or str(meta.get("counterparty_vendor_id") or "")).strip()
    if not vendor:
        raise ContractIngestError("counterparty_vendor_id is required (a contract must belong to one vendor)")
    checked_by = (confidentiality_checked_by or str(meta.get("confidentiality_basis") or "")).strip()
    if not checked_by:
        raise ContractIngestError("confidentiality_checked_by is required: Legal must check the confidentiality clause before ingest")
    permitted = third_party_processing_permitted
    if permitted is None:
        permitted = meta.get("third_party_processing_permitted", 0)
    roles = list(allowed_roles) if allowed_roles is not None else list(meta.get("allowed_roles") or [])
    eff = _as_date(effective_date if effective_date is not None else meta.get("effective_date"))

    clauses = chunk_clauses(body, title=resolved_title)
    if not clauses:
        raise ContractIngestError("no numbered clauses found (expected lines like '4.1 The Service Provider shall ...')")

    try:
        source_file = str(path.resolve().relative_to(_ROOT.resolve())).replace(os.sep, "/")
    except ValueError:
        source_file = path.name
    row_id = (contract_id or str(meta.get("id") or "")).strip() or None
    row = None
    if row_id:
        row = session.scalar(select(ContractRow).where(ContractRow.id == row_id, ContractRow.operator_id == op_id))
    if row is None:
        row = session.scalar(select(ContractRow).where(ContractRow.operator_id == op_id, ContractRow.source_file == source_file))
    replaced = row is not None
    if row is None:
        row = ContractRow(id=row_id or new_id(), operator_id=op_id)
        session.add(row)

    est = sum(estimate_tokens(c.text) for c in clauses)
    row.counterparty_vendor_id = vendor
    row.title = resolved_title
    row.effective_date = eff
    row.version = (version or str(meta.get("version") or "")).strip() or "1"
    row.source_file = source_file
    row.sha256 = hashlib.sha256(raw).hexdigest()
    row.confidentiality_checked_by = checked_by
    row.confidentiality_checked_at = row.confidentiality_checked_at or utcnow()
    row.third_party_processing_permitted = 1 if str(permitted).strip().lower() in _TRUE else 0
    row.allowed_roles = roles
    row.token_count = est
    row.ingested_at = utcnow()
    session.flush()

    ensure_fts(session)
    # Rebuild, never merge: the index must equal the file. A clause renumbered in a new
    # version would otherwise leave its old row (and its old FTS entry) behind.
    session.execute(sql(f"DELETE FROM {_FTS_TABLE} WHERE contract_id = :cid"), {"cid": row.id})
    for old in session.scalars(select(ContractClauseRow).where(ContractClauseRow.contract_id == row.id)).all():
        session.delete(old)
    session.flush()
    for c in clauses:
        clause_row = ContractClauseRow(
            id=new_id(),
            contract_id=row.id,
            clause_number=c.clause_number,
            heading=c.heading or None,
            parent_path=c.parent_path,
            text=c.text,
            context_header=c.context_header,
            token_count=estimate_tokens(c.text),
            ordinal=c.ordinal,
        )
        session.add(clause_row)
        session.flush()
        session.execute(
            sql(f"INSERT INTO {_FTS_TABLE} (clause_id, contract_id, heading, context_header, text) VALUES (:id, :cid, :h, :ctx, :t)"),
            {"id": clause_row.id, "cid": row.id, "h": c.heading, "ctx": c.context_header, "t": c.text},
        )
    return IngestResult(contract=row, clauses=len(clauses), est_tokens=est, replaced=replaced)


def ingest_seed_samples(session: Session, *, operator_id: str | None = None) -> list[IngestResult]:
    """Chunk the two synthetic samples. Their ``contracts`` rows may already exist (noc-seed-v2)."""
    out: list[IngestResult] = []
    for path in sorted(SEED_CONTRACTS_DIR.glob("*_sample.md")):
        out.append(ingest_contract(session, path=path, operator_id=operator_id))
    return out


def ensure_fts(session: Session) -> bool:
    """Create the FTS5 index if absent. Returns False (and logs) when FTS5 is unavailable.

    ``IF NOT EXISTS`` makes it safe to call on every ingest and search; it is a catalogue
    check after the first time. Kept out of ``migrate_additive`` because that walks
    ``Base.metadata`` and a virtual table cannot be declared there (see ``models_contracts``).
    """
    try:
        session.execute(sql(_FTS_DDL))
        return True
    except Exception:  # noqa: BLE001 — a build without FTS5 must degrade, not crash the app
        log.warning("contract_clauses_fts could not be created; FTS5 unavailable", exc_info=True)
        return False


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if text:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            pass
    return utcnow().date()


# --------------------------------------------------------------------------- allow-set


def allowed_contracts_for(
    session: Session,
    *,
    role: str,
    incident_id: str | None,
    vendor_id: str | None,
) -> frozenset[str]:
    """Server-side only: role ∩ ``contract.allowed_roles``, narrowed to the incident's vendor.

    Starts from ``_owned(ContractRow)`` (operator clause first, always). A contract is in the
    set when the caller's role is listed in its ``allowed_roles_json`` — ``admin`` included
    only when Legal listed it; there is no implicit bypass, matching ``api/deps.py``. With an
    ``incident_id`` the set is narrowed to contracts whose counterparty is that incident's
    vendor (``incidents.vendor_id``, else resolved from ``msp_name`` through
    ``services.vendors.resolve_vendor``); an incident this operator does not own, or one that
    does not exist, yields the **empty** set rather than the un-narrowed one. An incident with
    no resolvable vendor (NOC queue, field engineer) is not narrowed — there is no vendor to
    narrow to — which is a wider answer, not a leak, because the role filter still applies.

    Vendor identity is matched on the vendor row **id or code**: ``noc-seed-v2`` seeds
    ``vendor-sfc-egypro`` while ``seed_vendors_from_contacts`` mints a uuid5 for the same
    EGYPRO row, and whichever landed first owns the natural key.
    """
    wanted_role = (role or "").strip().lower()
    if not wanted_role:
        return frozenset()
    try:
        rows = session.scalars(_owned(ContractRow)).all()
        vendor_keys = _vendor_keys(session, vendor_id) if vendor_id else set()
        if incident_id:
            inc = session.scalar(_owned(IncidentRow).where(IncidentRow.id == incident_id))
            if inc is None:
                return frozenset()
            inc_keys = _incident_vendor_keys(session, inc)
            if inc_keys:
                vendor_keys = (vendor_keys & inc_keys) if vendor_keys else inc_keys
                if not vendor_keys:
                    return frozenset()
        out: set[str] = set()
        for row in rows:
            if wanted_role not in row.allowed_roles:
                continue
            if vendor_keys and not (_vendor_keys(session, row.counterparty_vendor_id) & vendor_keys):
                continue
            out.add(row.id)
        return frozenset(out)
    except Exception:  # noqa: BLE001 — an unreadable allow-set is an EMPTY allow-set (fail-safe)
        log.warning("allowed_contracts_for degraded to empty", exc_info=True)
        return frozenset()


def _vendor_keys(session: Session, vendor_ref: str | None) -> set[str]:
    """``{id, code}`` for a vendor named by id or code; ``{ref}`` when no row is found."""
    ref = (vendor_ref or "").strip()
    if not ref:
        return set()
    keys = {ref}
    row = session.get(VendorRow, ref)
    if row is None:
        row = session.scalar(
            select(VendorRow)
            .where(VendorRow.operator_id == _settings().operator.operator_id, VendorRow.code == ref.upper())
            .order_by(VendorRow.active_from.desc())
        )
    if row is not None:
        keys.update({row.id, row.code})
    return keys


def _incident_vendor_keys(session: Session, inc: IncidentRow) -> set[str]:
    if inc.vendor_id:
        return _vendor_keys(session, inc.vendor_id)
    row = resolve_vendor(session, operator_id=inc.operator_id, msp_name=incident_vendor_name(inc), as_of=incident_as_of(inc))
    return {row.id, row.code} if row is not None else set()


def list_contracts_for(session: Session, *, role: str) -> list[ContractRow]:
    """The contracts this role may see, operator-scoped, newest effective date first."""
    allowed = allowed_contracts_for(session, role=role, incident_id=None, vendor_id=None)
    if not allowed:
        return []
    rows = session.scalars(_owned(ContractRow).where(ContractRow.id.in_(sorted(allowed)))).all()
    return sorted(rows, key=lambda r: (r.effective_date or date.min, r.title), reverse=True)


# --------------------------------------------------------------------------- retrieval

#: Words that carry no signal for a clause lookup and would otherwise match every row.
#: FTS5 has no stop-list of its own. Deliberately small: "not", "no", "only" are kept because
#: a negative clause ("shall not", "is not valid") is often exactly what is asked for.
_STOPWORDS = frozenset(
    """a an the and or of to in on at for by with from as is are was were be been being it its this that these
    those there their they them we our you your he she his her do does did done has have had having will would
    shall should can could may might must if then than so such what which who whom whose when where why how
    into onto over under about after before during while per each any all both either neither also very""".split()
)
_TERM_RE = re.compile(r"[0-9a-zÀ-ɏ]+")


def fts_query(question: str) -> str:
    """A safe FTS5 MATCH expression from free text: quoted terms joined with OR.

    Quoting every term neutralises FTS5 query syntax typed by a user (``"``, ``AND``, ``NOT``,
    ``heading:``, ``*``), which would otherwise raise a syntax error out of the search route.
    OR, not AND: a question is not a conjunction of required words, and bm25 already ranks a
    clause that matches more of them higher. Terms are lower-cased and de-duplicated in
    order; single letters and stop-words are dropped. Returns ``""`` when nothing usable
    remains, which the caller treats as "no hits".
    """
    seen: list[str] = []
    for term in _TERM_RE.findall((question or "").lower()):
        if len(term) < 2 or term in _STOPWORDS or term in seen:
            continue
        seen.append(term)
    return " OR ".join(f'"{t}"' for t in seen)


@dataclass(frozen=True)
class ClauseHit:
    """One retrieved clause with its contract's identity and its bm25 score (lower = better)."""

    clause_id: str
    contract_id: str
    contract_title: str
    effective_date: date | None
    clause_number: str
    heading: str | None
    parent_path: str
    text: str
    context_header: str
    score: float
    rank: int
    third_party_processing_permitted: bool
    ordinal: int


def retrieve_clauses(
    session: Session,
    query: str,
    *,
    allowed_contract_ids: frozenset[str],
    k: int = DEFAULT_K,
) -> list[ClauseHit]:
    """BM25 top-``k`` clauses for ``query``, **only** from ``allowed_contract_ids``.

    ``allowed_contract_ids`` is required and must be non-empty — ``ValueError`` otherwise
    (§7.8.3, §7.8.5: never a silent full scan). The SQL is
    ``... MATCH ? AND contract_id IN (...) ORDER BY bm25(...) LIMIT ?`` with the operator
    clause joined in from ``_owned(ContractRow)``, and every row is re-checked against the
    allow-set in Python before it is returned. A caller holding contract A's id cannot get
    contract B's clause out of this function; ``tests/unit/test_contract_retrieval.py`` proves
    it with two MSPs in one database.

    Degrades to ``[]`` on an FTS error (missing index, unavailable FTS5) — the fail-safe
    direction, and logged.
    """
    if not isinstance(allowed_contract_ids, (frozenset, set)) or not allowed_contract_ids:
        raise ValueError("allow-set must be non-empty")
    allowed = frozenset(str(c) for c in allowed_contract_ids)
    limit = max(1, min(int(k or DEFAULT_K), MAX_K))
    match = fts_query(query)
    if not match:
        return []
    try:
        ensure_fts(session)
        ids = sorted(allowed)
        placeholders = ", ".join(f":c{i}" for i in range(len(ids)))
        params: dict[str, Any] = {f"c{i}": cid for i, cid in enumerate(ids)}
        params.update({"q": match, "k": limit})
        ranked = session.execute(
            sql(
                f"SELECT clause_id, contract_id, "
                f"bm25({_FTS_TABLE}, 0.0, 0.0, {_W_HEADING}, {_W_CONTEXT}, {_W_TEXT}) AS score "
                f"FROM {_FTS_TABLE} WHERE {_FTS_TABLE} MATCH :q AND contract_id IN ({placeholders}) "
                f"ORDER BY score LIMIT :k"
            ),
            params,
        ).all()
    except Exception:  # noqa: BLE001 — a broken index is an empty answer, never a 500
        log.warning("retrieve_clauses degraded to empty", exc_info=True)
        return []
    if not ranked:
        return []
    scores = {str(r[0]): float(r[2]) for r in ranked}
    order = [str(r[0]) for r in ranked]
    rows = session.execute(
        _owned(ContractRow)
        .add_columns(ContractClauseRow)
        .join(ContractClauseRow, ContractClauseRow.contract_id == ContractRow.id)
        .where(ContractClauseRow.id.in_(order))
    ).all()
    by_id = {clause.id: (contract, clause) for contract, clause in rows}
    hits: list[ClauseHit] = []
    for clause_id in order:
        found = by_id.get(clause_id)
        if found is None:
            continue
        contract, clause = found
        if contract.id not in allowed or clause.contract_id not in allowed:
            # Cannot happen given the IN clause; kept because the cost is one set lookup and
            # the failure it guards against is the one this lane exists to prevent.
            continue
        hits.append(_hit(contract, clause, scores[clause_id], len(hits) + 1))
    return hits


def _hit(contract: ContractRow, clause: ContractClauseRow, score: float, rank: int) -> ClauseHit:
    return ClauseHit(
        clause_id=clause.id,
        contract_id=contract.id,
        contract_title=contract.title,
        effective_date=contract.effective_date,
        clause_number=clause.clause_number,
        heading=clause.heading,
        parent_path=clause.parent_path,
        text=clause.text,
        context_header=clause.context_header,
        score=round(float(score), 6),
        rank=rank,
        third_party_processing_permitted=bool(contract.third_party_processing_permitted),
        ordinal=clause.ordinal,
    )


def _all_clauses(session: Session, contract_ids: Iterable[str]) -> list[ClauseHit]:
    """Every clause of the given (already allowed) contracts, in document order."""
    ids = sorted(set(contract_ids))
    if not ids:
        return []
    rows = session.execute(
        _owned(ContractRow)
        .add_columns(ContractClauseRow)
        .join(ContractClauseRow, ContractClauseRow.contract_id == ContractRow.id)
        .where(ContractRow.id.in_(ids))
        .order_by(ContractRow.title, ContractClauseRow.ordinal)
    ).all()
    return [_hit(contract, clause, 0.0, i + 1) for i, (contract, clause) in enumerate(rows)]


# --------------------------------------------------------------------------- FAQ

_SUFFIXES = ("ing", "ed", "es", "s")


def _faq_tokens(text: str) -> set[str]:
    """Lower-cased content words with a crude suffix strip — enough for "disputes a line" to
    meet "dispute any line" without a stemmer dependency."""
    out: set[str] = set()
    for term in _TERM_RE.findall((text or "").lower()):
        if len(term) < 2 or term in _STOPWORDS:
            continue
        for suf in _SUFFIXES:
            if len(term) > len(suf) + 3 and term.endswith(suf):
                term = term[: -len(suf)]
                break
        out.add(term)
    return out


def match_faq(
    session: Session,
    question: str,
    *,
    allowed_contract_ids: frozenset[str],
    threshold: float = FAQ_MATCH_THRESHOLD,
) -> tuple[ContractFaqRow, float] | None:
    """The best active FAQ row the asker may see, if it covers the question well enough.

    Scored in Python rather than through a second FTS index: the table is tens of rows curated
    by hand, and the allow-set rule below has to run on every candidate anyway. Score is the
    fraction of the FAQ question's content tokens present in the asked question; the reverse
    fraction must also clear half the threshold so a two-word FAQ cannot match everything.

    Allow-set rule: a row is offered only when **every** contract it quotes is in the asker's
    allow-set (or it quotes none). Intersection would be the leak — an FAQ answer quoting two
    contracts discloses both.
    """
    try:
        rows = session.scalars(_owned(ContractFaqRow).where(ContractFaqRow.active == 1)).all()
    except Exception:  # noqa: BLE001
        log.warning("match_faq degraded to none", exc_info=True)
        return None
    asked = _faq_tokens(question)
    if not asked:
        return None
    best: tuple[ContractFaqRow, float] | None = None
    for row in rows:
        quoted = set(row.contract_ids)
        if quoted and not quoted <= allowed_contract_ids:
            continue
        faq_terms = _faq_tokens(row.question)
        if not faq_terms:
            continue
        overlap = len(asked & faq_terms)
        coverage = overlap / len(faq_terms)
        reverse = overlap / len(asked)
        if coverage >= threshold and reverse >= threshold / 2 and (best is None or coverage > best[1]):
            best = (row, round(coverage, 4))
    return best


def create_faq(
    session: Session,
    *,
    question: str,
    approved_answer: str,
    approved_by: str,
    contract_ids: Sequence[str],
    clause_refs: Sequence[dict[str, Any]],
    active: bool = True,
) -> ContractFaqRow:
    """One Legal-approved FAQ row. Contract ids must be this operator's; unknown ids are refused.

    ``approved_by`` is a person in Legal — this is the only path to ``official=True``, so an
    empty approver is refused rather than defaulted. Flushes; the caller commits.
    """
    q = " ".join((question or "").split())
    a = " ".join((approved_answer or "").split())
    who = (approved_by or "").strip()
    if not q or not a or not who:
        raise ValueError("question, approved_answer and approved_by are all required")
    wanted = sorted({str(c).strip() for c in contract_ids if str(c).strip()})
    if wanted:
        known = {r.id for r in session.scalars(_owned(ContractRow).where(ContractRow.id.in_(wanted))).all()}
        unknown = [c for c in wanted if c not in known]
        if unknown:
            raise ValueError(f"unknown contract id(s): {', '.join(unknown)}")
    row = ContractFaqRow(
        id=new_id(),
        operator_id=_settings().operator.operator_id,
        contract_ids_json=json.dumps(wanted),
        question=q,
        approved_answer=a,
        clause_refs_json=json.dumps([dict(c) for c in clause_refs or []]),
        approved_by=who,
        approved_at=utcnow(),
        active=1 if active else 0,
    )
    session.add(row)
    session.flush()
    return row


# --------------------------------------------------------------------------- redaction

_SIGNATURE_LINE = re.compile(r"^\s*(signed\s+by|signature|signatory|for and on behalf of)\b.*$", re.IGNORECASE | re.MULTILINE)


def redact_clause_text(text: str) -> str:
    """What a clause looks like before it leaves the building.

    E-mails and Kenyan MSISDNs go through the same ``scrub_contacts`` the incident path uses;
    signature lines ("Signed by: ...") are removed whole. There is no NER, so a signatory's
    name inside a sentence is not recognised — the limit ``llm/redaction.py`` records — which
    is why ``third_party_processing_permitted`` is a per-contract decision by Legal and not a
    default. The redacted text is what the model sees AND what its ``cited_text`` is validated
    against, so the quote the asker reads is the redacted one.
    """
    out = _SIGNATURE_LINE.sub("", text or "")
    return (scrub_contacts(out) or "").strip()


# --------------------------------------------------------------------------- answering

_UNSET: Any = object()


@dataclass
class ContractAnswer:
    """What ``POST /contracts/ask`` returns and what ``contract_queries`` records (§7.8.2)."""

    source: str
    answer: str
    citations: list[dict[str, Any]]
    validated: bool
    official: bool
    escalated_to_legal: bool
    nearest_clauses: list[dict[str, Any]]
    disclosure: str
    allowed_contract_ids: list[str]
    query_id: str | None = None
    model: str | None = None
    llm_call_id: str | None = None
    transfer_record_id: str | None = None
    fallback_reason: str | None = None
    faq_id: str | None = None
    corpus: dict[str, Any] = field(default_factory=dict)


def answer_question(
    session: Session,
    *,
    question: str,
    role: str,
    actor: str,
    incident_id: str | None = None,
    vendor_id: str | None = None,
    k: int = DEFAULT_K,
    llm: Any = _UNSET,
) -> ContractAnswer:
    """The §7.8.3 answer path, in this order and with no shortcut past any step:

    1. **Allow-set** — derived here from ``role`` / ``incident_id`` / ``vendor_id``; never from
       the caller's body. Empty → a recorded refusal (``REFUSAL_ACCESS_TEXT``) with no
       retrieval at all. (``retrieve_clauses`` would raise; this is the one place that is
       allowed to answer the empty set, and it answers it with a refusal.)
    2. **FAQ first** — an active Legal-curated row covering the question is returned as
       ``source=faq, official=True`` before any generation.
    3. **Retrieve** — BM25 top-``k`` over the allow-set.
    4. **Deterministic** when no model may be used (``LLM_ENABLED=false``, provider not
       Anthropic, spend gate, no permitted contract, transfer paperwork missing): the top
       clauses verbatim, ``validated=True`` because nothing was generated.
    5. **Cited model answer** otherwise: transfer record → redaction → ``cited_answer`` →
       ``validate_citations``. Valid → the fixed template from the verbatim quotes.
       Invalid or nothing → **refusal** with the three nearest clauses, ``escalated_to_legal``.

    ``llm`` is the raw Anthropic client to use; leave it unset to resolve
    ``llm.client.get_llm()`` at call time (``None`` when the layer is off), or pass ``None``
    to force the deterministic path, or a fake in tests. Every outcome writes a
    ``contract_queries`` row and an ``audit_events`` row; the caller commits.
    """
    q = " ".join((question or "").split())
    role_value = (role or "").strip().lower()
    who = (actor or "").strip() or role_value or "unknown"
    allowed = allowed_contracts_for(session, role=role_value, incident_id=incident_id, vendor_id=vendor_id)
    corpus = corpus_status(session, allowed)

    if not q:
        result = _refusal([], allowed, reason="empty question", text_override=REFUSAL_ACCESS_TEXT)
    elif not allowed:
        result = _refusal([], allowed, reason="empty allow-set", text_override=f"{REFUSAL_ACCESS_TEXT} {REFUSAL_TEXT}")
    else:
        faq = match_faq(session, q, allowed_contract_ids=allowed)
        if faq is not None:
            result = _faq_answer(faq[0], allowed)
        else:
            hits = retrieve_clauses(session, q, allowed_contract_ids=allowed, k=k)
            client = llm_client.get_llm() if llm is _UNSET else llm
            result = _generate(session, q, hits, allowed, client=client, role=role_value, actor=who, incident_id=incident_id, corpus=corpus)
    result.corpus = corpus
    _record(session, result, question=q, role=role_value, actor=who, incident_id=incident_id)
    return result


def corpus_status(session: Session, allowed: frozenset[str]) -> dict[str, Any]:
    """Token estimate of the asker's allowed corpus and whether it fits the prompt ceiling."""
    if not allowed:
        return {"contracts": 0, "est_tokens": 0, "ceiling_tokens": PROMPT_CORPUS_TOKEN_CEILING, "fits_in_prompt": True}
    try:
        rows = session.scalars(_owned(ContractRow).where(ContractRow.id.in_(sorted(allowed)))).all()
    except Exception:  # noqa: BLE001
        rows = []
    tokens = sum(int(r.token_count or 0) for r in rows)
    return {
        "contracts": len(rows),
        "est_tokens": tokens,
        "ceiling_tokens": PROMPT_CORPUS_TOKEN_CEILING,
        "fits_in_prompt": tokens <= PROMPT_CORPUS_TOKEN_CEILING,
    }


def _faq_answer(row: ContractFaqRow, allowed: frozenset[str]) -> ContractAnswer:
    when = row.approved_at.date().isoformat() if isinstance(row.approved_at, datetime) else str(row.approved_at or "")
    return ContractAnswer(
        source=SOURCE_FAQ,
        answer=row.approved_answer,
        citations=[
            {"contract_id": None, "contract_ref": c.get("contract_ref"), "clause_number": str(c.get("clause") or c.get("clause_number") or ""), "cited_text": None}
            for c in row.clause_refs
        ],
        validated=True,
        official=True,
        escalated_to_legal=False,
        nearest_clauses=[],
        disclosure=f"{FAQ_DISCLOSURE_PREFIX} ({row.approved_by}, {when}).",
        allowed_contract_ids=sorted(allowed),
        faq_id=row.id,
    )


def _refusal(hits: Sequence[ClauseHit], allowed: frozenset[str], *, reason: str, text_override: str | None = None) -> ContractAnswer:
    nearest = list(hits[:REFUSAL_NEAREST])
    lines = [text_override or f"{REFUSAL_TEXT} {REFUSAL_ACCESS_TEXT}"]
    if nearest:
        lines.append("Nearest clauses:")
        lines.extend(f"- §{h.clause_number} of {h.contract_title}: {_snippet(h.text)}" for h in nearest)
    lines.append(DISCLOSURE)
    return ContractAnswer(
        source=SOURCE_REFUSED,
        answer="\n".join(lines),
        citations=[],
        validated=False,
        official=False,
        escalated_to_legal=True,
        nearest_clauses=[clause_hit_out(h) for h in nearest],
        disclosure=DISCLOSURE,
        allowed_contract_ids=sorted(allowed),
        fallback_reason=reason,
    )


def _deterministic(hits: Sequence[ClauseHit], allowed: frozenset[str], *, reason: str) -> ContractAnswer:
    """§7.8.3 with ``LLM_ENABLED=false`` (or any reason a model may not be used): the top
    clauses verbatim. ``validated=True`` because there is no generated text to validate."""
    shown = list(hits[:DETERMINISTIC_LIST_SIZE])
    if not shown:
        return _refusal([], allowed, reason=f"no lexical match ({reason})")
    lines = [DETERMINISTIC_NOTE]
    for h in shown:
        eff = h.effective_date.isoformat() if h.effective_date else "n/a"
        lines.append(f"Clause {h.clause_number} of {h.contract_title} (effective {eff}): {h.text}")
    lines.append(DISCLOSURE)
    return ContractAnswer(
        source=SOURCE_DETERMINISTIC,
        answer="\n\n".join(lines),
        citations=[_citation_out(h, h.text) for h in shown],
        validated=True,
        official=False,
        escalated_to_legal=False,
        nearest_clauses=[clause_hit_out(h) for h in hits[:REFUSAL_NEAREST]],
        disclosure=DISCLOSURE,
        allowed_contract_ids=sorted(allowed),
        fallback_reason=reason,
    )


def _generate(
    session: Session,
    question: str,
    hits: list[ClauseHit],
    allowed: frozenset[str],
    *,
    client: Any,
    role: str,
    actor: str,
    incident_id: str | None,
    corpus: dict[str, Any],
) -> ContractAnswer:
    """Steps 4–5 of :func:`answer_question`."""
    if client is None:
        # ``get_llm()`` is None for LLM_ENABLED=false, an open spend circuit, a refused
        # credential, a missing SDK AND for LLM_PROVIDER=openai_compat — §7.8.3: a provider
        # without citations gets the clause list, because an uncited contract answer is not
        # traceable to the document that produced it.
        return _deterministic(hits, allowed, reason=llm_client.llm_unavailable_reason() or "provider_without_citations")
    gate = llm_client.spend_gate(session, operator_id=_settings().operator.operator_id)
    if gate:
        return _deterministic(hits, allowed, reason=gate)

    # Only clauses whose contract Legal has cleared for third-party processing may leave.
    permitted_ids = {
        r.id
        for r in session.scalars(
            _owned(ContractRow).where(ContractRow.id.in_(sorted(allowed)), ContractRow.third_party_processing_permitted == 1)
        ).all()
    }
    if not permitted_ids:
        return _deterministic(hits, allowed, reason="third_party_processing_not_permitted")

    # §7.8 measured-corpus rule: under the ceiling the model sees every permitted clause
    # (prompt-stuffing); above it, the BM25 top-k. Either way the citations map to clauses.
    if corpus.get("fits_in_prompt", True):
        candidates = _all_clauses(session, permitted_ids)
    else:
        candidates = [h for h in hits if h.contract_id in permitted_ids]
    if not candidates:
        return _deterministic(hits, allowed, reason="no_permitted_clauses")
    docs, index = _documents(candidates)

    try:
        transfer = record_transfer(
            session,
            recipient=TRANSFER_RECIPIENT,
            recipient_country=TRANSFER_COUNTRY,
            justification=TRANSFER_JUSTIFICATION,
            data_description=TRANSFER_DATA_DESCRIPTION,
            actor=actor,
            actor_role=role,
            incident_id=incident_id,
            residency="abroad",
        )
    except TransferPaperworkMissing as exc:
        log.warning("contracts: cited answer refused by the transfer paperwork gate: %s", exc)
        return _deterministic(hits, allowed, reason="transfer_paperwork_missing")

    answer, rec = cited_answer(client, question=question, docs=docs, model=CITED_MODEL, timeout=llm_client.timeout_s())
    valid = validate_citations(answer, docs)
    call = record_llm_call(
        session,
        operator_id=_settings().operator.operator_id,
        agent=AGENT_NAME,
        purpose=LLM_PURPOSE,
        provider=PROVIDER_ANTHROPIC,
        rec=rec,
        audit_id=transfer.id,
        incident_id=incident_id,
        validated=valid,
    )
    session.flush()
    if not valid or answer is None:
        result = _refusal(hits, allowed, reason=rec.error or "citations_not_validated")
    else:
        result = _template_answer(answer, index, hits, allowed)
    result.model = rec.model_used or CITED_MODEL
    result.llm_call_id = call.id
    result.transfer_record_id = transfer.id
    return result


def _documents(candidates: Sequence[ClauseHit]) -> tuple[list[CitedDocument], dict[tuple[str, int], ClauseHit]]:
    """One :class:`CitedDocument` per contract, blocks in document order, text redacted.

    Returns the docs and an index ``(document_id, block_index) -> ClauseHit`` so a citation
    maps straight back to the clause it quotes.
    """
    by_contract: dict[str, list[ClauseHit]] = {}
    for h in candidates:
        by_contract.setdefault(h.contract_id, []).append(h)
    docs: list[CitedDocument] = []
    index: dict[tuple[str, int], ClauseHit] = {}
    for contract_id, hits in by_contract.items():
        ordered = sorted(hits, key=lambda h: h.ordinal)
        blocks = [redact_clause_text(f"{h.clause_number} {h.text}") for h in ordered]
        docs.append(CitedDocument(id=contract_id, title=ordered[0].contract_title, blocks=blocks, block_refs=[h.clause_number for h in ordered]))
        for i, h in enumerate(ordered):
            index[(contract_id, i)] = h
    return docs, index


def _template_answer(answer: Any, index: dict[tuple[str, int], ClauseHit], hits: Sequence[ClauseHit], allowed: frozenset[str]) -> ContractAnswer:
    """The fixed advisory template (§7.8.3) rendered from validated verbatim quotes only."""
    lines: list[str] = []
    citations: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for cit in answer.citations:
        hit = index.get((cit.document_id, cit.block_index))
        if hit is None:
            continue
        key = (cit.document_id, cit.block_index, cit.cited_text.strip())
        if key in seen:
            continue
        seen.add(key)
        eff = hit.effective_date.isoformat() if hit.effective_date else "n/a"
        lines.append(f'Per clause {hit.clause_number} of {hit.contract_title} (effective {eff}): "{cit.cited_text.strip()}".')
        citations.append(_citation_out(hit, cit.cited_text.strip()))
    if not lines:  # validated, yet nothing mapped — treat as a refusal rather than answer blind
        return _refusal(hits, allowed, reason="citations_unmapped")
    lines.append(DISCLOSURE)
    return ContractAnswer(
        source=SOURCE_LLM,
        answer="\n".join(lines),
        citations=citations,
        validated=True,
        official=False,
        escalated_to_legal=False,
        nearest_clauses=[clause_hit_out(h) for h in hits[:REFUSAL_NEAREST]],
        disclosure=DISCLOSURE,
        allowed_contract_ids=sorted(allowed),
    )


def _record(session: Session, result: ContractAnswer, *, question: str, role: str, actor: str, incident_id: str | None) -> None:
    """Every answer → ``contract_queries`` + an ``audit_events`` row. Never raises."""
    op_id = _settings().operator.operator_id
    try:
        row = ContractQueryRow(
            id=new_id(),
            operator_id=op_id,
            asked_by=actor,
            role=role,
            incident_id=incident_id,
            question=question,
            allowed_contract_ids_json=json.dumps(result.allowed_contract_ids),
            source=result.source,
            answer=result.answer,
            citations_json=json.dumps(result.citations, default=str),
            validated=1 if result.validated else 0,
            escalated_to_legal=1 if result.escalated_to_legal else 0,
            model=result.model,
            llm_call_id=result.llm_call_id,
            transfer_record_id=result.transfer_record_id,
            created_at=utcnow(),
        )
        session.add(row)
        # No free text in the audit payload: the question may carry what an asker typed, and
        # the answer may quote a confidential clause. Both live in contract_queries, which is
        # legal-only; the audit row carries the decision facts.
        session.add(
            AuditRow(
                id=new_id(),
                ts=utcnow(),
                operator_id=op_id,
                actor=actor,
                action=AUDIT_ACTION,
                entity_type="incident" if incident_id else "contract_query",
                entity_id=incident_id or row.id,
                rationale=f"contract question answered from {result.source}",
                payload_json=json.dumps(
                    {
                        "query_id": row.id,
                        "role": role,
                        "source": result.source,
                        "validated": result.validated,
                        "official": result.official,
                        "escalated_to_legal": result.escalated_to_legal,
                        "allowed_contract_ids": result.allowed_contract_ids,
                        "citations": len(result.citations),
                        "model": result.model,
                        "llm_call_id": result.llm_call_id,
                        "transfer_record_id": result.transfer_record_id,
                        "fallback_reason": result.fallback_reason,
                    }
                ),
            )
        )
        session.flush()
        result.query_id = row.id
    except Exception:  # noqa: BLE001 — a failed log line must not turn an answer into a 500
        log.warning("contract query could not be recorded", exc_info=True)


def _snippet(text: str, limit: int = 200) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------- serialisers


def contract_out(row: ContractRow, *, clauses: int | None = None) -> dict[str, Any]:
    return {
        "id": row.id,
        "title": row.title,
        "counterparty_vendor_id": row.counterparty_vendor_id,
        "effective_date": row.effective_date.isoformat() if row.effective_date else None,
        "version": row.version,
        "source_file": row.source_file,
        "sha256": row.sha256,
        "confidentiality_checked_by": row.confidentiality_checked_by,
        "confidentiality_checked_at": row.confidentiality_checked_at,
        "third_party_processing_permitted": bool(row.third_party_processing_permitted),
        "allowed_roles": row.allowed_roles,
        "token_count": row.token_count,
        "clauses": clauses,
        "ingested_at": row.ingested_at,
    }


def clause_hit_out(h: ClauseHit) -> dict[str, Any]:
    return {
        "clause_id": h.clause_id,
        "contract_id": h.contract_id,
        "contract_title": h.contract_title,
        "effective_date": h.effective_date.isoformat() if h.effective_date else None,
        "clause_number": h.clause_number,
        "heading": h.heading,
        "parent_path": h.parent_path,
        "context_header": h.context_header,
        "text": h.text,
        "score": h.score,
        "rank": h.rank,
    }


def _citation_out(h: ClauseHit, cited_text: str) -> dict[str, Any]:
    return {
        "contract_id": h.contract_id,
        "contract_title": h.contract_title,
        "effective_date": h.effective_date.isoformat() if h.effective_date else None,
        "clause_number": h.clause_number,
        "cited_text": cited_text,
    }


def answer_out(a: ContractAnswer) -> dict[str, Any]:
    return {
        "query_id": a.query_id,
        "source": a.source,
        "answer": a.answer,
        "citations": a.citations,
        "validated": a.validated,
        "official": a.official,
        "escalated_to_legal": a.escalated_to_legal,
        "nearest_clauses": a.nearest_clauses,
        "disclosure": a.disclosure,
        "allowed_contract_ids": a.allowed_contract_ids,
        "model": a.model,
        "fallback_reason": a.fallback_reason,
        "faq_id": a.faq_id,
        "corpus": a.corpus,
    }


def faq_out(row: ContractFaqRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "question": row.question,
        "approved_answer": row.approved_answer,
        "contract_ids": row.contract_ids,
        "clause_refs": row.clause_refs,
        "approved_by": row.approved_by,
        "approved_at": row.approved_at,
        "active": bool(row.active),
    }


def query_out(row: ContractQueryRow) -> dict[str, Any]:
    try:
        citations = json.loads(row.citations_json or "[]")
    except ValueError:
        citations = []
    try:
        allowed = json.loads(row.allowed_contract_ids_json or "[]")
    except ValueError:
        allowed = []
    return {
        "id": row.id,
        "asked_by": row.asked_by,
        "role": row.role,
        "incident_id": row.incident_id,
        "question": row.question,
        "allowed_contract_ids": allowed,
        "source": row.source,
        "answer": row.answer,
        "citations": citations,
        "validated": bool(row.validated),
        "escalated_to_legal": bool(row.escalated_to_legal),
        "model": row.model,
        "llm_call_id": row.llm_call_id,
        "transfer_record_id": row.transfer_record_id,
        "created_at": row.created_at,
    }


# --------------------------------------------------------------------------- in-house eval
# RAGAS is out (§7.8: it mandates langchain + openai). recall@k and MRR are a dozen lines.

ClauseRef = tuple[str, str]  # (contract_id, clause_number)


def recall_at_k(expected: Iterable[ClauseRef], ranked: Sequence[ClauseRef], k: int) -> float:
    """Fraction of ``expected`` found in the first ``k`` of ``ranked``; 1.0 when nothing is expected."""
    want = set(expected)
    if not want:
        return 1.0
    top = set(ranked[:k])
    return len(want & top) / len(want)


def reciprocal_rank(expected: Iterable[ClauseRef], ranked: Sequence[ClauseRef]) -> float:
    """1/rank of the first expected clause in ``ranked``; 0.0 when none appears."""
    want = set(expected)
    for i, ref in enumerate(ranked, start=1):
        if ref in want:
            return 1.0 / i
    return 0.0


@dataclass
class EvalItem:
    id: str
    question: str
    scope: list[str]
    expected: list[ClauseRef]
    forbidden: list[ClauseRef]
    retrieved: list[ClauseRef]
    recall_at: dict[int, float]
    reciprocal_rank: float
    must_refuse: bool
    leaked: list[ClauseRef]

    @property
    def scored(self) -> bool:
        """Only rows with expected clauses count toward recall/MRR — refusal rows measure
        something else (the golden file's own scoring note says so)."""
        return bool(self.expected) and not self.must_refuse


@dataclass
class EvalReport:
    items: list[EvalItem]
    k_values: tuple[int, ...]
    recall_at: dict[int, float]
    mrr: float
    leaks: int

    @property
    def scored_items(self) -> int:
        return sum(1 for i in self.items if i.scored)

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": len(self.items),
            "scored_items": self.scored_items,
            "recall_at": {str(k): round(v, 4) for k, v in self.recall_at.items()},
            "mrr": round(self.mrr, 4),
            "leaks": self.leaks,
            "misses": [
                {"id": i.id, "missing": [f"{c}:{n}" for (c, n) in i.expected if (c, n) not in set(i.retrieved[: max(self.k_values)])]}
                for i in self.items
                if i.scored and i.recall_at.get(max(self.k_values), 0.0) < 1.0
            ],
        }


def evaluate_retrieval(
    session: Session,
    golden: dict[str, Any],
    *,
    ref_to_id: dict[str, str],
    k_values: Sequence[int] = (5, 10, 20),
    question_keys: Sequence[str] = ("questions", "eval_supplement"),
) -> EvalReport:
    """Run every golden question through :func:`retrieve_clauses` with the allow-set the
    question's ``scope_contract_refs`` implies; compute recall@k (mean over scored rows) and
    MRR. ``leaks`` counts any ``must_not_cite`` clause that was retrieved — structurally zero,
    counted anyway.

    ``question_keys`` names the lists read from the golden file. ``questions`` is the frozen
    twelve-row set ``scripts/seed_v2.py`` validates; ``eval_supplement`` is the §7.8.7 extension
    to 30–50 items, kept under its own key so the twelve-row contract stays intact.
    """
    ks = tuple(sorted({int(k) for k in k_values}))
    top = max(ks)
    items: list[EvalItem] = []
    for key in question_keys:
        for q in golden.get(key) or []:
            expect = q.get("expect") or {}
            scope_refs = [str(r) for r in (q.get("scope_contract_refs") or [])]
            allowed = frozenset(ref_to_id[r] for r in scope_refs if r in ref_to_id)
            expected = [(ref_to_id.get(c["contract_ref"], c["contract_ref"]), str(c["clause"])) for c in expect.get("clauses") or []]
            forbidden = [(ref_to_id.get(c["contract_ref"], c["contract_ref"]), str(c["clause"])) for c in expect.get("must_not_cite") or []]
            retrieved: list[ClauseRef] = []
            if allowed:
                retrieved = [(h.contract_id, h.clause_number) for h in retrieve_clauses(session, q["question"], allowed_contract_ids=allowed, k=top)]
            items.append(
                EvalItem(
                    id=str(q.get("id")),
                    question=str(q.get("question") or ""),
                    scope=scope_refs,
                    expected=expected,
                    forbidden=forbidden,
                    retrieved=retrieved,
                    recall_at={k: recall_at_k(expected, retrieved, k) for k in ks},
                    reciprocal_rank=reciprocal_rank(expected, retrieved),
                    must_refuse=bool(expect.get("must_refuse")),
                    leaked=[ref for ref in forbidden if ref in set(retrieved)],
                )
            )
    scored = [i for i in items if i.scored]
    n = len(scored) or 1
    return EvalReport(
        items=items,
        k_values=ks,
        recall_at={k: sum(i.recall_at[k] for i in scored) / n for k in ks},
        mrr=sum(i.reciprocal_rank for i in scored) / n,
        leaks=sum(len(i.leaked) for i in items),
    )
