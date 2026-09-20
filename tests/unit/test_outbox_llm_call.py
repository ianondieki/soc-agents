"""The ``LLM_CALL`` transmitter (§7.7.3): the out-of-band PIR draft, through the §7.0.9 port.

``POST /pir/{id}/draft/llm`` has queued a redacted ``LLM_CALL`` row since Phase 4, but
``_TRANSMITTERS`` had no entry for the kind, so every drain marked that row ``DEAD`` with
"no transmitter for outbox kind 'LLM_CALL'" — the lane queued work nobody could do.

No test here opens a socket, imports the SDK or reads a credential: the port is injected as a
fake, the way ``tests/unit/test_llm_port.py`` fakes its adapters. What is proved:

* with ``LLM_ENABLED=false`` (the suite default, and the deployed default) the row is inert —
  finished, not retried, no alarm, and not one database row written for it;
* when the layer is on, the call goes THROUGH the port (so the subscription guard, the
  provider choice and the spend circuit apply) and lands one ``llm_calls`` row linked to the
  reg 41(2) audit row that was committed BEFORE the call;
* §7.0.10's paperwork gate is enforced for the hosted provider: no filed DPIA/TIA, no call;
* only the redacted block leaves, and a payload that still carries contact identifiers is
  refused rather than transmitted;
* a model refusal is terminal, a transport error is retried, and the spend circuit makes the
  row inert rather than failing it.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from noc_agents.db.models import AuditRow, LlmCallRow, OutboxRow, utcnow
from noc_agents.llm.structured import LlmCallRecord
from noc_agents.orchestrator import outbox
from noc_agents.orchestrator.outbox import DEAD, LLM_CALL, PENDING, SENT, drain_once, enqueue
from noc_agents.realtime.hub import hub
from noc_agents.services import pir as pir_service

FIELDS = ["summary", "root_causes", "went_well", "went_poorly", "got_lucky"]
DRAFT = {f: f"drafted {f}" for f in FIELDS}


@pytest.fixture(autouse=True)
def circuit_closed():
    """The spend circuit is process-global: never leave it open for the next test."""
    outbox.llm_client.reset_spend_cap()
    hub._history.clear()
    yield
    outbox.llm_client.reset_spend_cap()
    hub._history.clear()


class FakePort:
    """An ``LlmPort`` that records its call and answers from memory. No SDK, no socket."""

    provider = "anthropic"

    def __init__(self, answer=None, rec: LlmCallRecord | None = None):
        self.calls: list[dict] = []
        self._answer = answer
        self._rec = rec or LlmCallRecord(
            model_requested="claude-opus-5", model_used="claude-opus-5", ok=answer is not None,
            input_tokens=1200, output_tokens=300, latency_ms=900,
        )

    def draft(self, *, model, system, user, output_model, effort="low", max_tokens=2048, timeout=None):
        self.calls.append(
            {"model": model, "system": system, "user": user, "output_model": output_model,
             "effort": effort, "max_tokens": max_tokens, "timeout": timeout}
        )
        answer = output_model(**self._answer) if isinstance(self._answer, dict) else self._answer
        return answer, self._rec

    def cite(self, **_kwargs):  # pragma: no cover - the PIR draft never cites
        raise AssertionError("cite() is not part of this lane")


def _install(monkeypatch, port: FakePort | None) -> FakePort | None:
    """Turn the layer "on" by injecting a port, without touching credentials or the SDK."""
    monkeypatch.setattr(outbox.llm_client, "llm_unavailable_reason", lambda: None)
    monkeypatch.setattr(outbox.llm_client, "get_llm_port", lambda *_a, **_k: port)
    return port


def _payload(**over) -> dict:
    """The shape ``services/pir.queue_llm_draft`` writes (redacted at enqueue)."""
    payload = {
        "operator_id": "safaricom",
        "purpose": pir_service.LLM_DRAFT_PURPOSE,
        "model": pir_service.LLM_DRAFT_MODEL,
        "pir_id": "pir-0001",
        "incident_number": "INC000001",
        "fields": list(FIELDS),
        "redacted_incident": {
            "incident_number": "INC000001",
            "priority": "P1",
            "site_id": "SFC-NBIE-HUB-EMB",
            "assignee_name": "<PERSON_1>",
            "notes": [{"author": "<PERSON_1>", "author_role": "AGENT", "body": "genset out at <PERSON_2> shift"}],
        },
    }
    payload.update(over)
    return payload


def _queue(session, payload: dict, key: str = "pir-llm-draft:pir-0001") -> OutboxRow:
    row = enqueue(session, kind=LLM_CALL, idempotency_key=key, payload=payload, operator_id="safaricom")
    session.commit()
    return row


def _row(session) -> OutboxRow:
    return session.scalars(select(OutboxRow).where(OutboxRow.kind == LLM_CALL)).one()


def _llm_rows(session) -> list[LlmCallRow]:
    return list(session.scalars(select(LlmCallRow)))


def _transfer_rows(session) -> list[AuditRow]:
    return list(session.scalars(select(AuditRow).where(AuditRow.action == "external.call")))


# --- the blocker itself ---------------------------------------------------------------------


def test_the_kind_has_a_transmitter_at_all():
    assert LLM_CALL in outbox._TRANSMITTERS


def test_a_queued_draft_no_longer_dies_for_want_of_a_transmitter(tmp_db):
    settings, session = tmp_db
    _queue(session, _payload())
    report = drain_once(session)
    row = _row(session)
    assert "no transmitter" not in (row.last_error or "")
    assert report.dead == 0 and row.status != DEAD


# --- LLM_ENABLED=false: the inert thing, not a failure ----------------------------------------


def test_with_the_layer_off_the_row_finishes_and_writes_nothing(tmp_db):
    """The rest of the LLM layer answers "use the deterministic path" when it is off; for a
    PIR the deterministic path is the human writing the review. So: SENT (terminal, not
    retried, no ``outbox.failed`` alarm), provider ``none``, and no rows written for it."""
    settings, session = tmp_db
    _queue(session, _payload())
    report = drain_once(session)
    row = _row(session)
    assert row.status == SENT and report.sent == 1
    assert row.provider == outbox.LLM_INERT_PROVIDER
    assert row.last_error is None  # the off switch is not an error worth a last_error
    assert _llm_rows(session) == [] and _transfer_rows(session) == []
    assert [e for e in hub._history if e["type"] == "outbox.failed"] == []


def test_with_the_layer_off_the_port_is_never_even_built(tmp_db, monkeypatch):
    """Asked before any session is opened, so an off deployment does no work for the row."""
    settings, session = tmp_db
    monkeypatch.setattr(outbox.llm_client, "get_llm_port", lambda *_a, **_k: pytest.fail("port built while disabled"))
    _queue(session, _payload())
    assert drain_once(session).sent == 1


def test_the_spend_circuit_makes_the_row_inert_and_says_so(tmp_db, monkeypatch):
    """The provider's own spend limit tripped: the port refuses to build, and the row records
    WHY it drafted nothing instead of going quiet."""
    settings, session = tmp_db
    monkeypatch.setenv("LLM_ENABLED", "true")  # on, but the circuit is open
    outbox.llm_client.open_spend_cap()
    _queue(session, _payload())
    drain_once(session)
    row = _row(session)
    assert row.status == SENT and row.provider == outbox.LLM_INERT_PROVIDER
    assert row.last_error == "no model call: spend_cap"  # visible on the row, not silent
    assert _llm_rows(session) == []


def test_our_own_monthly_budget_stops_the_call_before_it_is_made(tmp_db, monkeypatch):
    """``client.spend_gate`` is consulted with a real session inside the transmitter, so the
    local ceiling stops an out-of-band draft exactly as it stops an on-demand assist."""
    settings, session = tmp_db
    monkeypatch.setenv("NOC_ENV", "demo")
    monkeypatch.setenv("LLM_MONTHLY_BUDGET_USD", "1")
    session.add(
        LlmCallRow(
            id="spent-1", ts=utcnow(), operator_id="safaricom", agent="PostIncidentReviewAgent",
            purpose="pir_draft", provider="anthropic", model_requested="claude-opus-5",
            ok=1, est_cost_usd=2.0, audit_id="audit-0",
        )
    )
    session.commit()
    port = _install(monkeypatch, FakePort(DRAFT))
    _queue(session, _payload())
    drain_once(session)
    row = _row(session)
    assert row.status == SENT and row.last_error == "no model call: budget_exhausted"
    assert port.calls == [] and _transfer_rows(session) == []


# --- the §7.0.10 paperwork gate ----------------------------------------------------------------


def test_the_hosted_call_is_refused_while_the_dpia_and_tia_are_unfiled(tmp_db, monkeypatch):
    """The gate is ON for a hosted model provider (unlike the SMTP relay): no filed DPIA/TIA
    means no call at all, and nothing is recorded because nothing left the machine."""
    settings, session = tmp_db
    monkeypatch.delenv("NOC_ENV", raising=False)  # unset reads as production: the strict side
    port = _install(monkeypatch, FakePort(DRAFT))
    _queue(session, _payload())
    drain_once(session)
    row = _row(session)
    assert row.status == DEAD
    assert "refused" in (row.last_error or "") and "dpia_ref" in (row.last_error or "")
    assert port.calls == []  # the refusal happened BEFORE the call
    assert _llm_rows(session) == []


# --- the call, recorded ------------------------------------------------------------------------


def test_the_call_goes_through_the_port_and_lands_one_linked_llm_calls_row(tmp_db, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("NOC_ENV", "demo")  # records DEMO-UNFILED instead of refusing
    port = _install(monkeypatch, FakePort(DRAFT))
    _queue(session, _payload())

    report = drain_once(session)
    row = _row(session)
    assert row.status == SENT and report.sent == 1
    assert row.provider == "anthropic" and row.provider_message_id == "claude-opus-5"

    transfers = _transfer_rows(session)
    assert len(transfers) == 1
    register = json.loads(transfers[0].payload_json)
    assert register["recipient"] == "Anthropic API" and register["recipient_country"] == "US"
    assert register["cross_border"] is True and register["paperwork_status"] == "demo_unfiled"

    calls = _llm_rows(session)
    assert len(calls) == 1
    call = calls[0]
    assert call.audit_id == transfers[0].id  # the engineering row cites the reg 41(2) row
    assert (call.purpose, call.provider, call.agent) == ("pir_draft", "anthropic", "PostIncidentReviewAgent")
    assert call.ok == 1 and call.validated == 1
    assert (call.input_tokens, call.output_tokens) == (1200, 300)
    assert call.est_cost_usd is not None and call.est_cost_usd > 0  # priced from config/llm_prices.yaml


def test_only_the_redacted_block_is_sent_and_the_shape_is_the_pir_draft(tmp_db, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("NOC_ENV", "demo")
    payload = _payload()
    port = _install(monkeypatch, FakePort(DRAFT))
    _queue(session, payload)
    drain_once(session)

    sent = port.calls[0]
    assert json.loads(sent["user"]) == payload["redacted_incident"]  # nothing else from the row
    assert sent["output_model"] is outbox.PirDraftText
    assert sent["model"] == "claude-opus-5"  # §5.3.18: opus or local, never Fable
    assert "<PERSON_1>" in sent["system"] and "blameless" in sent["system"]


def test_the_drafted_text_is_not_written_into_any_record_here(tmp_db, monkeypatch):
    """Deliberate: review text is ``services/pir.py``'s business — blameless-validated on the
    way in, published by a named human. A dispatcher writing it straight into the review
    would walk around both gates, so this transmitter proves and records the call only."""
    settings, session = tmp_db
    monkeypatch.setenv("NOC_ENV", "demo")
    _install(monkeypatch, FakePort(DRAFT))
    _queue(session, _payload())
    drain_once(session)
    stored = json.dumps([json.loads(r.payload_json or "{}") for r in session.scalars(select(OutboxRow))])
    assert "drafted summary" not in stored
    assert "drafted summary" not in json.dumps([a.payload_json for a in _transfer_rows(session)])


# --- redaction is the producer's job, and this is the last gate --------------------------------


@pytest.mark.parametrize(
    "leak", [{"notes": [{"body": "call the FE on 0712345678"}]}, {"notes": [{"body": "mail ops@example.com"}]}]
)
def test_a_payload_that_still_carries_contact_identifiers_is_refused(tmp_db, monkeypatch, leak):
    """``queue_llm_draft`` redacts before the row is written; ``enqueue`` cannot enforce that,
    so the transmitter re-checks with redaction's own patterns before anything leaves."""
    settings, session = tmp_db
    monkeypatch.setenv("NOC_ENV", "demo")
    port = _install(monkeypatch, FakePort(DRAFT))
    _queue(session, _payload(redacted_incident={"incident_number": "INC000001", **leak}))
    drain_once(session)
    row = _row(session)
    assert row.status == DEAD and "not redacted" in (row.last_error or "")
    assert port.calls == []
    assert "0712345678" not in (row.last_error or "") and "ops@example.com" not in (row.last_error or "")
    assert _transfer_rows(session) == []  # refused before the register, because nothing went


# --- what the model answered -------------------------------------------------------------------


def test_a_model_refusal_is_terminal(tmp_db, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("NOC_ENV", "demo")
    rec = LlmCallRecord(model_requested="claude-opus-5", model_used="claude-opus-5", ok=False, refused=True, error="refusal")
    _install(monkeypatch, FakePort(None, rec))
    _queue(session, _payload())
    drain_once(session)
    row = _row(session)
    assert row.status == DEAD and "model refused" in (row.last_error or "")
    assert _llm_rows(session)[0].refused == 1  # the attempt is still recorded and still costed


def test_an_unusable_answer_is_retried_like_any_transient_failure(tmp_db, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("NOC_ENV", "demo")
    rec = LlmCallRecord(model_requested="claude-opus-5", model_used="claude-opus-5", ok=False, error="APITimeoutError")
    _install(monkeypatch, FakePort(None, rec))
    _queue(session, _payload())
    report = drain_once(session)
    row = _row(session)
    assert report.retried == 1
    assert row.status == PENDING and row.next_attempt_at is not None
    assert "no usable draft" in (row.last_error or "")


# --- the purpose registry ----------------------------------------------------------------------


def test_the_producer_and_the_transmitter_agree_on_the_purpose_and_the_fields():
    """One string and one field list, or the row is refused rather than half-understood."""
    assert pir_service.LLM_DRAFT_PURPOSE in outbox._LLM_PURPOSES
    assert list(outbox.PirDraftText.model_fields) == FIELDS


def test_an_unknown_purpose_is_dead_rather_than_improvised(tmp_db):
    settings, session = tmp_db
    _queue(session, _payload(purpose="exec_brief"))
    drain_once(session)
    row = _row(session)
    assert row.status == DEAD and "no output shape" in (row.last_error or "")


def test_a_field_list_the_shape_does_not_draft_is_dead(tmp_db):
    settings, session = tmp_db
    _queue(session, _payload(fields=["summary", "action_items"]))
    drain_once(session)
    row = _row(session)
    assert row.status == DEAD and "drafts" in (row.last_error or "")


def test_a_row_with_no_redacted_payload_is_dead(tmp_db):
    settings, session = tmp_db
    _queue(session, _payload(redacted_incident={}))
    drain_once(session)
    assert _row(session).status == DEAD
