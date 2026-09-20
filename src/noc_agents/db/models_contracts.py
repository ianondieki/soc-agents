"""Contracts, clauses, the Legal-curated FAQ and the query log (spec §7.8.1, Phase 5 Lane 5B,
``CONTRACTS_ENABLED=false``).

Four tables, declared against the shared ``Base`` from ``db/models.py`` so the generic
additive migration in ``db/migrate.py`` creates them with no hand-written DDL
(``db/models_all.py`` imports this module before ``migrate_additive`` walks
``Base.metadata``). ``SCHEMA_VERSION`` is not bumped: these are new tables, and the
additive path creates a missing table on every start regardless of the version number.

What is deliberately NOT here:

* ``contract_clauses_fts`` — an FTS5 *virtual* table. SQLAlchemy's ``create_all`` cannot
  express ``CREATE VIRTUAL TABLE ... USING fts5(...)``, and the migration walks
  ``Base.metadata`` only, so the index is created lazily by ``services/contracts.py``
  (``ensure_fts``) with ``IF NOT EXISTS`` the first time anything ingests or searches. It is
  a derived index: every row in it is rebuilt from ``contract_clauses`` at ingest, so losing
  it loses nothing.
* ``relationship_complaints`` and ``subject_persons`` — the complaint intake half of §7.8 is a
  separate lane and lives in ``db/models_complaints.py``.

Column names follow the spec DDL 1:1 **and** the raw ``INSERT`` that ``scripts/seed_v2.py``
already issues against ``contracts`` and ``contract_faq`` (it writes the intersection of its
row dict with the live columns, and stamps ``ingested_at``). Renaming a column here would
silently make the seeder skip it, so the spec's spelling is kept even where the codebase
would normally prefer another (``allowed_roles_json`` rather than a relationship table).

Operator scoping (§8): ``contracts``, ``contract_faq`` and ``contract_queries`` each carry
``operator_id`` and are read through ``api.deps._owned``. ``contract_clauses`` does not — a
clause is owned through ``contract_id -> contracts.operator_id`` and is only ever reached
*after* the contract has passed the operator clause AND the per-request allow-set
(``services/contracts.allowed_contracts_for``). A denormalised ``operator_id`` on the clause
would be a second copy of a derivable value; see the same argument for ``hitl_tasks`` in
``api/deps.py``.

**Confidentiality is a property of the contract row, not of the request.** Contracts are
mutually confidential across MSPs (§7.8): ``allowed_roles_json`` says which NOC roles may see
a contract at all, ``counterparty_vendor_id`` lets an incident narrow the set to its own
vendor, and ``third_party_processing_permitted`` is the only thing that lets a clause leave
the building for a hosted model. All three are decided at ingest by Legal, never by the
asker.
"""

from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column

from noc_agents.db.models import Base, new_id, utcnow

__all__ = ["ContractClauseRow", "ContractFaqRow", "ContractQueryRow", "ContractRow"]


class ContractRow(Base):
    """One ingested contract or SLA schedule (§7.8.1 ``contracts``).

    ``sha256`` is of the source file as ingested, so a re-ingest of an unchanged file is
    detectable and a changed file is a new ``version`` decision for Legal, not a silent
    overwrite. ``token_count`` is the corpus measurement §7.8 insists on *before* choosing
    retrieval over prompt-stuffing: it is an estimate (``services/contracts.estimate_tokens``,
    chars/4 — there is no tokenizer dependency), and the seeder's word count is overwritten
    by the estimate when the file is chunked.

    ``confidentiality_checked_by`` is a person in Legal, or (for the two synthetic samples)
    the seed file's ``confidentiality_basis`` line. Ingest refuses an empty value: a contract
    nobody has checked for a confidentiality clause cannot be indexed, however harmless it
    looks.
    """

    __tablename__ = "contracts"
    __table_args__ = (Index("ix_contracts_operator_vendor", "operator_id", "counterparty_vendor_id"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    operator_id: Mapped[str] = mapped_column(Text, index=True)
    counterparty_vendor_id: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    effective_date: Mapped[date] = mapped_column(Date)
    version: Mapped[str] = mapped_column(Text)
    source_file: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(Text)
    confidentiality_checked_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidentiality_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    third_party_processing_permitted: Mapped[int] = mapped_column(Integer, default=0, server_default=sql_text("0"))
    allowed_roles_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    token_count: Mapped[int] = mapped_column(Integer, default=0, server_default=sql_text("0"))
    # server_default as well as default: scripts/seed_v2.py stamps this column itself, but a
    # raw INSERT from anywhere else must still satisfy the spec's NOT NULL.
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, server_default=sql_text("CURRENT_TIMESTAMP"))

    @property
    def allowed_roles(self) -> list[str]:
        try:
            value = json.loads(self.allowed_roles_json or "[]")
        except ValueError:
            return []
        return [str(r).strip().lower() for r in value] if isinstance(value, list) else []

    @allowed_roles.setter
    def allowed_roles(self, value: list[str]) -> None:
        self.allowed_roles_json = json.dumps(sorted({str(r).strip().lower() for r in value if str(r).strip()}))


class ContractClauseRow(Base):
    """One numbered clause or sub-clause (§7.8.1 ``contract_clauses``) — the retrieval unit.

    ``ordinal`` is the clause's position in the document and is what a citation's
    ``block_index`` maps back to (``llm/cited.py``): the documents handed to the model are
    built one block per clause *in ordinal order*, so ``blocks[i]`` is always the clause with
    ``ordinal == i`` among the clauses sent. ``context_header`` is the one-line contextual
    prefix of §7.8 ("Contract X §12 Service Levels — restoration targets by priority"); it is
    indexed in FTS alongside the text because a clause about response times rarely contains
    the word "response" more than once, while its heading does.
    """

    __tablename__ = "contract_clauses"
    __table_args__ = (
        UniqueConstraint("contract_id", "clause_number", name="uq_contract_clauses_contract_clause"),
        Index("ix_contract_clauses_contract_ordinal", "contract_id", "ordinal"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    contract_id: Mapped[str] = mapped_column(Text, index=True)
    clause_number: Mapped[str] = mapped_column(Text)
    heading: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_path: Mapped[str] = mapped_column(Text, default="", server_default="")
    text: Mapped[str] = mapped_column(Text)
    context_header: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer, default=0, server_default=sql_text("0"))
    ordinal: Mapped[int] = mapped_column(Integer)


class ContractFaqRow(Base):
    """A Legal-approved question and answer (§7.8.1 ``contract_faq``).

    The ONLY source of an answer labelled ``official`` (§7.8: Stanford measured >17 %
    incorrect answers from commercial legal AI, so nothing a model produced is ever
    official). ``contract_ids_json`` scopes the row: it is offered only to an asker whose
    allow-set covers every contract the answer quotes — the safe direction, because an FAQ
    answer that quotes two contracts discloses both.
    """

    __tablename__ = "contract_faq"
    __table_args__ = (Index("ix_contract_faq_operator_active", "operator_id", "active"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    operator_id: Mapped[str] = mapped_column(Text, index=True)
    contract_ids_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    question: Mapped[str] = mapped_column(Text)
    approved_answer: Mapped[str] = mapped_column(Text)
    clause_refs_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    approved_by: Mapped[str] = mapped_column(Text)
    approved_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    active: Mapped[int] = mapped_column(Integer, default=1, server_default=sql_text("1"))

    @property
    def contract_ids(self) -> list[str]:
        try:
            value = json.loads(self.contract_ids_json or "[]")
        except ValueError:
            return []
        return [str(v) for v in value] if isinstance(value, list) else []

    @property
    def clause_refs(self) -> list[dict]:
        try:
            value = json.loads(self.clause_refs_json or "[]")
        except ValueError:
            return []
        return [dict(v) for v in value if isinstance(v, dict)] if isinstance(value, list) else []


class ContractQueryRow(Base):
    """Every question asked, and what came back (§7.8.1 ``contract_queries``).

    ``source`` vocabulary: ``faq | llm | deterministic | refused``. ``allowed_contract_ids_json``
    is the allow-set the server derived — recorded so a reviewer can check, per answer, that
    nothing outside it was cited. ``answer`` is the text the asker saw; for ``llm`` that is
    the fixed advisory template built from validated citations, never the model's free
    prose. ``escalated_to_legal`` is set on every refusal: a refusal is a hand-off, not a
    dead end.
    """

    __tablename__ = "contract_queries"
    __table_args__ = (Index("ix_contract_queries_operator_created", "operator_id", "created_at"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    operator_id: Mapped[str] = mapped_column(Text, index=True)
    asked_by: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    incident_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    question: Mapped[str] = mapped_column(Text)
    allowed_contract_ids_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    source: Mapped[str] = mapped_column(String(16))
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    citations_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    validated: Mapped[int] = mapped_column(Integer, default=0, server_default=sql_text("0"))
    escalated_to_legal: Mapped[int] = mapped_column(Integer, default=0, server_default=sql_text("0"))
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_call_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    transfer_record_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
