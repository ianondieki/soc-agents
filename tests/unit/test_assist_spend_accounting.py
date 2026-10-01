"""CONFORMANCE A-15: the on-demand assist routes pay into ``llm_calls``.

``POST /incidents/{id}/analysis`` and ``/brief/draft`` both run ``llm/assist.run_assist``.
Before this fix the path consulted ``llm.client.spend_gate`` but wrote no ``llm_calls`` row,
and the gate sums that table: the two routes most likely to be pressed repeatedly could read
``LLM_MONTHLY_BUDGET_USD`` and never move it. M11 (template-fallback rate, measured from
``llm_calls.fallback_reason``) had the same hole. What is pinned here:

* one attempted hosted call -> exactly ONE ``llm_calls`` row, with the tokens, the priced
  cost, the latency, the run, the incident and the reg 41(2) transfer record it cites;
* the two routes are distinguishable by ``purpose`` (M11 is "per assist function");
* a fable -> opus fallback is ONE call and therefore ONE row (``fallback_used=1``), not two:
  ``parse_structured`` returns one record for both attempts;
* failures, refusals and drafts the validators threw away are all recorded — money left the
  building either way — with a closed ``fallback_reason`` vocabulary M11 can group by;
* nothing that made no call writes a row: LLM off, a saturated slot, the paperwork gate and
  the spend gate itself all leave the table untouched, so nothing is double-counted or
  invented;
* the headline: spend accumulated BY THE ASSIST PATH ALONE exhausts the monthly budget, and
  the next assist call is refused by the gate without reaching the model.

No network and no SDK: ``assist.get_llm`` is replaced by a fake raw client, the same
injection point ``tests/integration/test_llm_assist.py`` and ``test_assist_transfer_gate.py``
use.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from noc_agents.db.models import AuditRow, IncidentRow, LlmCallRow, WorkNoteRow, get_session
from noc_agents.llm import assist
from noc_agents.llm import client as llm_client
from noc_agents.llm.client import MODEL_DRAFTING, MODEL_FALLBACK, MODEL_REASONING
from noc_agents.llm.outputs import ExecBriefDraft, Hypothesis, RootCauseAnalysis
from noc_agents.llm.structured import LlmCallRecord
from noc_agents.services import external_calls

# fable is $10 / MTok in and $50 / MTok out (config/llm_prices.yaml, pinned by test_llm_port),
# so 100_000 input tokens is exactly $1.00 — one assist call can fill a $1 monthly budget.
TOKENS_WORTH_ONE_DOLLAR = 100_000


@pytest.fixture()
def env(tmp_db, monkeypatch):
    """A committed incident in demo mode: the paperwork gate records DEMO-UNFILED and lets the
    call through, so the spend accounting is the only thing under test here."""
    settings, session = tmp_db
    monkeypatch.setenv("NOC_ENV", "demo")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)  # unset reads as anthropic
    inc = IncidentRow(
        operator_id="safaricom",
        incident_number="INC-A15-1",
        priority="P1",
        status="IN_PROGRESS",
        site_id="SFC-NBI-HUB-001",
        region_code="NBI_E",
        failure_domain="POWER",
        alarm_code="MAINS_FAIL",
        correlation_fingerprint="fp-a15",
        assignee_name="Peter Otieno",
        access_notes="Genset not started.",
    )
    session.add(inc)
    session.flush()
    session.add(WorkNoteRow(incident_id=inc.id, author="Peter Otieno", author_role="msp", body="Team dispatched"))
    session.commit()  # run_assist rolls back after prepare(), which would discard a flush-only row
    return settings, session, inc


def _ok(model, parsed, *, input_tokens=10, output_tokens=20):
    return SimpleNamespace(
        parsed_output=parsed,
        stop_reason="end_turn",
        model=model,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


class FakeClient:
    """A raw client with ``beta.messages.parse`` that walks a script, like the other two suites."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _analysis(**usage):
    parsed = RootCauseAnalysis(
        summary="Mains failure with genset not starting",
        hypotheses=[Hypothesis(cause="Genset start failure", likelihood="high", evidence=["MAINS_FAIL"])],
        recommended_checks=["Check the genset controller"],
    )
    return _ok(MODEL_REASONING, parsed, **usage)


def _brief(body):
    return _ok(MODEL_DRAFTING, ExecBriefDraft(body=body))


def _inject(monkeypatch, fake):
    monkeypatch.setattr(assist, "get_llm", lambda *_a, **_k: fake)


def _rows(incident_id=None):
    """Read the committed ``llm_calls`` rows from a FRESH session, oldest first."""
    s = get_session()
    try:
        stmt = select(LlmCallRow).order_by(LlmCallRow.ts, LlmCallRow.id)
        if incident_id is not None:
            stmt = stmt.where(LlmCallRow.incident_id == incident_id)
        return list(s.scalars(stmt).all())
    finally:
        s.close()


def _transfer_rows(incident_id):
    s = get_session()
    try:
        return list(
            s.scalars(
                select(AuditRow).where(AuditRow.action == external_calls.ACTION, AuditRow.entity_id == incident_id)
            ).all()
        )
    finally:
        s.close()


def _llm_audit_rows(incident_id):
    s = get_session()
    try:
        return list(
            s.scalars(select(AuditRow).where(AuditRow.action == "llm.call", AuditRow.entity_id == incident_id)).all()
        )
    finally:
        s.close()


# ------------------------------------------------------------------ one call, one row


def test_a_successful_analysis_writes_exactly_one_priced_llm_calls_row(env, monkeypatch):
    settings, session, inc = env
    fake = FakeClient(_analysis())
    _inject(monkeypatch, fake)

    out = assist.analyse_incident(session, settings, inc)
    assert out["source"] == "llm" and len(fake.calls) == 1

    (row,) = _rows(inc.id)
    assert row.operator_id == settings.operator.operator_id
    assert row.agent == assist.ANALYSIS_AGENT and row.purpose == assist.ANALYSIS_PURPOSE
    assert row.provider == "anthropic"
    assert row.model_requested == MODEL_REASONING and row.model_used == MODEL_REASONING
    assert row.ok == 1 and row.refused == 0 and row.fallback_used == 0
    assert row.input_tokens == 10 and row.output_tokens == 20
    # 10/1e6 * $10 + 20/1e6 * $50 = $0.0011 — the gate can now see it.
    assert row.est_cost_usd == pytest.approx(0.0011)
    assert row.latency_ms is not None and row.latency_ms >= 0
    assert row.validated == 1 and row.fallback_reason is None
    assert row.incident_id == inc.id and row.run_id == out["run_id"]
    # ``audit_id`` cites the reg 41(2) transfer record, exactly as services/contracts and the
    # outbox LLM_CALL transmitter do, so one join answers "what left, and what did it cost".
    (transfer,) = _transfer_rows(inc.id)
    assert row.audit_id == transfer.id
    # and the llm.call audit row is still written, unchanged: one of each, never two.
    assert len(_llm_audit_rows(inc.id)) == 1


def test_the_brief_route_is_counted_under_its_own_purpose(env, monkeypatch):
    settings, session, inc = env
    body = f"{inc.incident_number} ({inc.priority}) power outage at the hub. Next update in 15 minutes."
    _inject(monkeypatch, FakeClient(_brief(body)))

    out = assist.draft_exec_brief(session, settings, inc)
    assert out["source"] == "llm"

    (row,) = _rows(inc.id)
    assert row.purpose == assist.BRIEF_PURPOSE and row.agent == assist.BRIEF_AGENT
    assert row.model_requested == MODEL_DRAFTING and row.validated == 1
    assert assist.ANALYSIS_PURPOSE != assist.BRIEF_PURPOSE  # M11 is measured per assist function


def test_two_assist_calls_write_two_rows_and_not_four(env, monkeypatch):
    """The no-double-count proof: ``record_llm_call`` is the only writer and phase 3 calls it once."""
    settings, session, inc = env
    body = f"{inc.incident_number} ({inc.priority}) power outage at the hub."
    _inject(monkeypatch, FakeClient(_analysis()))
    assist.analyse_incident(session, settings, inc)
    _inject(monkeypatch, FakeClient(_brief(body)))
    assist.draft_exec_brief(session, settings, inc)

    rows = _rows(inc.id)
    assert [r.purpose for r in rows] == [assist.ANALYSIS_PURPOSE, assist.BRIEF_PURPOSE]
    assert len(_llm_audit_rows(inc.id)) == 2 and len(_transfer_rows(inc.id)) == 2
    assert len({r.audit_id for r in rows}) == 2  # each row cites its own transfer record


def test_a_model_fallback_is_one_call_and_one_row(env, monkeypatch):
    """fable errors, opus answers: ``parse_structured`` returns ONE record for both attempts,
    so the register must show one row with ``fallback_used=1`` — not one row per HTTP request."""
    settings, session, inc = env
    APITimeoutError = type("APITimeoutError", (Exception,), {})
    parsed = RootCauseAnalysis(
        summary="Mains failure", hypotheses=[Hypothesis(cause="Genset", likelihood="low", evidence=[])], recommended_checks=[]
    )
    fake = FakeClient(APITimeoutError("timed out"), _ok(MODEL_FALLBACK, parsed))
    _inject(monkeypatch, fake)

    out = assist.analyse_incident(session, settings, inc)
    assert out["source"] == "llm" and [c["model"] for c in fake.calls] == [MODEL_REASONING, MODEL_FALLBACK]

    (row,) = _rows(inc.id)
    assert row.fallback_used == 1 and row.ok == 1 and row.validated == 1
    assert row.model_requested == MODEL_REASONING and row.model_used == MODEL_FALLBACK
    assert row.fallback_reason is None  # the draft WAS used; M11 counts rejections, not retries
    # priced against the model that actually answered, not the one that was asked
    assert row.est_cost_usd == pytest.approx(0.00055)  # opus: 10/1e6*$5 + 20/1e6*$25


# ------------------------------------------------------------------ M11: honest fallback reasons


def test_a_draft_the_validators_reject_is_still_paid_for_and_marked_output_invalid(env, monkeypatch):
    """The model answered (the tokens are billed) and we threw the answer away. That is exactly
    M11's numerator, and the money is real, so the row is written with the cost intact."""
    settings, session, inc = env
    _inject(monkeypatch, FakeClient(_brief("Everything is fine, no incident number here.")))

    out = assist.draft_exec_brief(session, settings, inc)
    assert out["source"] == "template" and inc.incident_number in out["body"]

    (row,) = _rows(inc.id)
    assert row.ok == 0 and row.validated == 0
    assert row.fallback_reason == assist.FALLBACK_REASON_OUTPUT_INVALID
    assert row.input_tokens == 10 and row.est_cost_usd == pytest.approx(0.00055)


def test_a_refusal_on_every_model_is_recorded_as_model_refused(env, monkeypatch):
    settings, session, inc = env
    refusal = SimpleNamespace(parsed_output=None, stop_reason="refusal", model=MODEL_REASONING, usage=None)
    _inject(monkeypatch, FakeClient(refusal, refusal))

    out = assist.analyse_incident(session, settings, inc)
    assert out["source"] == "template"

    (row,) = _rows(inc.id)
    assert row.refused == 1 and row.ok == 0 and row.validated == 0
    assert row.fallback_reason == assist.FALLBACK_REASON_MODEL_REFUSED


def test_a_call_that_produced_nothing_is_recorded_as_no_usable_output(env, monkeypatch):
    settings, session, inc = env
    APITimeoutError = type("APITimeoutError", (Exception,), {})
    _inject(monkeypatch, FakeClient(APITimeoutError("t1"), APITimeoutError("t2")))

    out = assist.analyse_incident(session, settings, inc)
    assert out["source"] == "template"

    (row,) = _rows(inc.id)
    assert row.ok == 0 and row.validated == 0 and row.fallback_used == 1
    assert row.fallback_reason == assist.FALLBACK_REASON_NO_OUTPUT
    assert row.input_tokens == 0 and row.est_cost_usd == pytest.approx(0.0)


def test_a_validation_crash_after_the_call_is_output_invalid_not_a_lost_row(env, monkeypatch):
    settings, session, inc = env
    _inject(monkeypatch, FakeClient(_analysis()))

    def boom(*_a, **_k):
        raise KeyError("restore blew up")

    monkeypatch.setattr(assist, "restore_names", boom)
    out = assist.analyse_incident(session, settings, inc)
    assert out["source"] == "template"

    (row,) = _rows(inc.id)
    assert row.fallback_reason == assist.FALLBACK_REASON_OUTPUT_INVALID and row.validated == 0


def test_the_fallback_reason_vocabulary_is_closed_and_content_free():
    """A GROUP BY on ``fallback_reason`` has to terminate, and §9.5 forbids model text in it."""
    rec = LlmCallRecord(model_requested=MODEL_REASONING)
    rec.ok = True
    assert assist.fallback_reason(rec, used=True) is None

    rec.ok = False
    rec.error = "AuthenticationError: invalid x-api-key sk-ant-super-secret"
    assert assist.fallback_reason(rec, used=False) == assist.FALLBACK_REASON_NO_OUTPUT

    rec.error = f"{assist.VALIDATION_FAILED} (length / INC number / priority)"
    assert assist.fallback_reason(rec, used=False) == assist.FALLBACK_REASON_OUTPUT_INVALID
    rec.error = f"{assist.VALIDATION_CRASHED}: KeyError"
    assert assist.fallback_reason(rec, used=False) == assist.FALLBACK_REASON_OUTPUT_INVALID

    rec.refused = True
    assert assist.fallback_reason(rec, used=False) == assist.FALLBACK_REASON_MODEL_REFUSED
    assert assist.fallback_reason(rec, used=True) is None  # a used draft is never a fallback


# ------------------------------------------------------------------ no call, no row


def test_llm_off_writes_no_row(env, monkeypatch):
    settings, session, inc = env
    monkeypatch.setenv("LLM_ENABLED", "false")  # the suite default, pinned for clarity
    assert assist.analyse_incident(session, settings, inc)["source"] == "template"
    assert _rows(inc.id) == []


def test_the_paperwork_gate_refusal_writes_no_row(env, monkeypatch):
    settings, session, inc = env
    monkeypatch.setenv("NOC_ENV", "production")  # shipped transfers.yaml has no DPIA/TIA refs
    fake = FakeClient(_analysis())
    _inject(monkeypatch, fake)

    assert assist.analyse_incident(session, settings, inc)["source"] == "template"
    assert fake.calls == [] and _rows(inc.id) == []


def test_a_saturated_slot_writes_no_row(env, monkeypatch):
    settings, session, inc = env
    fake = FakeClient(_analysis())
    _inject(monkeypatch, fake)
    held = 0
    while assist._ASSIST_SLOTS.acquire(blocking=False):
        held += 1
    try:
        assert assist.analyse_incident(session, settings, inc)["source"] == "template"
    finally:
        for _ in range(held):
            assist._ASSIST_SLOTS.release()

    assert fake.calls == [] and _rows(inc.id) == []


# ------------------------------------------------------------------ the budget, end to end


def _month_spend(settings):
    s = get_session()
    try:
        return llm_client.month_spend_usd(s, operator_id=settings.operator.operator_id)
    finally:
        s.close()


def test_assist_spend_alone_exhausts_the_budget_and_the_gate_then_refuses_the_next_call(env, monkeypatch):
    """The A-15 gap, end to end. No other path writes a row in this test: every dollar the gate
    sees was spent by ``analyse_incident`` itself. Before the fix the second call went through
    because the table the gate sums stayed empty for ever."""
    settings, session, inc = env
    monkeypatch.setenv("LLM_MONTHLY_BUDGET_USD", "1")
    assert _month_spend(settings) == 0.0
    assert llm_client.spend_gate(session, operator_id=settings.operator.operator_id) is None
    session.rollback()

    first = FakeClient(_analysis(input_tokens=TOKENS_WORTH_ONE_DOLLAR, output_tokens=0))
    _inject(monkeypatch, first)
    out1 = assist.analyse_incident(session, settings, inc)
    assert out1["source"] == "llm" and len(first.calls) == 1

    (row,) = _rows(inc.id)
    assert row.est_cost_usd == pytest.approx(1.0)
    assert _month_spend(settings) == pytest.approx(1.0)  # 100 % of a $1 budget -> state "stop"

    second = FakeClient(_analysis())
    _inject(monkeypatch, second)
    out2 = assist.analyse_incident(session, settings, inc)

    assert second.calls == [], "the assist route called the model although its own spend filled the budget"
    assert out2["source"] == "template" and out2["model"] is None
    assert out2["analysis"] == assist.template_analysis(inc).model_dump()
    # The refused run left no trace in the register or the spend table: nothing left the machine.
    assert len(_rows(inc.id)) == 1 and len(_transfer_rows(inc.id)) == 1
    s = get_session()
    try:
        assert s.scalar(select(func.count()).select_from(LlmCallRow)) == 1
    finally:
        s.close()


def test_a_budget_still_under_the_ceiling_lets_the_second_assist_call_through(env, monkeypatch):
    """Positive control: the same setup at 80 % spends, warns and keeps working."""
    settings, session, inc = env
    monkeypatch.setenv("LLM_MONTHLY_BUDGET_USD", "1")
    _inject(monkeypatch, FakeClient(_analysis(input_tokens=80_000, output_tokens=0)))
    assert assist.analyse_incident(session, settings, inc)["source"] == "llm"
    assert _month_spend(settings) == pytest.approx(0.80)

    s = get_session()
    try:
        assert llm_client.budget_state(s, operator_id=settings.operator.operator_id)["state"] == "warn"
    finally:
        s.close()

    second = FakeClient(_analysis())
    _inject(monkeypatch, second)
    assert assist.analyse_incident(session, settings, inc)["source"] == "llm"
    assert len(second.calls) == 1 and len(_rows(inc.id)) == 2
