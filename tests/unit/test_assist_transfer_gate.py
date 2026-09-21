"""CONFORMANCE A-09: the on-demand assist routes go through the reg 41(2) transfer gate.

``POST /incidents/{id}/analysis`` and ``/brief/draft`` both run ``llm/assist.run_assist``.
Before this fix they made the hosted call and wrote only an ``llm.call`` row afterwards;
the transfer register (``services/external_calls.record_transfer``) never saw them and the
DPIA/TIA gate never ran. What is pinned here:

* production + empty refs (the shipped ``transfers.yaml``) -> the model is NOT called, the
  deterministic template answers, and the step rationale says the gate refused;
* ``NOC_ENV=demo`` -> the call proceeds, with a ``DEMO-UNFILED`` transfer row;
* filed refs in production -> the call proceeds with ``paperwork_status="filed"``;
* the transfer row is COMMITTED before the model call (read from a second session while the
  fake model is "thinking", the same proof ``test_outbox_ics_invite`` uses);
* LLM off, or a LOCAL openai-compatible endpoint -> identical to before: no transfer row;
* the payload the model sees is the redacted one (no MSISDN, no assignee name);
* the DB spend gate (``llm.client.spend_gate``) is asked before the record, as the outbox
  LLM_CALL transmitter does: an exhausted ``LLM_MONTHLY_BUDGET_USD`` means no call and no
  transfer row, and a budget under the ceiling still lets the call through.

No network and no SDK: ``assist.get_llm`` is replaced by a fake raw client, the same
injection point ``tests/integration/test_llm_assist.py`` uses.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from noc_agents.db.models import AgentRunRow, AuditRow, IncidentRow, WorkNoteRow, get_session
from noc_agents.llm import assist
from noc_agents.llm import client as llm_client
from noc_agents.llm.client import MODEL_DRAFTING, MODEL_REASONING
from noc_agents.llm.outputs import ExecBriefDraft, Hypothesis, RootCauseAnalysis
from noc_agents.llm.port import record_llm_call
from noc_agents.llm.structured import LlmCallRecord
from noc_agents.services import external_calls
from noc_agents.services.external_calls import DEMO_UNFILED

PHONE = "0712345678"
ASSIGNEE = "Peter Otieno"


# ------------------------------------------------------------------ fixtures / fakes


@pytest.fixture()
def env(tmp_db, monkeypatch):
    """A committed incident, the strict environment and the anthropic provider."""
    settings, session = tmp_db
    monkeypatch.setenv("NOC_ENV", "production")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)  # unset reads as anthropic
    inc = IncidentRow(
        operator_id="safaricom",
        incident_number="INC-A09-1",
        priority="P1",
        status="IN_PROGRESS",
        site_id="SFC-NBI-HUB-001",
        region_code="NBI_E",
        failure_domain="POWER",
        alarm_code="MAINS_FAIL",
        correlation_fingerprint="fp-a09",
        assignee_name=ASSIGNEE,
        access_notes=f"Genset not started. Guard: {PHONE}",
    )
    session.add(inc)
    session.flush()
    session.add(WorkNoteRow(incident_id=inc.id, author=ASSIGNEE, author_role="msp", body=f"Team dispatched, call {PHONE}"))
    # Committed on purpose: run_assist rolls back after prepare(), which would otherwise
    # discard an incident that was only flushed.
    session.commit()
    return settings, session, inc


def _ok(model, parsed):
    return SimpleNamespace(parsed_output=parsed, stop_reason="end_turn", model=model,
                           usage=SimpleNamespace(input_tokens=10, output_tokens=20))


class FakeClient:
    """A raw client with ``beta.messages.parse``; ``on_call`` runs at call time."""

    def __init__(self, reply, on_call=None):
        self.reply = reply
        self.on_call = on_call
        self.calls: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.on_call is not None:
            self.on_call(kwargs)
        return self.reply


def _analysis_reply():
    parsed = RootCauseAnalysis(
        summary="Mains failure with genset not starting",
        hypotheses=[Hypothesis(cause="Genset start failure", likelihood="high", evidence=["MAINS_FAIL"])],
        recommended_checks=["Check the genset controller"],
    )
    return _ok(MODEL_REASONING, parsed)


def _inject(monkeypatch, fake):
    monkeypatch.setattr(assist, "get_llm", lambda *_a, **_k: fake)


def _transfer_rows(incident_id):
    """Read from a FRESH session: only committed rows are visible."""
    s = get_session()
    try:
        return s.scalars(select(AuditRow).where(AuditRow.action == external_calls.ACTION, AuditRow.entity_id == incident_id)).all()
    finally:
        s.close()


def _llm_rows(incident_id):
    s = get_session()
    try:
        return s.scalars(select(AuditRow).where(AuditRow.action == "llm.call", AuditRow.entity_id == incident_id)).all()
    finally:
        s.close()


def _assist_step(run_id):
    s = get_session()
    try:
        run = s.get(AgentRunRow, run_id)
        assert run is not None and run.graph_name == assist.GRAPH_NAME
        step = run.steps[0]
        return {"rationale": step.rationale, "tools": step.tools_called, "status": run.status}
    finally:
        s.close()


# ------------------------------------------------------------------ production, no paperwork


def test_production_with_empty_refs_never_calls_the_model_and_answers_with_the_template(env, monkeypatch):
    settings, session, inc = env
    fake = FakeClient(_analysis_reply())
    _inject(monkeypatch, fake)

    out = assist.analyse_incident(session, settings, inc)

    assert fake.calls == [], "the hosted model was called although the paperwork gate refused"
    assert out["source"] == "template" and out["model"] is None
    assert out["analysis"] == assist.template_analysis(inc).model_dump()
    assert _transfer_rows(inc.id) == []  # nothing left the machine, so nothing is recorded
    assert _llm_rows(inc.id) == []  # and no llm.call row claims a call that never happened
    step = _assist_step(out["run_id"])
    assert "transfer paperwork gate refused" in step["rationale"]
    assert "dpia_ref" in step["rationale"] and "tia_ref" in step["rationale"]
    assert "anthropic_api" in step["rationale"]
    assert step["tools"][0]["name"] == "template" and step["status"] == "SUCCEEDED"


def test_production_refusal_covers_the_brief_draft_route_too(env, monkeypatch):
    settings, session, inc = env
    fake = FakeClient(_ok(MODEL_DRAFTING, ExecBriefDraft(body="INC-A09-1 P1 brief")))
    _inject(monkeypatch, fake)

    out = assist.draft_exec_brief(session, settings, inc)

    assert fake.calls == []
    assert out["source"] == "template" and out["body"]
    assert _transfer_rows(inc.id) == [] and _llm_rows(inc.id) == []
    assert "transfer paperwork gate refused" in _assist_step(out["run_id"])["rationale"]


# ------------------------------------------------------------------ demo: recorded, then called


def test_demo_records_a_demo_unfiled_transfer_before_the_call_and_the_call_proceeds(env, monkeypatch):
    settings, session, inc = env
    monkeypatch.setenv("NOC_ENV", "demo")
    seen_at_call_time: list[list[dict]] = []

    def check_register(_kwargs):
        # A second session sees only COMMITTED rows: the record must be durable before the
        # bytes leave, not merely flushed inside the request's transaction.
        seen_at_call_time.append([json.loads(r.payload_json) for r in _transfer_rows(inc.id)])

    fake = FakeClient(_analysis_reply(), on_call=check_register)
    _inject(monkeypatch, fake)

    out = assist.analyse_incident(session, settings, inc)

    assert len(fake.calls) == 1 and out["source"] == "llm"
    assert len(seen_at_call_time) == 1 and len(seen_at_call_time[0]) == 1, "no committed transfer row at call time"
    rec = seen_at_call_time[0][0]
    assert rec["paperwork_status"] == external_calls.STATUS_DEMO_UNFILED
    assert rec["dpia_ref"] == DEMO_UNFILED and rec["tia_ref"] == DEMO_UNFILED
    assert rec["recipient"] == "Anthropic API" and rec["recipient_country"] == "US"
    assert rec["residency"] == "abroad" and rec["cross_border"] is True
    assert rec["incident_id"] == inc.id and rec["env"] == "demo"

    # The existing llm.call row is still written, and now points at the transfer record.
    (transfer,) = _transfer_rows(inc.id)
    (llm_row,) = _llm_rows(inc.id)
    llm_payload = json.loads(llm_row.payload_json)
    assert llm_payload["recipient"] == assist.RECIPIENT and llm_payload["ok"] is True
    assert llm_payload["transfer_record_id"] == transfer.id


def test_filed_paperwork_in_production_lets_the_call_through(env, monkeypatch):
    settings, session, inc = env
    register = {"anthropic_api": {"entity": "Anthropic PBC", "country": "US", "dpia_ref": "DPIA-2026-01", "tia_ref": "TIA-2026-01"}}
    monkeypatch.setattr(external_calls, "load_transfers", lambda _op: register)
    fake = FakeClient(_analysis_reply())
    _inject(monkeypatch, fake)

    out = assist.analyse_incident(session, settings, inc)

    assert len(fake.calls) == 1 and out["source"] == "llm"
    (transfer,) = _transfer_rows(inc.id)
    rec = json.loads(transfer.payload_json)
    assert rec["paperwork_status"] == external_calls.STATUS_FILED and rec["tia_ref"] == "TIA-2026-01"


# ------------------------------------------------------------------ redaction still precedes the call


def test_the_model_only_sees_the_redacted_payload(env, monkeypatch):
    settings, session, inc = env
    monkeypatch.setenv("NOC_ENV", "demo")
    fake = FakeClient(_analysis_reply())
    _inject(monkeypatch, fake)

    assist.analyse_incident(session, settings, inc)

    sent = json.dumps(fake.calls[0], default=str)
    assert PHONE not in sent and ASSIGNEE not in sent and "Otieno" not in sent
    user = json.loads(fake.calls[0]["messages"][0]["content"])
    assert user["assignee_name"] == "<PERSON_1>"
    # Nor does the register row carry them: it describes the data, it does not contain it.
    blob = " ".join(r.payload_json for r in _transfer_rows(inc.id))
    assert PHONE not in blob and ASSIGNEE not in blob


# ------------------------------------------------------------------ no hosted call: unchanged


def test_llm_off_is_unchanged_and_writes_no_transfer_row(env, monkeypatch):
    settings, session, inc = env
    monkeypatch.setenv("LLM_ENABLED", "false")  # the suite default, pinned for clarity

    out = assist.analyse_incident(session, settings, inc)

    assert out["source"] == "template" and out["model"] is None
    assert _transfer_rows(inc.id) == [] and _llm_rows(inc.id) == []
    assert _assist_step(out["run_id"])["rationale"] == "LLM assist off: deterministic template"


def test_local_openai_compatible_endpoint_is_unchanged_and_writes_no_transfer_row(env, monkeypatch):
    """``get_llm`` is the raw anthropic client factory, so a local endpoint makes no assist
    call at all today — and therefore needs no register row from this path."""
    settings, session, inc = env
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("LLM_PROVIDER", "openai_compat")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "http://127.0.0.1:11434/v1")

    out = assist.analyse_incident(session, settings, inc)

    assert out["source"] == "template"
    assert _transfer_rows(inc.id) == [] and _llm_rows(inc.id) == []
    assert _assist_step(out["run_id"])["rationale"] == "LLM assist off: deterministic template"


def test_busy_slots_skip_both_the_record_and_the_call(env, monkeypatch):
    """A caller turned away for want of a slot sent nothing, so it must not be in the register."""
    settings, session, inc = env
    monkeypatch.setenv("NOC_ENV", "demo")
    fake = FakeClient(_analysis_reply())
    _inject(monkeypatch, fake)
    held = 0
    while assist._ASSIST_SLOTS.acquire(blocking=False):
        held += 1
    try:
        out = assist.analyse_incident(session, settings, inc)
    finally:
        for _ in range(held):
            assist._ASSIST_SLOTS.release()

    assert fake.calls == [] and out["source"] == "template"
    assert _transfer_rows(inc.id) == []
    assert "assist busy" in _assist_step(out["run_id"])["rationale"]


# ------------------------------------------------------------------ the monthly budget ceiling


def _spend(session, settings, input_tokens):
    """Commit one priced ``llm_calls`` row: claude-opus-5 input is $5 per million tokens
    (config/llm_prices.yaml, pinned by test_llm_port), so 200_000 tokens cost $1.00."""
    rec = LlmCallRecord(model_requested="claude-opus-5", model_used="claude-opus-5", ok=True)
    rec.input_tokens, rec.output_tokens = input_tokens, 0
    record_llm_call(
        session,
        operator_id=settings.operator.operator_id,
        agent="OutboxDispatcher",
        purpose="earlier spend this month",
        provider="anthropic",
        rec=rec,
        audit_id="audit-earlier",
    )
    session.commit()


def test_an_exhausted_monthly_budget_makes_no_call_and_writes_no_transfer_row(env, monkeypatch):
    """``get_llm`` checks only the in-process spend circuit; the ``LLM_MONTHLY_BUDGET_USD``
    ceiling lives in ``llm_calls`` and only ``spend_gate`` reads it. Demo mode, so the
    paperwork gate would let the call through: the budget is the only thing that can stop it."""
    settings, session, inc = env
    monkeypatch.setenv("NOC_ENV", "demo")
    monkeypatch.setenv("LLM_MONTHLY_BUDGET_USD", "1")
    _spend(session, settings, 200_000)  # $1.00 of $1.00: state "stop"
    assert llm_client.spend_gate(session, operator_id=settings.operator.operator_id) == llm_client.FALLBACK_REASON_BUDGET
    session.rollback()
    fake = FakeClient(_analysis_reply())
    _inject(monkeypatch, fake)

    out = assist.analyse_incident(session, settings, inc)

    assert fake.calls == [], "the hosted model was called although the monthly budget is spent"
    assert out["source"] == "template" and out["model"] is None
    assert out["analysis"] == assist.template_analysis(inc).model_dump()
    assert _transfer_rows(inc.id) == []  # nothing left the machine, so nothing is recorded
    assert _llm_rows(inc.id) == []
    step = _assist_step(out["run_id"])
    assert step["rationale"] == "spend gate open (budget_exhausted): deterministic template"
    assert step["tools"][0]["name"] == "template" and step["status"] == "SUCCEEDED"


def test_a_budget_under_the_ceiling_still_lets_the_call_through(env, monkeypatch):
    """Positive control for the test above: same setup at 80 % ("warn", which stops nothing)."""
    settings, session, inc = env
    monkeypatch.setenv("NOC_ENV", "demo")
    monkeypatch.setenv("LLM_MONTHLY_BUDGET_USD", "1")
    _spend(session, settings, 160_000)  # $0.80 of $1.00: state "warn"
    fake = FakeClient(_analysis_reply())
    _inject(monkeypatch, fake)

    out = assist.analyse_incident(session, settings, inc)

    assert len(fake.calls) == 1 and out["source"] == "llm"
    assert len(_transfer_rows(inc.id)) == 1 and len(_llm_rows(inc.id)) == 1
