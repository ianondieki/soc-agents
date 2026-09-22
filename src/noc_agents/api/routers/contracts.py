"""Contract retrieval and cited-answer routes (spec §7.8.2, Phase 5 Lane 5B,
``CONTRACTS_ENABLED=false``).

Thin transport over ``services/contracts.py``: bodies, RBAC, status codes, serialisation.
The rules worth arguing about — the allow-set, the FAQ-first order, citation validation, the
refusal — live in the service where they are tested without a client.

**The flag is a 404, not a 403** (the PIR precedent): with ``CONTRACTS_ENABLED`` unset the
lane is invisible, because "the system behaves exactly as it does today" means a system with
no ``/contracts`` surface at all. The UI reads the 404 as "off".

**The allow-set never comes from the body.** ``POST /contracts/ask`` reads ``question`` and
``incident_id`` and nothing else that scopes; the role is the principal's and the vendor is
the incident's, both resolved server-side in ``allowed_contracts_for``. A body carrying
``allowed_contract_ids`` is accepted (pydantic ignores unknown keys) and ignored — pinned by
``tests/unit/test_contract_retrieval.py``.

**The production guard is registration-time.** §7.8.6 names contracts in the same breath
as complaints: contract text may be confidential and may carry signatories' personal data,
and "the routes cannot exist while ``AUTH_DISABLED=true`` in production (§7.0.5 guard)". Not
"answer 403" — *cannot exist*. So the routes below are declared inside an ``if``, the way
``api/routers/complaints.py`` and ``main.py``'s ledger download declare theirs, and with
``AUTH_DISABLED=true`` and ``NOC_ENV=production`` this router registers no routes at all.
Registration is the only control that can hold in that configuration: ``require_role`` never
rejects while ``AUTH_DISABLED=true``, so every gate on every route below is inert there, and
an inert gate in front of an MSP's confidential contract text is not a gate.

**Operator scoping** is in the service (every statement starts from ``api.deps._owned``); this
module adds no query of its own against a contract table.

Roles (§9.3's contracts row, via ``api/deps``): ingest, FAQ curation and the query log are
``legal``/``admin`` ("all"; §7.8.2 names only ``legal`` for the FAQ and the log, a spec
conflict resolved toward §9.3); reads take the "ask" cell -- noc_analyst, shift_supervisor,
duty_manager, planning, legal, admin -- and inside a read each contract's own
``allowed_roles`` still applies — the route-level gate says who may *ask*, the contract row
says who may *see*.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from noc_agents.api import auth
from noc_agents.api.auth import require_role
from noc_agents.api.deps import CONTRACT_ASKERS, CONTRACT_OWNERS, _actor, _owned
from noc_agents.db.models import get_session
from noc_agents.db.models_contracts import ContractClauseRow, ContractFaqRow, ContractQueryRow
from noc_agents.services import contracts as svc
from noc_agents.services.clock import z_utc

router = APIRouter(prefix="/api/v1", tags=["contracts"])

#: Who may ask. ``legal`` is not in ``READERS`` (it is an audit/curation role elsewhere) but
#: is the one role that must be able to read every contract it curates.
#: §9.3's "ask" cell (api/deps.CONTRACT_ASKERS). Until round 4 this was READERS + legal, which
#: let management and the MSP coordinator list contracts and search clauses.
CONTRACT_READERS: tuple[str, ...] = CONTRACT_ASKERS
#: §7.8.2: ingest is legal/admin.
CONTRACT_INGESTERS: tuple[str, ...] = ("legal", "admin")
#: §7.8.2: FAQ curation and the query log are legal.
CONTRACT_CURATORS: tuple[str, ...] = CONTRACT_OWNERS  # §9.3 "all" for admin too; §7.8.2 says legal

MAX_QUESTION_CHARS = 2000
MAX_QUERY_LOG = 500


def require_contracts_enabled() -> None:
    """404 the whole lane while ``CONTRACTS_ENABLED`` is off (the default)."""
    if not svc.contracts_enabled():
        raise HTTPException(404, "contracts assistant is not enabled on this deployment (CONTRACTS_ENABLED=false)")


_ENABLED = Depends(require_contracts_enabled)

#: §7.0.5 / §7.8.6. Evaluated once, at import, exactly like ``complaints.py`` and ``main.py``:
#: the routes are not registered at all in an unauthenticated production deployment.
PRODUCTION_GUARDED_ROUTES: tuple[str, ...] = (
    "GET /api/v1/contracts/status",
    "GET /api/v1/contracts",
    "POST /api/v1/contracts",
    "POST /api/v1/contracts/ingest-samples",
    "GET /api/v1/contracts/clauses/search",
    "POST /api/v1/contracts/ask",
    "GET /api/v1/contracts/faq",
    "POST /api/v1/contracts/faq",
    "GET /api/v1/contracts/queries",
)
_PRODUCTION_GUARD = auth.production_guard_active()


# ------------------------------------------------------------------------------- bodies


class AskIn(BaseModel):
    """``question`` and, optionally, the incident it concerns. Nothing here scopes the answer:
    ``allowed_contract_ids`` in a body is silently dropped by pydantic and never read."""

    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    incident_id: str | None = None
    asked_by: str | None = None  # honoured only with AUTH_DISABLED (see deps._actor)


class IngestIn(BaseModel):
    """A file under ``data/contracts/`` (or the seed samples folder) plus Legal's decisions.

    Front matter in the file fills any field left ``None``; the two samples carry all of them.
    ``confidentiality_checked_by`` must end up non-empty or ingest refuses (422).
    """

    path: str = Field(min_length=1, max_length=512)
    contract_id: str | None = None
    counterparty_vendor_id: str | None = None
    title: str | None = None
    effective_date: date | None = None
    version: str | None = None
    confidentiality_checked_by: str | None = None
    third_party_processing_permitted: bool | None = None
    allowed_roles: list[str] | None = None


class FaqIn(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    approved_answer: str = Field(min_length=1)
    approved_by: str | None = None
    contract_ids: list[str] = Field(default_factory=list)
    clause_refs: list[dict[str, Any]] = Field(default_factory=list)
    active: bool = True


# ------------------------------------------------------------------------------ helpers


def _z(row: dict[str, Any]) -> dict[str, Any]:
    """Explicit ``Z`` on outgoing timestamps (§7.0.6, defect #41) — same rule as the memory router."""
    return {k: (z_utc(v) if isinstance(v, datetime) else v) for k, v in row.items()}


def _clause_counts(session, contract_ids: list[str]) -> dict[str, int]:
    if not contract_ids:
        return {}
    rows = session.execute(
        select(ContractClauseRow.contract_id, func.count(ContractClauseRow.id))
        .where(ContractClauseRow.contract_id.in_(contract_ids))
        .group_by(ContractClauseRow.contract_id)
    ).all()
    return {cid: int(n) for cid, n in rows}


if _PRODUCTION_GUARD:
    auth.log_production_guard(PRODUCTION_GUARDED_ROUTES)
else:

    # ------------------------------------------------------------------------------- routes


    @router.get("/contracts/status", dependencies=[_ENABLED])
    def contracts_status(principal: auth.Principal = Depends(require_role(*CONTRACT_READERS))) -> dict[str, Any]:
        """What the page needs to explain itself: the flag, the corpus measurement, the LLM state.

        ``corpus`` is the §7.8 measurement for *this asker's* allowed contracts, with the
        prompt-stuffing verdict spelled out; ``llm`` says whether an ask will produce a cited
        model answer or the deterministic clause list, and why.
        """
        session = get_session()
        try:
            allowed = svc.allowed_contracts_for(session, role=principal.role, incident_id=None, vendor_id=None)
            corpus = svc.corpus_status(session, allowed)
            fts_ok = svc.ensure_fts(session)
            session.commit()
        finally:
            session.close()
        from noc_agents.llm import client as llm_client  # local: keep the router importable without the LLM layer warm

        reason = llm_client.llm_unavailable_reason()
        return {
            "enabled": True,
            "role": principal.role,
            "allowed_contract_ids": sorted(allowed),
            "corpus": corpus,
            "fts5_available": fts_ok,
            "llm": {
                "cited_answers": reason is None and llm_client.llm_provider() == llm_client.PROVIDER_ANTHROPIC,
                "provider": llm_client.llm_provider(),
                "unavailable_reason": reason,
                "model": svc.CITED_MODEL,
            },
            "disclosure": svc.DISCLOSURE,
        }


    @router.get("/contracts", dependencies=[_ENABLED])
    def list_contracts(principal: auth.Principal = Depends(require_role(*CONTRACT_READERS))) -> list[dict[str, Any]]:
        """The contracts this role may see (role ∩ ``allowed_roles``), operator-scoped."""
        session = get_session()
        try:
            rows = svc.list_contracts_for(session, role=principal.role)
            counts = _clause_counts(session, [r.id for r in rows])
            return [_z(svc.contract_out(r, clauses=counts.get(r.id, 0))) for r in rows]
        finally:
            session.close()


    @router.post("/contracts", dependencies=[_ENABLED], status_code=201)
    def ingest_contract(body: IngestIn, principal: auth.Principal = Depends(require_role(*CONTRACT_INGESTERS))) -> dict[str, Any]:
        """Chunk and index one Markdown/TXT contract (§7.8.2). 404 unknown path, 413 too large,
        415 PDF/binary, 422 missing Legal fields or no clauses."""
        session = get_session()
        try:
            try:
                path = svc.resolve_source_path(body.path)
                result = svc.ingest_contract(
                    session,
                    path=path,
                    contract_id=body.contract_id,
                    counterparty_vendor_id=body.counterparty_vendor_id,
                    title=body.title,
                    effective_date=body.effective_date,
                    version=body.version,
                    confidentiality_checked_by=body.confidentiality_checked_by,
                    third_party_processing_permitted=body.third_party_processing_permitted,
                    allowed_roles=body.allowed_roles,
                )
            except svc.ContractIngestError as exc:
                session.rollback()
                raise HTTPException(exc.status, str(exc)) from exc
            session.commit()
            out = _z(svc.contract_out(result.contract, clauses=result.clauses))
            out["replaced"] = result.replaced
            out["est_tokens"] = result.est_tokens
            return out
        finally:
            session.close()


    @router.post("/contracts/ingest-samples", dependencies=[_ENABLED], status_code=201)
    def ingest_samples(principal: auth.Principal = Depends(require_role(*CONTRACT_INGESTERS))) -> list[dict[str, Any]]:
        """Index the two SYNTHETIC sample contracts (demo). ``noc-seed-v2`` writes their
        ``contracts`` rows but not their clauses — clause chunking is this lane's job."""
        session = get_session()
        try:
            results = svc.ingest_seed_samples(session)
            session.commit()
            return [_z(svc.contract_out(r.contract, clauses=r.clauses)) for r in results]
        finally:
            session.close()


    @router.get("/contracts/clauses/search", dependencies=[_ENABLED])
    def search_clauses(
        q: str = Query(min_length=1, max_length=MAX_QUESTION_CHARS),
        k: int = Query(svc.DEFAULT_K, ge=1, le=svc.MAX_K),
        incident_id: str | None = None,
        principal: auth.Principal = Depends(require_role(*CONTRACT_READERS)),
    ) -> dict[str, Any]:
        """Deterministic FTS list, scoped (§7.8.2). No model, no generation.

        An asker whose role can see no contract gets an empty list with the reason stated — the
        service's ``ValueError`` for an empty allow-set is a guard for *callers*, not something
        to show a browser as a 500.
        """
        session = get_session()
        try:
            allowed = svc.allowed_contracts_for(session, role=principal.role, incident_id=incident_id, vendor_id=None)
            if not allowed:
                return {"query": q, "allowed_contract_ids": [], "hits": [], "reason": f"no contracts are accessible to role '{principal.role}' in this scope"}
            hits = svc.retrieve_clauses(session, q, allowed_contract_ids=allowed, k=k)
            session.commit()  # ensure_fts may have created the index
            return {"query": q, "allowed_contract_ids": sorted(allowed), "hits": [svc.clause_hit_out(h) for h in hits], "reason": None}
        finally:
            session.close()


    @router.post("/contracts/ask", dependencies=[_ENABLED])
    def ask(body: AskIn, principal: auth.Principal = Depends(require_role(*CONTRACT_READERS))) -> dict[str, Any]:
        """FAQ first, then a cited advisory answer or a refusal (§7.8.3). Always 200 with a
        ``source`` the client can switch on; never a 500 for "nothing found"."""
        session = get_session()
        try:
            result = svc.answer_question(
                session,
                question=body.question,
                role=principal.role,
                actor=_actor(principal, body.asked_by),
                incident_id=body.incident_id,
            )
            session.commit()
            return svc.answer_out(result)
        finally:
            session.close()


    @router.get("/contracts/faq", dependencies=[_ENABLED])
    def list_faq(principal: auth.Principal = Depends(require_role(*CONTRACT_CURATORS))) -> list[dict[str, Any]]:
        session = get_session()
        try:
            rows = session.scalars(_owned(ContractFaqRow).order_by(ContractFaqRow.approved_at.desc())).all()
            return [_z(svc.faq_out(r)) for r in rows]
        finally:
            session.close()


    @router.post("/contracts/faq", dependencies=[_ENABLED], status_code=201)
    def create_faq(body: FaqIn, principal: auth.Principal = Depends(require_role(*CONTRACT_CURATORS))) -> dict[str, Any]:
        """One Legal-approved answer. The approver is the principal when authenticated."""
        session = get_session()
        try:
            try:
                row = svc.create_faq(
                    session,
                    question=body.question,
                    approved_answer=body.approved_answer,
                    approved_by=_actor(principal, body.approved_by),
                    contract_ids=body.contract_ids,
                    clause_refs=body.clause_refs,
                    active=body.active,
                )
            except ValueError as exc:
                session.rollback()
                raise HTTPException(422, str(exc)) from exc
            session.commit()
            return _z(svc.faq_out(row))
        finally:
            session.close()


    @router.get("/contracts/queries", dependencies=[_ENABLED])
    def list_queries(
        limit: int = Query(100, ge=1, le=MAX_QUERY_LOG),
        source: str | None = None,
        principal: auth.Principal = Depends(require_role(*CONTRACT_CURATORS)),
    ) -> list[dict[str, Any]]:
        """The query log (§7.8.2): what was asked, what came back — for FAQ curation and the eval set."""
        session = get_session()
        try:
            stmt = _owned(ContractQueryRow)
            if source:
                stmt = stmt.where(ContractQueryRow.source == source.strip().lower())
            stmt = stmt.order_by(ContractQueryRow.created_at.desc()).limit(limit)
            return [_z(svc.query_out(r)) for r in session.scalars(stmt).all()]
        finally:
            session.close()
