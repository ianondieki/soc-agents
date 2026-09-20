"""Contract clause retrieval — the tenant filter, chunking, the allow-set and the API surface
(spec §7.8, Phase 5 Lane 5B).

The one property everything here protects: **a caller cannot get another MSP's clause out of
retrieval**. Contracts are mutually confidential across vendors, and §7.8 records that an
omitted partition filter "returns everything". So the allow-set is a required argument, an
empty one raises, the SQL carries the IN clause *and* the operator clause, and the route
derives the set server-side and ignores anything a body says. Each of those is a test below,
with two MSPs (and a second operator) in one database so a leak has somewhere to leak to.

No network, no model: with ``LLM_ENABLED=false`` (pinned by conftest) the answer path is the
deterministic clause list, which is exactly the §7.8.3 "LLM off" behaviour under test here.
The cited path has its own file (``test_cited_answers.py``).
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from noc_agents.db.models import AuditRow, IncidentRow
from noc_agents.db.models_contracts import ContractClauseRow, ContractFaqRow, ContractQueryRow, ContractRow
from noc_agents.services import contracts as svc

EGYPRO = "contract-sample-egypro-msa"
TETRANET = "contract-sample-tetranet-sla"
EGYPRO_FILE = svc.SEED_CONTRACTS_DIR / "egypro_msa_sample.md"
TETRANET_FILE = svc.SEED_CONTRACTS_DIR / "tetranet_sla_sample.md"
#: Only the Tetranet sample answers this (clause 2.5, 72 hours) — golden q10's leak probe.
FUEL_QUESTION = "How many hours of generator fuel must be held on site?"


# --------------------------------------------------------------------------- fixtures


@pytest.fixture()
def session(tmp_path, monkeypatch):
    """A fresh database file with the schema built by ``init_db`` and both samples ingested."""
    import noc_agents.config as cfg
    import noc_agents.db.models as models

    db = tmp_path / "contracts.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
    monkeypatch.delenv("CONTRACTS_ENABLED", raising=False)
    cfg.clear_settings_cache()
    models._engine = None
    models.SessionLocal = None
    models.init_db(f"sqlite:///{db.as_posix()}", backup_dir=tmp_path / "backups")
    s = models.get_session()
    svc.ingest_seed_samples(s)
    s.commit()
    try:
        yield s
    finally:
        s.close()
        models._engine = None
        models.SessionLocal = None
        cfg.clear_settings_cache()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """The app on its own database with ``CONTRACTS_ENABLED=true``, built like ``test_memory_api``."""
    db = tmp_path / "contracts_api.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path / "ledgers"))
    monkeypatch.setenv("CONTRACTS_ENABLED", "true")

    import noc_agents.config as cfg
    import noc_agents.db.models as models
    import noc_agents.main as main
    from noc_agents.realtime.hub import hub

    cfg.clear_settings_cache()
    models._engine = None
    models.SessionLocal = None
    importlib.reload(main)
    hub._history.clear()
    with TestClient(main.app) as c:
        yield c
    hub._history.clear()
    models._engine = None
    models.SessionLocal = None
    cfg.clear_settings_cache()


def _as(client: TestClient, role: str) -> None:
    """Switch the demo role switcher (AUTH_DISABLED=true is the suite default)."""
    r = client.post("/api/v1/session", json={"display_name": f"{role} user", "role": role})
    assert r.status_code == 200, r.text


def _ingest_samples_via_app() -> None:
    from noc_agents.db.models import get_session

    s = get_session()
    try:
        svc.ingest_seed_samples(s)
        s.commit()
    finally:
        s.close()


# --------------------------------------------------------------------------- flag


def test_contracts_enabled_defaults_to_false_and_only_an_explicit_true_turns_it_on(monkeypatch):
    monkeypatch.delenv(svc.CONTRACTS_ENABLED_ENV, raising=False)
    assert svc.contracts_enabled() is False
    for value in ("", "0", "false", "no", "nonsense"):
        monkeypatch.setenv(svc.CONTRACTS_ENABLED_ENV, value)
        assert svc.contracts_enabled() is False, value
    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv(svc.CONTRACTS_ENABLED_ENV, value)
        assert svc.contracts_enabled() is True, value


# --------------------------------------------------------------------------- chunking


def test_chunking_yields_one_row_per_numbered_clause_and_keeps_a_table_with_its_clause():
    meta, body = svc.split_front_matter(TETRANET_FILE.read_text(encoding="utf-8"))
    clauses = svc.chunk_clauses(body, title=meta["title"])
    numbers = [c.clause_number for c in clauses]
    assert len(numbers) == len(set(numbers)) == 34
    assert numbers[:4] == ["1.1", "1.2", "1.3", "1.4"]
    by_no = {c.clause_number: c for c in clauses}
    # Golden q02: the answer is a table row, so the table must travel with clause 2.2.
    assert "| Rural, beyond 90 km | 240 minutes | 480 minutes |" in by_no["2.2"].text
    assert "Rural" not in by_no["2.3"].text
    assert [c.ordinal for c in clauses] == list(range(34))


def test_chunking_skips_the_synthetic_banners_and_carries_the_section_heading_into_the_context_header():
    meta, body = svc.split_front_matter(EGYPRO_FILE.read_text(encoding="utf-8"))
    clauses = svc.chunk_clauses(body, title=meta["title"])
    joined = "\n".join(c.text for c in clauses)
    assert "THIS FILE IS FICTION" not in joined and "END OF SYNTHETIC SAMPLE" not in joined
    c41 = next(c for c in clauses if c.clause_number == "4.1")
    assert c41.heading == "4. Service Levels — Response and Restoration (SAMPLE — FICTIONAL)".replace("4. ", "")
    assert "SAMPLE" in c41.context_header and "FICTIONAL" in c41.context_header
    assert c41.context_header.startswith(meta["title"] + " §4.1 ")
    assert c41.parent_path.endswith("> 4.1")


def test_a_file_with_no_front_matter_and_a_file_with_no_clauses_are_handled_without_raising(tmp_path):
    assert svc.split_front_matter("just text") == ({}, "just text")
    assert svc.chunk_clauses("# Title\n\nNo numbered clauses here.\n", title="T") == []


# --------------------------------------------------------------------------- the tenant filter


def test_retrieve_clauses_requires_a_non_empty_allow_set_and_raises_value_error_otherwise(session):
    with pytest.raises(ValueError, match="allow-set must be non-empty"):
        svc.retrieve_clauses(session, "response time", allowed_contract_ids=frozenset())
    with pytest.raises(ValueError):
        svc.retrieve_clauses(session, "response time", allowed_contract_ids=set())
    # The argument has no default: leaving it out is a TypeError at the call site, not a full scan.
    with pytest.raises(TypeError):
        svc.retrieve_clauses(session, "response time")  # type: ignore[call-arg]


def test_a_caller_holding_one_msps_allow_set_cannot_get_the_other_msps_clause_out_of_retrieval(session):
    # Proof the query WOULD find it when allowed: Tetranet 2.5 is a top hit with both contracts.
    both = svc.retrieve_clauses(session, FUEL_QUESTION, allowed_contract_ids=frozenset({EGYPRO, TETRANET}))
    assert (TETRANET, "2.5") in [(h.contract_id, h.clause_number) for h in both]
    # Scoped to the Egypro sample only — golden q10, "the most important row in the set".
    scoped = svc.retrieve_clauses(session, FUEL_QUESTION, allowed_contract_ids=frozenset({EGYPRO}), k=100)
    assert scoped, "the Egypro sample still has lexical neighbours; an empty list would hide a broken index"
    assert {h.contract_id for h in scoped} == {EGYPRO}
    assert all(h.clause_number != "2.5" or h.contract_id == EGYPRO for h in scoped)


def test_an_allow_set_naming_another_operators_contract_returns_none_of_its_clauses(session):
    """The operator clause from ``api.deps._owned`` is applied even when the caller holds the id."""
    other = svc.ingest_contract(session, path=TETRANET_FILE, operator_id="airtel", contract_id="airtel-sla")
    session.commit()
    assert other.contract.operator_id == "airtel" and other.clauses == 34
    hits = svc.retrieve_clauses(session, FUEL_QUESTION, allowed_contract_ids=frozenset({"airtel-sla"}))
    assert hits == []
    # ...and the active operator's own set is untouched by the other operator's rows.
    own = svc.retrieve_clauses(session, FUEL_QUESTION, allowed_contract_ids=frozenset({EGYPRO, TETRANET}))
    assert {h.contract_id for h in own} <= {EGYPRO, TETRANET}


def test_fts_query_neutralises_fts5_syntax_typed_by_a_user(session):
    nasty = 'heading:"foo" AND NOT (bar OR baz) * NEAR/2 ^prefix restoration'
    expr = svc.fts_query(nasty)
    assert '"restoration"' in expr and " OR " in expr and "NEAR" not in expr.replace('"near"', "")
    hits = svc.retrieve_clauses(session, nasty, allowed_contract_ids=frozenset({EGYPRO}))
    assert isinstance(hits, list) and hits, "a query with FTS5 syntax must still search, not 500"
    assert svc.fts_query("the of and") == "" and svc.retrieve_clauses(session, "the of and", allowed_contract_ids=frozenset({EGYPRO})) == []


def test_bm25_ranks_the_governing_clause_first_for_a_plain_lookup(session):
    hits = svc.retrieve_clauses(session, "What is the response time for a Priority 1 fault?", allowed_contract_ids=frozenset({EGYPRO}))
    assert (hits[0].contract_id, hits[0].clause_number) == (EGYPRO, "4.1")
    assert hits[0].rank == 1 and hits[0].score <= hits[-1].score  # bm25: lower is better


# --------------------------------------------------------------------------- allow-set derivation


def test_allowed_contracts_for_is_role_intersect_allowed_roles_with_no_admin_bypass(session):
    assert svc.allowed_contracts_for(session, role="noc_analyst", incident_id=None, vendor_id=None) == frozenset()
    assert svc.allowed_contracts_for(session, role="duty_manager", incident_id=None, vendor_id=None) == {EGYPRO, TETRANET}
    assert svc.allowed_contracts_for(session, role="", incident_id=None, vendor_id=None) == frozenset()
    # admin sees both today only because Legal listed it on both samples...
    assert svc.allowed_contracts_for(session, role="admin", incident_id=None, vendor_id=None) == {EGYPRO, TETRANET}
    egypro = session.get(ContractRow, EGYPRO)
    egypro.allowed_roles = ["legal"]
    session.commit()
    # ...and loses one the moment it is not listed: there is no implicit bypass.
    assert svc.allowed_contracts_for(session, role="admin", incident_id=None, vendor_id=None) == {TETRANET}
    assert svc.allowed_contracts_for(session, role="legal", incident_id=None, vendor_id=None) == {EGYPRO, TETRANET}


def test_allowed_contracts_for_narrows_to_the_incidents_vendor_and_denies_an_unowned_incident(session):
    def incident(operator_id: str, vendor_id: str | None, msp_name: str | None = None) -> str:
        inc = IncidentRow(
            operator_id=operator_id, incident_number=f"INC-{vendor_id or msp_name}-{operator_id}", site_id="SFC-X",
            region_code="NBI_E", correlation_fingerprint="x", vendor_id=vendor_id, msp_name=msp_name,
        )
        session.add(inc)
        session.flush()
        return inc.id

    egypro_inc = incident("safaricom", "vendor-sfc-egypro")
    other_operator_inc = incident("airtel", "vendor-sfc-tetranet")
    session.commit()
    assert svc.allowed_contracts_for(session, role="duty_manager", incident_id=egypro_inc, vendor_id=None) == {EGYPRO}
    assert svc.allowed_contracts_for(session, role="duty_manager", incident_id="no-such-incident", vendor_id=None) == frozenset()
    # Another operator's incident: 404-equivalent — nothing, not the un-narrowed set.
    assert svc.allowed_contracts_for(session, role="duty_manager", incident_id=other_operator_inc, vendor_id=None) == frozenset()
    # An explicit vendor narrows the same way, and a role with no access stays empty however it is narrowed.
    assert svc.allowed_contracts_for(session, role="duty_manager", incident_id=None, vendor_id="vendor-sfc-tetranet") == {TETRANET}
    assert svc.allowed_contracts_for(session, role="noc_analyst", incident_id=egypro_inc, vendor_id=None) == frozenset()


# --------------------------------------------------------------------------- answer path, LLM off


def test_with_the_llm_off_the_answer_is_the_verbatim_clause_list_with_the_disclosure_and_no_official_label(session):
    out = svc.answer_question(session, question="What is the response time for a Priority 1 fault?", role="duty_manager", actor="Duty Manager", llm=None)
    session.commit()
    assert out.source == svc.SOURCE_DETERMINISTIC
    assert out.validated is True and out.official is False and out.escalated_to_legal is False
    assert svc.DISCLOSURE in out.answer and svc.DETERMINISTIC_NOTE in out.answer
    assert out.citations[0]["clause_number"] == "4.1" and out.citations[0]["contract_id"] == EGYPRO
    clause = session.scalar(select(ContractClauseRow).where(ContractClauseRow.contract_id == EGYPRO, ContractClauseRow.clause_number == "4.1"))
    assert out.citations[0]["cited_text"] == clause.text, "no generated text: the citation IS the clause"
    assert out.model is None and out.llm_call_id is None and out.transfer_record_id is None
    assert sorted(out.allowed_contract_ids) == sorted([EGYPRO, TETRANET])
    # Every answer is recorded, with the allow-set it was answered under.
    row = session.get(ContractQueryRow, out.query_id)
    assert row.source == svc.SOURCE_DETERMINISTIC and row.validated == 1 and row.role == "duty_manager"
    assert set(__import__("json").loads(row.allowed_contract_ids_json)) == {EGYPRO, TETRANET}
    audit = session.scalars(select(AuditRow).where(AuditRow.action == svc.AUDIT_ACTION)).all()
    assert len(audit) == 1 and "Priority 1" not in audit[0].payload_json, "no free text in the audit payload"


def test_an_asker_with_no_accessible_contract_gets_a_recorded_refusal_not_a_value_error(session):
    out = svc.answer_question(session, question="What is the response time for a Priority 1 fault?", role="noc_analyst", actor="Analyst", llm=None)
    session.commit()
    assert out.source == svc.SOURCE_REFUSED and out.escalated_to_legal is True and out.official is False
    assert svc.REFUSAL_ACCESS_TEXT in out.answer and svc.REFUSAL_TEXT in out.answer
    assert out.allowed_contract_ids == [] and out.nearest_clauses == []
    row = session.get(ContractQueryRow, out.query_id)
    assert row.source == svc.SOURCE_REFUSED and row.escalated_to_legal == 1


def test_the_faq_is_returned_first_official_and_only_when_the_asker_may_see_every_contract_it_quotes(session):
    faq = svc.create_faq(
        session,
        question="How long does a vendor have to dispute a line in the monthly performance report?",
        approved_answer="Ten (10) working days — SYNTHETIC sample answer, not legal advice.",
        approved_by="Legal (test)",
        contract_ids=[EGYPRO, TETRANET],
        clause_refs=[{"contract_ref": "SAMPLE-MSA-EGYPRO-2026-01", "clause": "10.2"}],
    )
    session.commit()
    # Paraphrased, so the token-overlap matcher (not string equality) is what is being tested.
    out = svc.answer_question(session, question="how long does a vendor have to dispute a line in the monthly report?", role="duty_manager", actor="DM", llm=None)
    assert out.source == svc.SOURCE_FAQ and out.official is True and out.validated is True and out.faq_id == faq.id
    assert out.answer == faq.approved_answer and out.disclosure.startswith(svc.FAQ_DISCLOSURE_PREFIX)
    assert out.citations[0]["clause_number"] == "10.2"
    # Narrowed to one vendor, the asker may not see everything the FAQ quotes → no FAQ, clause list instead.
    narrowed = svc.answer_question(session, question=faq.question, role="duty_manager", actor="DM", vendor_id="vendor-sfc-egypro", llm=None)
    assert narrowed.source == svc.SOURCE_DETERMINISTIC and narrowed.official is False
    # An unrelated question does not match the FAQ.
    unrelated = svc.answer_question(session, question="What is the response time for a Priority 1 fault?", role="duty_manager", actor="DM", llm=None)
    assert unrelated.source == svc.SOURCE_DETERMINISTIC


def test_create_faq_refuses_an_unknown_contract_and_an_empty_approver(session):
    with pytest.raises(ValueError, match="unknown contract"):
        svc.create_faq(session, question="q?", approved_answer="a", approved_by="Legal", contract_ids=["nope"], clause_refs=[])
    with pytest.raises(ValueError):
        svc.create_faq(session, question="q?", approved_answer="a", approved_by="", contract_ids=[], clause_refs=[])
    assert session.scalars(select(ContractFaqRow)).all() == []


def test_ingest_is_an_upsert_that_rebuilds_the_clauses_rather_than_duplicating_them(session):
    before = session.scalars(select(ContractClauseRow).where(ContractClauseRow.contract_id == EGYPRO)).all()
    again = svc.ingest_contract(session, path=EGYPRO_FILE)
    session.commit()
    assert again.replaced is True and again.contract.id == EGYPRO
    after = session.scalars(select(ContractClauseRow).where(ContractClauseRow.contract_id == EGYPRO)).all()
    assert len(before) == len(after) == 62
    assert session.scalar(select(ContractRow.token_count).where(ContractRow.id == EGYPRO)) == again.est_tokens > 0
    # The FTS index was rebuilt too: exactly one entry per clause, no orphans from the first ingest.
    hits = svc.retrieve_clauses(session, "response time priority", allowed_contract_ids=frozenset({EGYPRO}), k=100)
    assert len({h.clause_id for h in hits}) == len(hits)


# --------------------------------------------------------------------------- API


def test_every_contracts_route_is_404_while_the_flag_is_off(client, monkeypatch):
    monkeypatch.delenv("CONTRACTS_ENABLED", raising=False)
    _as(client, "legal")
    assert client.get("/api/v1/contracts").status_code == 404
    assert client.get("/api/v1/contracts/status").status_code == 404
    assert client.get("/api/v1/contracts/clauses/search?q=response").status_code == 404
    assert client.post("/api/v1/contracts/ask", json={"question": "anything?"}).status_code == 404
    assert client.get("/api/v1/contracts/faq").status_code == 404
    assert client.get("/api/v1/contracts/queries").status_code == 404
    assert client.post("/api/v1/contracts/ingest-samples").status_code == 404


def test_the_ask_route_ignores_an_allow_set_supplied_in_the_body(client):
    _ingest_samples_via_app()
    # A role with no access cannot buy it with a body field.
    _as(client, "noc_analyst")
    r = client.post("/api/v1/contracts/ask", json={"question": FUEL_QUESTION, "allowed_contract_ids": [EGYPRO, TETRANET]})
    assert r.status_code == 200, r.text
    assert r.json()["source"] == svc.SOURCE_REFUSED and r.json()["allowed_contract_ids"] == []
    # A role with access cannot narrow OR widen it either: the server's set is what is used.
    _as(client, "duty_manager")
    r = client.post("/api/v1/contracts/ask", json={"question": FUEL_QUESTION, "allowed_contract_ids": [EGYPRO]})
    body = r.json()
    assert sorted(body["allowed_contract_ids"]) == sorted([EGYPRO, TETRANET])
    assert body["source"] == svc.SOURCE_DETERMINISTIC and body["disclosure"] == svc.DISCLOSURE
    assert any(c["contract_id"] == TETRANET and c["clause_number"] == "2.5" for c in body["citations"])


def test_clause_search_is_scoped_by_role_and_returns_an_explained_empty_list_for_a_role_with_no_access(client):
    _ingest_samples_via_app()
    _as(client, "field_engineer")
    r = client.get("/api/v1/contracts/clauses/search", params={"q": FUEL_QUESTION})
    assert r.status_code == 200 and r.json()["hits"] == [] and "field_engineer" in r.json()["reason"]
    _as(client, "management")
    r = client.get("/api/v1/contracts/clauses/search", params={"q": FUEL_QUESTION, "k": 5})
    hits = r.json()["hits"]
    assert 1 <= len(hits) <= 5 and (hits[0]["contract_id"], hits[0]["clause_number"]) == (TETRANET, "2.5")
    assert client.get("/api/v1/contracts/clauses/search", params={"q": "x", "k": 0}).status_code == 422


def test_contract_list_and_status_report_the_measured_corpus_to_the_asker(client):
    _ingest_samples_via_app()
    _as(client, "management")
    rows = client.get("/api/v1/contracts").json()
    assert sorted(r["id"] for r in rows) == sorted([EGYPRO, TETRANET])
    assert all("SAMPLE" in r["title"] and r["clauses"] > 0 and r["token_count"] > 0 for r in rows)
    status = client.get("/api/v1/contracts/status").json()
    assert status["enabled"] is True and status["fts5_available"] is True
    assert status["corpus"]["contracts"] == 2 and status["corpus"]["fits_in_prompt"] is True
    assert status["llm"]["cited_answers"] is False and status["llm"]["model"] == "claude-opus-5"
    _as(client, "noc_analyst")
    assert client.get("/api/v1/contracts").json() == []


def test_ingest_refuses_pdf_binary_missing_confidentiality_and_paths_outside_the_contract_folders(client, tmp_path, monkeypatch):
    folder = tmp_path / "contracts"
    folder.mkdir()
    monkeypatch.setattr(svc, "CONTRACTS_DIR", folder)
    (folder / "scan.pdf").write_bytes(b"%PDF-1.7 not text")
    (folder / "blob.md").write_bytes(b"1.1 a clause\x00 with a NUL")
    (folder / "unchecked.md").write_text("# T\n\n1.1 A clause nobody in Legal has read.\n", encoding="utf-8")
    (folder / "ok.md").write_text("# T\n\n## 1. Terms\n\n1.1 The Contractor shall respond within nine (9) minutes.\n", encoding="utf-8")
    _as(client, "legal")
    post = lambda **kw: client.post("/api/v1/contracts", json={"counterparty_vendor_id": "vendor-sfc-egypro", "allowed_roles": ["legal"], **kw})  # noqa: E731
    assert post(path="scan.pdf", confidentiality_checked_by="Legal").status_code == 415
    assert post(path="blob.md", confidentiality_checked_by="Legal").status_code == 415
    assert post(path="unchecked.md").status_code == 422
    assert post(path="../../pyproject.toml", confidentiality_checked_by="Legal").status_code in (404, 422)
    assert post(path="does-not-exist.md", confidentiality_checked_by="Legal").status_code == 404
    ok = post(path="ok.md", confidentiality_checked_by="Legal", third_party_processing_permitted=False)
    assert ok.status_code == 201, ok.text
    assert ok.json()["clauses"] == 1 and ok.json()["allowed_roles"] == ["legal"] and ok.json()["third_party_processing_permitted"] is False
    # 20 MB + 1 byte → 413, checked before anything is parsed.
    big = folder / "big.md"
    big.write_bytes(b"1.1 x" + b" " * svc.MAX_SOURCE_BYTES)
    assert post(path="big.md", confidentiality_checked_by="Legal").status_code == 413


def test_the_query_log_records_every_ask_for_legal(client):
    _ingest_samples_via_app()
    _as(client, "duty_manager")
    client.post("/api/v1/contracts/ask", json={"question": FUEL_QUESTION})
    _as(client, "noc_analyst")
    client.post("/api/v1/contracts/ask", json={"question": FUEL_QUESTION})
    _as(client, "legal")
    log = client.get("/api/v1/contracts/queries").json()
    assert [q["source"] for q in log] == [svc.SOURCE_REFUSED, svc.SOURCE_DETERMINISTIC]
    assert [q["role"] for q in log] == ["noc_analyst", "duty_manager"]
    assert client.get("/api/v1/contracts/queries", params={"source": "refused"}).json()[0]["escalated_to_legal"] is True
