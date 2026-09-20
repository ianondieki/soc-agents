"""``llm/cited.py`` and the cited half of ``services/contracts.answer_question`` (spec §7.8.3),
with a FAKE client only — no socket, no ``anthropic`` import, ``LLM_ENABLED=false`` throughout.

Two design constraints from §7.8 are pinned here because they are easy to regress silently:

* **Citations and structured outputs are mutually exclusive (HTTP 400).** The request must
  never carry ``output_config`` / ``output_format`` / ``tools``; the fake records the kwargs
  so the test can look.
* **The refusal path is a feature.** Stanford's >17 % misgrounded-citation figure is why the
  answer the asker sees is a template over *validated verbatim quotes*, and why a true quote
  followed by an unsupported conclusion, a paraphrase, or an uncited answer each end in
  ``source=refused`` with the nearest clauses and ``escalated_to_legal=1`` — not in a 500 and
  not in a confident answer.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from noc_agents.db.models import AuditRow, LlmCallRow
from noc_agents.db.models_contracts import ContractQueryRow, ContractRow
from noc_agents.llm import cited
from noc_agents.llm.cited import CITED_MODEL, Citation, CitedAnswer, CitedDocument, cited_answer, validate_citations
from noc_agents.llm.client import MODEL_REASONING
from noc_agents.services import contracts as svc
from noc_agents.services.external_calls import ACTION as TRANSFER_ACTION

EGYPRO = "contract-sample-egypro-msa"
TETRANET = "contract-sample-tetranet-sla"
P1_QUESTION = "What is the response time for a Priority 1 fault?"
P1_QUOTE = "Priority 1 | 10 minutes | 90 minutes"


# --------------------------------------------------------------------------- fakes


def _text_block(text: str, citations: list[dict] | None = None) -> dict:
    return {"type": "text", "text": text, "citations": citations or []}


def _cite(doc_index: int, block: int, cited_text: str) -> dict:
    return {
        "type": "content_block_location",
        "document_index": doc_index,
        "document_title": None,
        "start_block_index": block,
        "end_block_index": block + 1,
        "cited_text": cited_text,
    }


def _response(content, *, stop_reason="end_turn", model=CITED_MODEL):
    return SimpleNamespace(model=model, stop_reason=stop_reason, usage=SimpleNamespace(input_tokens=1200, output_tokens=60), content=content)


class FakeClient:
    """``client.messages.create`` that records kwargs and returns a canned response, or raises."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.creates: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.creates.append(kwargs)
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class QuotingFake(FakeClient):
    """Finds ``quote`` inside the documents it is actually sent and cites that block — so the
    test does not hard-code which document index / clause ordinal the service chose."""

    def __init__(self, quote: str, *, prose: str = "The Priority 1 response time is ten minutes.", trailing: str | None = None, cite_as: str | None = None):
        super().__init__()
        self.quote, self.prose, self.trailing, self.cite_as = quote, prose, trailing, cite_as

    def _create(self, **kwargs):
        self.creates.append(kwargs)
        blocks = [b for b in kwargs["messages"][0]["content"] if b.get("type") == "document"]
        norm = lambda s: " ".join(s.split())  # noqa: E731
        for d_i, doc in enumerate(blocks):
            for b_i, block in enumerate(doc["source"]["content"]):
                if norm(self.quote) in norm(block["text"]):
                    content = [_text_block(self.prose, [_cite(d_i, b_i, self.cite_as or self.quote)])]
                    if self.trailing:
                        content.append(_text_block(" " + self.trailing))
                    return _response(content)
        raise AssertionError(f"quote not found in any document sent to the model: {self.quote!r}")


# --------------------------------------------------------------------------- fixtures


@pytest.fixture()
def session(tmp_path, monkeypatch):
    import noc_agents.config as cfg
    import noc_agents.db.models as models

    db = tmp_path / "cited.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
    # DEMO records tia_ref=DEMO-UNFILED instead of refusing (config/operators/safaricom/transfers.yaml);
    # the production gate is tested explicitly below.
    monkeypatch.setenv("NOC_ENV", "demo")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
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


DOCS = [
    CitedDocument(id="c1", title="MSA", blocks=["4.1 Response within ten (10) minutes.", "4.2 Restoration within ninety (90) minutes."], block_refs=["4.1", "4.2"]),
    CitedDocument(id="c2", title="SLA", blocks=["2.1 Acknowledge within fifteen (15) minutes."], block_refs=["2.1"]),
]


# --------------------------------------------------------------------------- the request


def test_the_request_enables_citations_on_custom_content_documents_and_never_sets_output_config():
    fake = FakeClient(_response([_text_block("Ten minutes.", [_cite(0, 0, "ten (10) minutes")])]))
    answer, rec = cited_answer(fake, question="How fast?", docs=DOCS, timeout=12.5)
    assert rec.ok and answer is not None
    kw = fake.creates[0]
    assert kw["model"] == CITED_MODEL == "claude-opus-5" and kw["timeout"] == 12.5 and kw["system"] == cited.SYSTEM_CITED
    for forbidden in ("output_config", "output_format", "tools", "tool_choice", "betas", "thinking"):
        assert forbidden not in kw, f"{forbidden} would make the Citations API return HTTP 400"
    content = kw["messages"][0]["content"]
    assert [b["type"] for b in content] == ["document", "document", "text"]
    assert content[0]["source"] == {"type": "content", "content": [{"type": "text", "text": b} for b in DOCS[0].blocks]}
    assert content[0]["citations"] == {"enabled": True} and content[0]["title"] == "MSA"
    assert content[-1] == {"type": "text", "text": "How fast?"}
    assert rec.input_tokens == 1200 and rec.output_tokens == 60 and rec.model_used == CITED_MODEL


def test_fable_is_never_the_model_for_cited_answers():
    """§7.8.4: opus (ZDR-eligible) for clause text; Fable is a Covered Model with 30-day retention."""
    assert CITED_MODEL == "claude-opus-5" and CITED_MODEL != MODEL_REASONING


def test_refusal_is_checked_before_any_content_block_is_read():
    class Exploding(list):
        def __iter__(self):
            raise AssertionError("content was read on a refusal")

    fake = FakeClient(_response(Exploding([_text_block("never")]), stop_reason="refusal"))
    answer, rec = cited_answer(fake, question="q", docs=DOCS)
    assert answer is None and rec.refused is True and rec.error == "refusal" and rec.ok is False


def test_cited_answer_never_raises_when_the_client_explodes_and_records_the_class_only():
    answer, rec = cited_answer(FakeClient(RuntimeError("connection reset by peer at 10.0.0.1")), question="q", docs=DOCS)
    assert answer is None and rec.ok is False and rec.error.startswith("RuntimeError")


def test_a_response_with_no_text_block_is_none_with_the_reason_recorded():
    answer, rec = cited_answer(FakeClient(_response([{"type": "tool_use", "name": "x"}], stop_reason="tool_use")), question="q", docs=DOCS)
    assert answer is None and rec.ok is False and "no text content" in rec.error and "tool_use" in rec.error


def test_an_empty_block_is_sent_as_a_space_so_later_indexes_are_not_shifted():
    blocks = cited.document_blocks([CitedDocument(id="d", title="t", blocks=["a", "", "c"])])
    assert [b["text"] for b in blocks[0]["source"]["content"]] == ["a", " ", "c"]


# --------------------------------------------------------------------------- the validator


def test_a_verbatim_citation_validates_and_maps_to_the_clause_number():
    answer, _ = cited_answer(FakeClient(_response([_text_block("Ten minutes to respond.", [_cite(0, 0, "ten (10) minutes")])])), question="q", docs=DOCS)
    assert validate_citations(answer, DOCS) is True
    cit = answer.citations[0]
    assert (cit.document_id, cit.block_index, cit.clause_number) == ("c1", 0, "4.1")
    assert answer.is_grounded and answer.raw_text == "Ten minutes to respond."


def test_a_paraphrased_citation_is_rejected_by_the_validator():
    answer = CitedAnswer(sentences=[("Ten minutes to respond.", [Citation("c1", 0, "respond in 10 min", "4.1")])], raw_text="Ten minutes to respond.")
    assert validate_citations(answer, DOCS) is False


def test_a_true_quote_followed_by_an_uncited_conclusion_is_rejected():
    """The misgrounding case: a real quote lends its authority to a sentence the clause does not support."""
    answer = CitedAnswer(
        sentences=[
            ("Response is within ten minutes.", [Citation("c1", 0, "ten (10) minutes", "4.1")]),
            (" Therefore a 50% service credit is due this month.", []),
        ],
        raw_text="Response is within ten minutes. Therefore a 50% service credit is due this month.",
    )
    assert validate_citations(answer, DOCS) is False


def test_a_short_connective_fragment_between_cited_sentences_is_tolerated():
    answer = CitedAnswer(
        sentences=[
            ("Response is within ten minutes.", [Citation("c1", 0, "ten (10) minutes", "4.1")]),
            (" See also:", []),
            (" restoration is within ninety minutes.", [Citation("c1", 1, "ninety (90) minutes", "4.2")]),
        ],
        raw_text="Response is within ten minutes. See also: restoration is within ninety minutes.",
    )
    assert validate_citations(answer, DOCS) is True


def test_a_citation_pointing_outside_the_document_or_at_an_unknown_document_is_rejected():
    ok = "ten (10) minutes"
    assert validate_citations(CitedAnswer([("Ten minutes to respond.", [Citation("c1", 5, ok, "?")])], "Ten minutes to respond."), DOCS) is False
    assert validate_citations(CitedAnswer([("Ten minutes to respond.", [Citation("c9", 0, ok, "4.1")])], "Ten minutes to respond."), DOCS) is False
    # Right text, wrong block: the quote exists in the document but not where the citation says.
    assert validate_citations(CitedAnswer([("Ten minutes to respond.", [Citation("c1", 1, ok, "4.2")])], "Ten minutes to respond."), DOCS) is False


def test_an_answer_with_no_citations_or_no_text_is_rejected():
    assert validate_citations(CitedAnswer([("Ten minutes to respond.", [])], "Ten minutes to respond."), DOCS) is False
    assert validate_citations(CitedAnswer([], ""), DOCS) is False
    assert validate_citations(None, DOCS) is False


def test_whitespace_rewrapping_in_cited_text_is_tolerated_but_a_changed_word_is_not():
    rewrapped = Citation("c1", 0, "within ten\n(10)   minutes", "4.1")
    assert validate_citations(CitedAnswer([("Ten minutes to respond.", [rewrapped])], "Ten minutes to respond."), DOCS) is True
    changed = Citation("c1", 0, "within ten (10) hours", "4.1")
    assert validate_citations(CitedAnswer([("Ten minutes to respond.", [changed])], "Ten minutes to respond."), DOCS) is False


def test_non_block_citation_shapes_are_dropped_so_their_sentence_fails_coverage():
    char_loc = {"type": "char_location", "document_index": 0, "start_char_index": 0, "end_char_index": 5, "cited_text": "4.1 R"}
    answer, _ = cited_answer(FakeClient(_response([_text_block("Ten minutes to respond.", [char_loc])])), question="q", docs=DOCS)
    assert answer is not None and answer.citations == [] and validate_citations(answer, DOCS) is False


# --------------------------------------------------------------------------- answer_question, cited path


def test_a_validated_model_answer_is_rendered_from_the_verbatim_quotes_through_the_fixed_template(session):
    fake = QuotingFake(P1_QUOTE)
    out = svc.answer_question(session, question=P1_QUESTION, role="duty_manager", actor="Duty Manager", llm=fake)
    session.commit()
    assert out.source == svc.SOURCE_LLM and out.validated is True and out.official is False and out.escalated_to_legal is False
    first, *_rest, last = out.answer.split("\n")
    assert first == f'Per clause 4.1 of SAMPLE (SYNTHETIC) Managed Services Agreement — Power and Passive Infrastructure (effective 2026-01-01): "{P1_QUOTE}".'
    assert last == svc.DISCLOSURE
    assert "ten minutes" not in out.answer.lower(), "the model's prose never reaches the asker — only the verbatim quote does"
    assert out.citations == [{"contract_id": EGYPRO, "contract_title": first.split(" of ")[1].split(" (effective")[0], "effective_date": "2026-01-01", "clause_number": "4.1", "cited_text": P1_QUOTE}]
    assert len(out.nearest_clauses) == svc.REFUSAL_NEAREST and out.model == CITED_MODEL
    # Paper trail: contract_queries ↔ llm_calls ↔ the reg 41(2) transfer row, all pointing at each other.
    row = session.get(ContractQueryRow, out.query_id)
    assert row.source == svc.SOURCE_LLM and row.validated == 1 and row.model == CITED_MODEL
    call = session.get(LlmCallRow, row.llm_call_id)
    assert call.purpose == svc.LLM_PURPOSE and call.agent == svc.AGENT_NAME and call.validated == 1 and call.ok == 1
    transfer = session.get(AuditRow, row.transfer_record_id)
    assert transfer.action == TRANSFER_ACTION and call.audit_id == transfer.id
    assert svc.TRANSFER_JUSTIFICATION in transfer.rationale and "redacted clause text" in transfer.payload_json
    assert "Anthropic" in transfer.payload_json and '"cross_border": true' in transfer.payload_json


def test_a_misgrounded_answer_becomes_a_refusal_with_the_three_nearest_clauses_and_a_legal_escalation(session):
    fake = QuotingFake(P1_QUOTE, cite_as="Priority 1 faults are answered in ten minutes")  # paraphrase, not a quote
    out = svc.answer_question(session, question=P1_QUESTION, role="duty_manager", actor="DM", llm=fake)
    session.commit()
    assert out.source == svc.SOURCE_REFUSED and out.validated is False and out.escalated_to_legal is True and out.official is False
    assert out.answer.startswith(svc.REFUSAL_TEXT) and svc.REFUSAL_ACCESS_TEXT in out.answer and svc.DISCLOSURE in out.answer
    assert len(out.nearest_clauses) == 3 and out.nearest_clauses[0]["clause_number"] == "4.1"
    assert "Nearest clauses:" in out.answer and out.citations == []
    call = session.get(LlmCallRow, out.llm_call_id)
    assert call.validated == 0 and call.ok == 1, "the call succeeded; the validator refused it — both facts are kept"
    assert session.get(ContractQueryRow, out.query_id).escalated_to_legal == 1


def test_a_true_quote_with_an_unsupported_conclusion_is_refused_not_answered(session):
    fake = QuotingFake(P1_QUOTE, trailing="Therefore a fifty per cent credit is payable this month.")
    out = svc.answer_question(session, question=P1_QUESTION, role="duty_manager", actor="DM", llm=fake)
    assert out.source == svc.SOURCE_REFUSED and "fifty" not in out.answer and "50" not in out.answer.split("Nearest")[0]


def test_an_answer_with_no_citations_at_all_is_a_refusal(session):
    fake = FakeClient(_response([_text_block("The Priority 1 response time is 10 minutes.")]))
    out = svc.answer_question(session, question=P1_QUESTION, role="duty_manager", actor="DM", llm=fake)
    assert out.source == svc.SOURCE_REFUSED and out.escalated_to_legal is True and "10 minutes" not in out.answer.split("Nearest")[0]


def test_a_model_refusal_or_transport_error_degrades_to_the_refusal_path_never_a_500(session):
    refused = svc.answer_question(session, question=P1_QUESTION, role="duty_manager", actor="DM", llm=FakeClient(_response([], stop_reason="refusal")))
    assert refused.source == svc.SOURCE_REFUSED and refused.fallback_reason == "refusal"
    exploded = svc.answer_question(session, question=P1_QUESTION, role="duty_manager", actor="DM", llm=FakeClient(RuntimeError("boom")))
    assert exploded.source == svc.SOURCE_REFUSED and exploded.fallback_reason.startswith("RuntimeError")
    assert session.scalar(select(LlmCallRow).where(LlmCallRow.refused == 1)) is not None


def test_no_clause_leaves_the_building_when_third_party_processing_is_not_permitted(session):
    for cid in (EGYPRO, TETRANET):
        session.get(ContractRow, cid).third_party_processing_permitted = 0
    session.commit()
    fake = QuotingFake(P1_QUOTE)
    out = svc.answer_question(session, question=P1_QUESTION, role="duty_manager", actor="DM", llm=fake)
    assert fake.creates == [], "no call may be made for a contract Legal has not cleared"
    assert out.source == svc.SOURCE_DETERMINISTIC and out.fallback_reason == "third_party_processing_not_permitted"
    assert out.transfer_record_id is None and session.scalars(select(AuditRow).where(AuditRow.action == TRANSFER_ACTION)).all() == []


def test_only_permitted_contracts_are_sent_when_one_of_two_is_cleared(session):
    session.get(ContractRow, TETRANET).third_party_processing_permitted = 0
    session.commit()
    fake = QuotingFake(P1_QUOTE)
    svc.answer_question(session, question=P1_QUESTION, role="duty_manager", actor="DM", llm=fake)
    docs = [b for b in fake.creates[0]["messages"][0]["content"] if b["type"] == "document"]
    assert [d["title"][:40] for d in docs] == ["SAMPLE (SYNTHETIC) Managed Services Agre"]
    assert not any("Contractor" in blk["text"] for blk in docs[0]["source"]["content"]), "no Tetranet clause text was sent"


def test_the_transfer_paperwork_gate_in_production_falls_back_to_the_clause_list_without_a_call(session, monkeypatch):
    monkeypatch.setenv("NOC_ENV", "production")
    fake = QuotingFake(P1_QUOTE)
    out = svc.answer_question(session, question=P1_QUESTION, role="duty_manager", actor="DM", llm=fake)
    assert fake.creates == [] and out.source == svc.SOURCE_DETERMINISTIC and out.fallback_reason == "transfer_paperwork_missing"
    assert out.validated is True and svc.DISCLOSURE in out.answer


def test_the_spend_circuit_stops_the_call_and_the_clause_list_answers_instead(session):
    from noc_agents.llm import client as llm_client

    llm_client.open_spend_cap()
    try:
        fake = QuotingFake(P1_QUOTE)
        out = svc.answer_question(session, question=P1_QUESTION, role="duty_manager", actor="DM", llm=fake)
        assert fake.creates == [] and out.source == svc.SOURCE_DETERMINISTIC and out.fallback_reason == llm_client.FALLBACK_REASON_SPEND_CAP
    finally:
        llm_client.reset_spend_cap()


def test_the_prompt_stuffing_rule_sends_every_permitted_clause_when_the_corpus_fits_and_only_top_k_when_it_does_not(session, monkeypatch):
    fake = QuotingFake(P1_QUOTE)
    out = svc.answer_question(session, question=P1_QUESTION, role="duty_manager", actor="DM", llm=fake)
    docs = [b for b in fake.creates[0]["messages"][0]["content"] if b["type"] == "document"]
    assert sum(len(d["source"]["content"]) for d in docs) == 96, "≈4.7k tokens: the whole allowed corpus goes in (§7.8)"
    assert out.corpus["fits_in_prompt"] is True and out.corpus["est_tokens"] < 10_000
    # Pretend the ceiling were tiny: only the BM25 top-k is sent.
    monkeypatch.setattr(svc, "PROMPT_CORPUS_TOKEN_CEILING", 10)
    fake2 = QuotingFake(P1_QUOTE)
    out2 = svc.answer_question(session, question=P1_QUESTION, role="duty_manager", actor="DM", llm=fake2)
    docs2 = [b for b in fake2.creates[0]["messages"][0]["content"] if b["type"] == "document"]
    assert 1 <= sum(len(d["source"]["content"]) for d in docs2) <= svc.DEFAULT_K and out2.corpus["fits_in_prompt"] is False
    assert out2.source == svc.SOURCE_LLM


def test_clause_text_is_redacted_before_it_leaves_and_the_quote_is_validated_against_the_redacted_text(session):
    text = "Signed by: Jane Doe, Director\n9.9 Contact ops@vendor.example or +254 712 345 678 for access.\nFor and on behalf of the Contractor: J. Doe"
    redacted = svc.redact_clause_text(text)
    assert "Signed by" not in redacted and "on behalf of" not in redacted
    assert "<EMAIL>" in redacted and "<PHONE>" in redacted and "@" not in redacted and "712" not in redacted
    # End to end: every block the fake receives is redacted text (the samples carry no contacts, so equality holds).
    fake = QuotingFake(P1_QUOTE)
    svc.answer_question(session, question=P1_QUESTION, role="duty_manager", actor="DM", llm=fake)
    for doc in (b for b in fake.creates[0]["messages"][0]["content"] if b["type"] == "document"):
        for blk in doc["source"]["content"]:
            assert blk["text"] == svc.redact_clause_text(blk["text"]) and "@" not in blk["text"]


def test_with_the_layer_off_or_a_provider_without_citations_the_default_client_is_none_and_the_clause_list_answers(session, monkeypatch):
    # conftest pins LLM_ENABLED=false: the unset ``llm`` argument resolves to None.
    off = svc.answer_question(session, question=P1_QUESTION, role="duty_manager", actor="DM")
    assert off.source == svc.SOURCE_DETERMINISTIC and off.fallback_reason == "disabled"
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("LLM_PROVIDER", "openai_compat")
    local = svc.answer_question(session, question=P1_QUESTION, role="duty_manager", actor="DM")
    assert local.source == svc.SOURCE_DETERMINISTIC and local.fallback_reason == "provider_without_citations"
    assert local.validated is True and svc.DISCLOSURE in local.answer
